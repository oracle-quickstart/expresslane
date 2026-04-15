"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane

Migration engine transplanted from vm_migrator_awesomeworking
(reliable step5/step6 + test handling) into v1.6 UI on 2026-04-11.
Key pieces inherited: time_updated-bump detection in step5,
single-apply step6 with on_job_created callback, validate_stack_boot_volumes
pre-flight, and terminate_test_instances (no Terraform DESTROY).

OCM (Oracle Cloud Migrations) Migration Module

This module provides functions to migrate VMs using Oracle Cloud Infrastructure's
Cloud Migrations service. It's adapted from oci_migration_automation.py to work
with the Flask web application and database.

Migration Steps:
1. Create Migration Project
2. Create Migration Plan
3. Add Asset to Project
4. Replicate Asset (longest step - can take hours)
5. Generate RMS Stack
6. Deploy RMS Stack
"""

import oci
from oci.cloud_migrations import MigrationClient, MigrationClientCompositeOperations
from oci.cloud_bridge import InventoryClient
from oci.object_storage import ObjectStorageClient
from oci.resource_manager import ResourceManagerClient
from oci.cloud_migrations.models import (
    CreateMigrationDetails,
    CreateMigrationPlanDetails,
    CreateMigrationAssetDetails,
    CreateReplicationScheduleDetails,
    VmTargetEnvironment,
    AverageResourceAssessmentStrategy
)
from oci.resource_manager.models import (
    CreateJobDetails,
    ApplyJobPlanResolution
)
from oci.retry import DEFAULT_RETRY_STRATEGY
from oci.exceptions import ServiceError, MaximumWaitTimeExceeded
from datetime import datetime
import time


class OCMMigrationError(Exception):
    """Custom exception for OCM migration errors"""
    pass


def log_to_stream(output_stream, message):
    """Write log message with timestamp to output stream."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_stream.write(f"[{timestamp}] {message}\n")


def format_elapsed_time(seconds):
    """Convert seconds to human-readable format."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def init_clients(output_stream=None):
    """
    Initialize all required OCI clients.

    Args:
        output_stream: Optional output stream for logging

    Returns:
        Dictionary of initialized clients
    """
    from oci_clients import get_oci_client

    if output_stream:
        log_to_stream(output_stream, "Initializing OCI clients...")

    migration_client = get_oci_client(MigrationClient)
    migration_composite = MigrationClientCompositeOperations(migration_client)
    inventory_client = get_oci_client(InventoryClient)
    object_storage_client = get_oci_client(ObjectStorageClient)
    resource_manager_client = get_oci_client(ResourceManagerClient)
    compute_client = get_oci_client(oci.core.ComputeClient)
    blockstorage_client = get_oci_client(oci.core.BlockstorageClient)

    if output_stream:
        log_to_stream(output_stream, "OCI clients initialized successfully")

    return {
        'migration': migration_client,
        'migration_composite': migration_composite,
        'inventory': inventory_client,
        'object_storage': object_storage_client,
        'resource_manager': resource_manager_client,
        'compute': compute_client,
        'blockstorage': blockstorage_client
    }


def get_namespace(object_storage_client):
    """Get the Object Storage namespace for the tenancy."""
    namespace = object_storage_client.get_namespace().data
    return namespace


def extract_bucket_name(object_storage_client, bucket_ocid, namespace, compartment_id, output_stream=None):
    """
    Extract bucket name from bucket OCID.

    Args:
        object_storage_client: OCI Object Storage client
        bucket_ocid: Bucket OCID
        namespace: Object Storage namespace
        compartment_id: Compartment OCID
        output_stream: Optional output stream for logging

    Returns:
        Bucket name (string)
    """
    if output_stream:
        log_to_stream(output_stream, f"Extracting bucket name from OCID: {bucket_ocid}")

    try:
        # List all buckets in the compartment
        buckets = object_storage_client.list_buckets(
            namespace_name=namespace,
            compartment_id=compartment_id
        )

        # Try to match by getting full bucket details for each
        for bucket_summary in buckets.data:
            try:
                # Get full bucket details
                bucket_details = object_storage_client.get_bucket(
                    namespace_name=namespace,
                    bucket_name=bucket_summary.name
                )
                # Check if the bucket ID matches
                if hasattr(bucket_details.data, 'id') and bucket_details.data.id == bucket_ocid:
                    if output_stream:
                        log_to_stream(output_stream, f"Found bucket name: {bucket_summary.name}")
                    return bucket_summary.name
            except Exception:
                # Skip buckets we can't access
                continue

        # If no match found by OCID, look for replication bucket by name pattern
        for bucket_summary in buckets.data:
            if 'replication' in bucket_summary.name.lower():
                if output_stream:
                    log_to_stream(output_stream, f"Found replication bucket by name: {bucket_summary.name}")
                    log_to_stream(output_stream, "  (Could not verify OCID match, using name pattern)")
                return bucket_summary.name

        raise OCMMigrationError(f"Bucket with OCID {bucket_ocid} not found in compartment")

    except Exception as e:
        raise OCMMigrationError(f"Failed to extract bucket name: {str(e)}")


def list_inventory_assets(clients, compartment_id, asset_source_id=None, output_stream=None):
    """
    List all VMs available in Cloud Bridge inventory.

    Args:
        clients: Dictionary of OCI clients
        compartment_id: Target compartment OCID
        asset_source_id: Optional asset source OCID to filter by
        output_stream: Optional output stream for logging

    Returns:
        List of asset objects
    """
    if output_stream:
        log_to_stream(output_stream, "Querying Cloud Bridge inventory...")

    inventory_client = clients['inventory']

    # List inventory assets with pagination
    all_assets = []
    next_page = None
    page_count = 0

    while True:
        page_count += 1
        if next_page:
            response = inventory_client.list_assets(
                compartment_id=compartment_id,
                lifecycle_state="ACTIVE",
                page=next_page
            )
        else:
            response = inventory_client.list_assets(
                compartment_id=compartment_id,
                lifecycle_state="ACTIVE"
            )

        all_assets.extend(response.data.items)

        # Check if there are more pages
        if hasattr(response, 'next_page') and response.next_page:
            next_page = response.next_page
        else:
            break

    if output_stream:
        log_to_stream(output_stream, f"Found {len(all_assets)} active assets across {page_count} page(s)")

    # Filter by asset source if provided
    if asset_source_id:
        filtered_assets = []
        for asset in all_assets:
            if hasattr(asset, 'asset_source_ids') and asset_source_id in asset.asset_source_ids:
                filtered_assets.append(asset)
        if output_stream:
            log_to_stream(output_stream, f"Filtered to {len(filtered_assets)} assets from target source")
        return filtered_assets

    return all_assets


def get_or_create_schedule(clients, schedule_type, compartment_id, target_hour=2, output_stream=None):
    """
    Get or create a replication schedule for warm migrations.

    Args:
        clients: Dictionary of OCI clients
        schedule_type: 'DAILY' or 'WEEKLY'
        compartment_id: Target compartment OCID
        target_hour: Hour of day (0-23 UTC) when sync should run
        output_stream: Optional output stream for logging

    Returns:
        replication_schedule_id: OCID of the replication schedule
    """
    if schedule_type not in ['DAILY', 'WEEKLY']:
        raise OCMMigrationError(f"Invalid schedule_type: {schedule_type}. Must be 'DAILY' or 'WEEKLY'")

    # Validate target_hour
    if not isinstance(target_hour, int) or target_hour < 0 or target_hour > 23:
        target_hour = 2  # Default to 2 AM if invalid

    # Format hour for display name (e.g., "0200" for 2 AM)
    hour_str = str(target_hour).zfill(2) + "00"

    # Build schedule name with hour to avoid reusing wrong schedules
    # Format: OCM-Daily-0200 or OCM-Weekly-1400
    if schedule_type == 'DAILY':
        display_name = f'OCM-Daily-{hour_str}'
        execution_recurrences = f'FREQ=DAILY;BYHOUR={target_hour};BYMINUTE=0;BYSECOND=0'
    else:  # WEEKLY
        display_name = f'OCM-Weekly-{hour_str}'
        execution_recurrences = f'FREQ=WEEKLY;BYDAY=SU;BYHOUR={target_hour};BYMINUTE=0;BYSECOND=0'

    if output_stream:
        log_to_stream(output_stream, f"Looking for existing {schedule_type} replication schedule at {str(target_hour).zfill(2)}:00 UTC...")

    try:
        migration_client = clients['migration']

        # Check if schedule already exists with this specific name
        schedules_response = migration_client.list_replication_schedules(
            compartment_id=compartment_id,
            display_name=display_name,
            lifecycle_state='ACTIVE'
        )

        if schedules_response.data.items:
            # Use existing schedule
            schedule_id = schedules_response.data.items[0].id
            if output_stream:
                log_to_stream(output_stream, f"Found existing schedule: {display_name}")
                log_to_stream(output_stream, f"  Schedule OCID: {schedule_id}")
            return schedule_id

        # Create new schedule
        if output_stream:
            log_to_stream(output_stream, f"Creating new {schedule_type} replication schedule...")
            log_to_stream(output_stream, f"  Display Name: {display_name}")
            log_to_stream(output_stream, f"  Recurrence: {execution_recurrences}")

        create_details = CreateReplicationScheduleDetails(
            compartment_id=compartment_id,
            display_name=display_name,
            execution_recurrences=execution_recurrences
        )

        create_response = migration_client.create_replication_schedule(
            create_replication_schedule_details=create_details,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        schedule_id = create_response.data.id
        if output_stream:
            log_to_stream(output_stream, f"Successfully created replication schedule")
            log_to_stream(output_stream, f"  Schedule OCID: {schedule_id}")

        # Wait for ACTIVE state
        max_wait = 60  # 1 minute
        start_time = time.time()
        while time.time() - start_time < max_wait:
            schedule_response = migration_client.get_replication_schedule(schedule_id)
            if schedule_response.data.lifecycle_state == 'ACTIVE':
                break
            time.sleep(5)

        return schedule_id

    except ServiceError as e:
        error_msg = f"Service error creating replication schedule: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def step1_create_project(clients, vm_name, compartment_id, output_stream=None):
    """
    Step 1: Create Migration Project

    Args:
        clients: Dictionary of OCI clients
        vm_name: Name of the VM to migrate
        compartment_id: Target compartment OCID
        output_stream: Optional output stream for logging

    Returns:
        project_ocid: OCID of the created migration project
    """
    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 1/6] Creating Migration Project")
        log_to_stream(output_stream, "=" * 80)

    project_name = f"{vm_name}-Project"
    if output_stream:
        log_to_stream(output_stream, f"Project name: {project_name}")

    start_time = time.time()

    try:
        migration_client = clients['migration']

        create_details = CreateMigrationDetails(
            display_name=project_name,
            compartment_id=compartment_id
        )

        if output_stream:
            log_to_stream(output_stream, "Creating migration project...")

        # Create the project
        create_response = migration_client.create_migration(
            create_migration_details=create_details,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        project_ocid = create_response.data.id
        if output_stream:
            log_to_stream(output_stream, f"Project created with OCID: {project_ocid}")
            log_to_stream(output_stream, f"Initial state: {create_response.data.lifecycle_state}")
            log_to_stream(output_stream, "Waiting for ACTIVE state (polling every 5 seconds)...")

        # Poll for ACTIVE state
        check_count = 0
        max_wait_time = 300  # 5 minutes timeout

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            if elapsed > max_wait_time:
                raise OCMMigrationError(f"Timeout waiting for project to become ACTIVE (exceeded {max_wait_time}s)")

            # Get current project status
            project_response = migration_client.get_migration(project_ocid)
            current_state = project_response.data.lifecycle_state

            if output_stream:
                log_to_stream(output_stream, f"Check #{check_count}: State = {current_state} | Elapsed: {format_elapsed_time(elapsed)}")

            if current_state == 'ACTIVE':
                break
            elif current_state in ['FAILED', 'DELETED']:
                raise OCMMigrationError(f"Project entered {current_state} state")

            time.sleep(5)

        elapsed = time.time() - start_time

        if output_stream:
            log_to_stream(output_stream, f"SUCCESS: Migration project created")
            log_to_stream(output_stream, f"  Project OCID: {project_ocid}")
            log_to_stream(output_stream, f"  Status: ACTIVE")
            log_to_stream(output_stream, f"  Elapsed time: {format_elapsed_time(elapsed)}")

        return project_ocid

    except ServiceError as e:
        error_msg = f"Service error in Create Project: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def step2_create_plan(clients, project_ocid, vm_name, plan_compartment_id, target_compartment_id, vcn_ocid, subnet_ocid, vm_config=None, vms_list=None, output_stream=None):
    """
    Step 2: Create Migration Plan

    Args:
        clients: Dictionary of OCI clients
        project_ocid: OCID of the migration project
        vm_name: Name of the VM to migrate
        plan_compartment_id: Compartment where the plan will be created (source/migration compartment)
        target_compartment_id: Compartment where the VM will be deployed (destination compartment)
        vcn_ocid: Target VCN OCID
        subnet_ocid: Target subnet OCID
        vm_config: Optional advanced VM configuration (shape, OCPUs, memory, etc.)
        vms_list: Optional list of VMs being migrated
        output_stream: Optional output stream for logging

    Returns:
        plan_ocid: OCID of the created migration plan
    """
    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 2/6] Creating Migration Plan")
        log_to_stream(output_stream, "=" * 80)

    plan_name = f"{vm_name}-Plan"
    if output_stream:
        log_to_stream(output_stream, f"Plan name: {plan_name}")
        log_to_stream(output_stream, f"Plan Compartment: {plan_compartment_id}")
        log_to_stream(output_stream, f"Target Compartment (for VM): {target_compartment_id}")
        log_to_stream(output_stream, f"Target VCN: {vcn_ocid}")
        log_to_stream(output_stream, f"Target Subnet: {subnet_ocid}")

        # Log advanced configuration if provided
        if vm_config:
            log_to_stream(output_stream, "")
            log_to_stream(output_stream, "Advanced Configuration Summary:")
            batch_config = vm_config.get('batch_config', {})
            if batch_config:
                log_to_stream(output_stream, f"  Default Shape: {batch_config.get('shape', 'N/A')}")
                if batch_config.get('ocpus'):
                    log_to_stream(output_stream, f"  Default OCPUs: {batch_config.get('ocpus')}")
                if batch_config.get('memory_gb'):
                    log_to_stream(output_stream, f"  Default Memory: {batch_config.get('memory_gb')} GB")
                log_to_stream(output_stream, f"  Volume Performance: {batch_config.get('volume_performance', 'BALANCED')}")
                if batch_config.get('boot_volume_size_gb'):
                    log_to_stream(output_stream, f"  Boot Volume Size: {batch_config.get('boot_volume_size_gb')} GB")
                log_to_stream(output_stream, f"  Fault Domain: {batch_config.get('fault_domain', 'AUTO')}")
                log_to_stream(output_stream, f"  Assign Public IP: {batch_config.get('assign_public_ip', False)}")

            vm_overrides = vm_config.get('vm_overrides', {})
            if vm_overrides and vms_list:
                custom_vms = []
                for vm in vms_list:
                    vm_id = vm.get('inventory_asset_id')
                    if vm_id in vm_overrides and not vm_overrides[vm_id].get('use_batch', True):
                        custom_vms.append(vm.get('vm_name'))
                if custom_vms:
                    log_to_stream(output_stream, f"  VMs with custom configuration: {', '.join(custom_vms)}")

            log_to_stream(output_stream, "")
            log_to_stream(output_stream, "NOTE: OCM generates RMS stack automatically based on source VM discovery.")
            log_to_stream(output_stream, "      The configuration above will be logged but may require manual")
            log_to_stream(output_stream, "      adjustment in the generated RMS stack Terraform files.")
            log_to_stream(output_stream, "")

    start_time = time.time()

    try:
        migration_client = clients['migration']

        # Configure target environment
        target_env = VmTargetEnvironment(
            target_environment_type="VM_TARGET_ENV",
            vcn=vcn_ocid,
            subnet=subnet_ocid,
            target_compartment_id=target_compartment_id
        )

        # Configure resource assessment strategy
        strategy = AverageResourceAssessmentStrategy(
            resource_type="ALL",
            strategy_type="AVERAGE"
        )

        create_details = CreateMigrationPlanDetails(
            display_name=plan_name,
            compartment_id=plan_compartment_id,
            migration_id=project_ocid,
            target_environments=[target_env],
            strategies=[strategy]
        )

        if output_stream:
            log_to_stream(output_stream, "Creating migration plan...")

        # Create the plan
        create_response = migration_client.create_migration_plan(
            create_migration_plan_details=create_details,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        plan_ocid = create_response.data.id
        if output_stream:
            log_to_stream(output_stream, f"Plan created with OCID: {plan_ocid}")
            log_to_stream(output_stream, "Waiting for ACTIVE state (polling every 10 seconds)...")

        # Poll for ACTIVE state
        check_count = 0
        max_wait_time = 600  # 10 minutes timeout

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            if elapsed > max_wait_time:
                raise OCMMigrationError(f"Timeout waiting for plan to become ACTIVE (exceeded {max_wait_time}s)")

            # Get current plan status
            plan_response = migration_client.get_migration_plan(plan_ocid)
            current_state = plan_response.data.lifecycle_state

            if output_stream:
                log_to_stream(output_stream, f"Check #{check_count}: State = {current_state} | Elapsed: {format_elapsed_time(elapsed)}")

            if current_state == 'ACTIVE':
                break
            elif current_state in ['FAILED', 'DELETED']:
                raise OCMMigrationError(f"Plan entered {current_state} state")

            time.sleep(10)

        elapsed = time.time() - start_time

        if output_stream:
            log_to_stream(output_stream, f"SUCCESS: Migration plan created")
            log_to_stream(output_stream, f"  Plan OCID: {plan_ocid}")
            log_to_stream(output_stream, f"  Status: ACTIVE")
            log_to_stream(output_stream, f"  Elapsed time: {format_elapsed_time(elapsed)}")

        return plan_ocid

    except ServiceError as e:
        error_msg = f"Service error in Create Plan: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def step3_add_asset(clients, project_ocid, vm_name, inventory_asset_id, bucket_name, compartment_id, replication_schedule_id=None, output_stream=None):
    """
    Step 3: Add Asset to Project

    Args:
        clients: Dictionary of OCI clients
        project_ocid: OCID of the migration project
        vm_name: Name of the VM to migrate
        inventory_asset_id: Cloud Bridge inventory asset OCID
        bucket_name: Name of the replication bucket
        compartment_id: Target compartment OCID
        replication_schedule_id: Optional replication schedule OCID for warm migrations
        output_stream: Optional output stream for logging

    Returns:
        asset_ocid: OCID of the added migration asset
    """
    from oci_clients import get_oci_client

    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 3/6] Adding Asset to Project")
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, f"Adding VM: {vm_name}")
        log_to_stream(output_stream, f"Inventory Asset ID: {inventory_asset_id}")
        if replication_schedule_id:
            log_to_stream(output_stream, f"Replication Schedule: {replication_schedule_id}")

    start_time = time.time()

    try:
        migration_client = clients['migration']

        # Get availability domain
        identity_client = get_oci_client(oci.identity.IdentityClient)
        ads = identity_client.list_availability_domains(compartment_id=compartment_id)
        availability_domain = ads.data[0].name if ads.data else "KoMy:US-ASHBURN-AD-1"

        if output_stream:
            log_to_stream(output_stream, f"Using Availability Domain: {availability_domain}")

        # Build migration asset details
        asset_kwargs = {
            'migration_id': project_ocid,
            'display_name': vm_name,
            'inventory_asset_id': inventory_asset_id,
            'replication_compartment_id': compartment_id,
            'snap_shot_bucket_name': bucket_name,
            'availability_domain': availability_domain
        }

        # Add replication schedule if provided (for warm migrations)
        if replication_schedule_id:
            asset_kwargs['replication_schedule_id'] = replication_schedule_id

        create_asset_details = CreateMigrationAssetDetails(**asset_kwargs)

        if output_stream:
            log_to_stream(output_stream, "Creating migration asset...")

        # Create the migration asset
        create_response = migration_client.create_migration_asset(
            create_migration_asset_details=create_asset_details,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        asset_ocid = create_response.data.id
        if output_stream:
            log_to_stream(output_stream, f"Migration asset created with OCID: {asset_ocid}")
            log_to_stream(output_stream, "Waiting for ACTIVE state (polling every 5 seconds)...")

        # Poll for ACTIVE state
        check_count = 0
        max_wait_time = 300  # 5 minutes timeout

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            if elapsed > max_wait_time:
                raise OCMMigrationError(f"Timeout waiting for asset to become ACTIVE (exceeded {max_wait_time}s)")

            # Get current asset status
            asset_response = migration_client.get_migration_asset(asset_ocid)
            current_state = asset_response.data.lifecycle_state

            if output_stream:
                log_to_stream(output_stream, f"Check #{check_count}: State = {current_state} | Elapsed: {format_elapsed_time(elapsed)}")

            if current_state == 'ACTIVE':
                break
            elif current_state in ['FAILED', 'DELETED']:
                raise OCMMigrationError(f"Asset entered {current_state} state")

            time.sleep(5)

        elapsed = time.time() - start_time

        if output_stream:
            log_to_stream(output_stream, f"SUCCESS: Asset added to migration project")
            log_to_stream(output_stream, f"  Migration Asset OCID: {asset_ocid}")
            log_to_stream(output_stream, f"  Status: ACTIVE")
            log_to_stream(output_stream, f"  Elapsed time: {format_elapsed_time(elapsed)}")

        return asset_ocid

    except ServiceError as e:
        error_msg = f"Service error in Add Asset: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def _short_ocid(ocid):
    """Return the tail of an OCID for compact log display."""
    if not ocid:
        return '?'
    return '...' + ocid[-10:]


def _fetch_active_replication_work_request(migration_client, compartment_id, asset_ocid):
    """
    Return the most recent START_ASSET_REPLICATION work request for an asset,
    or None if none exists or the call fails.

    Uses the same API the OCI Console uses to populate the work requests panel,
    so the values returned here match the console exactly: operation_type,
    status, percent_complete, time_accepted / time_started / time_finished,
    and the work request OCID (the ocid1.ocmworkrequest.oc1... string).

    Returns a dict with normalized fields, or None on any error. Never raises.
    """
    try:
        resp = migration_client.list_work_requests(
            compartment_id=compartment_id,
            resource_id=asset_ocid,
            sort_by='timeAccepted',
            sort_order='DESC',
            limit=5,
        )
        items = resp.data.items or []
    except Exception:
        return None  # Defensive: fall back to get_replication_progress only

    # Prefer the most recent START_ASSET_REPLICATION. The list is already
    # sorted DESC by timeAccepted, so the first match is newest.
    for wr in items:
        if wr.operation_type == 'START_ASSET_REPLICATION':
            pc = wr.percent_complete if wr.percent_complete is not None else 0.0
            return {
                'id': wr.id,
                'operation_type': wr.operation_type,
                'status': wr.status,
                'percent_complete': pc,
                'time_accepted': wr.time_accepted,
                'time_started': wr.time_started,
                'time_finished': wr.time_finished,
            }
    return None


def step4_replicate_asset(clients, asset_ocid, poll_interval=30, output_stream=None):
    """
    Step 4: Replicate Migration Asset

    CRITICAL: This is the longest step and can take hours.

    2026-04-11 log overhaul: instead of printing OCM's
    `get_replication_progress` fields (whose `status` / `last_replication_status`
    values are opaque and whose `percentage` resets mid-cycle), this function
    now ALSO queries `list_work_requests` filtered to the migration asset and
    logs the **active work request's** state. That matches exactly what the
    OCI Console's Work Requests panel shows. When OCM transitions between
    internal phases it creates a new work request, and we log that as a
    "phase transition" so the 0% reset stops being mysterious.

    Termination detection is unchanged — it still uses
    `get_replication_progress.last_replication_status` and the asset's own
    lifecycle state. Those signals work and we aren't touching them.

    Args:
        clients: Dictionary of OCI clients
        asset_ocid: OCID of the migration asset
        poll_interval: Seconds between status checks
        output_stream: Optional output stream for logging

    Returns:
        replication_status: Status of replication completion
    """
    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 4/6] Replicating Migration Asset")
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "CRITICAL: This step can take HOURS depending on VM size")
        log_to_stream(output_stream, f"Asset OCID: {asset_ocid}")

    start_time = time.time()

    try:
        migration_client = clients['migration']

        # Fetch the asset's compartment once up front — list_work_requests
        # needs it. Also gives us a known-good target to validate termination
        # later via lifecycle_state.
        compartment_id = None
        try:
            asset_resp = migration_client.get_migration_asset(asset_ocid)
            compartment_id = getattr(asset_resp.data, 'compartment_id', None)
        except Exception as e:
            if output_stream:
                log_to_stream(output_stream, f"Warning: could not fetch asset compartment for WR lookup: {e}")

        # Start asset replication
        if output_stream:
            log_to_stream(output_stream, "Starting asset replication...")

        try:
            migration_client.start_asset_replication(
                migration_asset_id=asset_ocid,
                retry_strategy=DEFAULT_RETRY_STRATEGY
            )
            if output_stream:
                log_to_stream(output_stream, "Replication initiated. Beginning monitoring...")
                log_to_stream(output_stream, "(Work request progress mirrors the OCI Console's Work Requests panel.")
                log_to_stream(output_stream, " OCM replication has internal phases; a new work request at 0% is a phase transition, not a regression.)")
        except ServiceError as e:
            if e.status == 409 and "ongoing replication" in e.message.lower():
                if output_stream:
                    log_to_stream(output_stream, "Replication already in progress. Monitoring existing replication...")
            else:
                raise

        # Poll replication progress
        last_percentage = -1
        last_wr_id = None
        last_wr_log_key = None  # (wr_id, status, int(percent)) — dedupe ticks
        check_count = 0

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            try:
                # --- Termination signal (unchanged, still authoritative) ---
                progress_response = migration_client.get_replication_progress(
                    migration_asset_id=asset_ocid
                )
                progress_data = progress_response.data
                percentage = progress_data.percentage if hasattr(progress_data, 'percentage') else 0
                status = progress_data.status if hasattr(progress_data, 'status') else "UNKNOWN"
                last_repl_status = progress_data.last_replication_status if hasattr(progress_data, 'last_replication_status') else None

                # --- Display signal (new: matches the OCI Console) ---
                wr = None
                if compartment_id:
                    wr = _fetch_active_replication_work_request(
                        migration_client, compartment_id, asset_ocid
                    )

                if wr is not None:
                    # Phase transition: a new work request has appeared.
                    if last_wr_id is not None and wr['id'] != last_wr_id:
                        if output_stream:
                            log_to_stream(
                                output_stream,
                                f"--- Phase transition: new work request {_short_ocid(wr['id'])} "
                                f"({wr['operation_type']}) ---"
                            )
                    last_wr_id = wr['id']

                    # Only log when something the user cares about changed:
                    # WR id, status, or integer percent.
                    log_key = (wr['id'], wr['status'], int(wr['percent_complete']))
                    changed = log_key != last_wr_log_key
                    heartbeat = (check_count % 10 == 0)

                    if changed or heartbeat:
                        prefix = "Replicating asset" if changed else "Still replicating"
                        if output_stream:
                            log_to_stream(
                                output_stream,
                                f"{prefix} | {wr['status']} | {wr['percent_complete']:.0f}% | "
                                f"WR {_short_ocid(wr['id'])} | elapsed {format_elapsed_time(elapsed)}"
                            )
                        last_wr_log_key = log_key
                elif compartment_id:
                    # We have a compartment but no active WR — either OCM hasn't
                    # scheduled the next phase yet, or we're briefly between
                    # work requests. Log sparingly.
                    if check_count % 10 == 0 and output_stream:
                        log_to_stream(
                            output_stream,
                            f"Waiting for next replication work request... | "
                            f"elapsed {format_elapsed_time(elapsed)}"
                        )
                else:
                    # Fallback: no compartment means we can't list WRs.
                    # Fall back to the old format so we still have visibility.
                    if percentage != last_percentage:
                        if output_stream:
                            log_to_stream(
                                output_stream,
                                f"Replication Progress: {percentage}% | Status: {status} | "
                                f"Elapsed: {format_elapsed_time(elapsed)}"
                            )
                        last_percentage = percentage

                # --- Termination detection (unchanged) ---
                if last_repl_status == "COMPLETED":
                    if output_stream:
                        log_to_stream(output_stream, f"DETECTED: Replication status is COMPLETED")
                        log_to_stream(output_stream, f"SUCCESS: Asset replication completed")
                        log_to_stream(output_stream, f"  Final Status: {status}")
                        log_to_stream(output_stream, f"  Percentage: {percentage}%")
                        log_to_stream(output_stream, f"  Total elapsed time: {format_elapsed_time(elapsed)}")
                        output_stream.flush()
                    return "COMPLETED"
                elif last_repl_status == "FAILED":
                    error_msg = "Replication failed"
                    if hasattr(progress_data, 'last_replication_error'):
                        error_msg += f": {progress_data.last_replication_error}"
                    raise OCMMigrationError(error_msg)

                if status == "COMPLETED":
                    if output_stream:
                        log_to_stream(output_stream, f"SUCCESS: Asset replication completed")
                        log_to_stream(output_stream, f"  Total elapsed time: {format_elapsed_time(elapsed)}")
                    return "COMPLETED"

                # Belt-and-suspenders: reached 100% then reset to 0/NONE.
                if last_percentage == 100 and percentage == 0 and status == "NONE":
                    asset_response = migration_client.get_migration_asset(asset_ocid)
                    asset_state = asset_response.data.lifecycle_state
                    if asset_state == "ACTIVE":
                        if output_stream:
                            log_to_stream(output_stream, f"SUCCESS: Asset is ACTIVE, replication confirmed complete")
                            log_to_stream(output_stream, f"  Total elapsed time: {format_elapsed_time(elapsed)}")
                        return "COMPLETED"

                # Update last_percentage AFTER the belt-and-suspenders check
                # so the 100 -> 0 transition is detectable on the SAME tick it
                # happens. Previously this was updated inside the "changed"
                # branch above, which made the check effectively unreachable
                # once we switched to work-request-driven logging.
                last_percentage = percentage

            except ServiceError as e:
                # Don't fail on transient errors
                if e.status == 429:  # Rate limiting
                    if output_stream:
                        log_to_stream(output_stream, f"Rate limited, waiting {poll_interval}s before retry...")
                else:
                    if output_stream:
                        log_to_stream(output_stream, f"Warning: Error checking progress: {e.message}")

            # Wait before next poll
            time.sleep(poll_interval)

    except ServiceError as e:
        error_msg = f"Service error in Replicate Asset: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def _fetch_latest_execute_plan_work_request(migration_client, compartment_id, plan_ocid):
    """
    Return the most recent EXECUTE_MIGRATION_PLAN work request for a plan,
    or None. Same shape as _fetch_active_replication_work_request. Never raises.
    """
    try:
        resp = migration_client.list_work_requests(
            compartment_id=compartment_id,
            resource_id=plan_ocid,
            sort_by='timeAccepted',
            sort_order='DESC',
            limit=5,
        )
        items = resp.data.items or []
    except Exception:
        return None
    for wr in items:
        if wr.operation_type == 'EXECUTE_MIGRATION_PLAN':
            pc = wr.percent_complete if wr.percent_complete is not None else 0.0
            return {
                'id': wr.id,
                'operation_type': wr.operation_type,
                'status': wr.status,
                'percent_complete': pc,
                'time_accepted': wr.time_accepted,
                'time_started': wr.time_started,
                'time_finished': wr.time_finished,
            }
    return None


def step5_generate_rms_stack(clients, plan_ocid, output_stream=None):
    """
    Step 5: Generate Resource Manager Stack

    2026-04-11 log overhaul: polls OCM's list_work_requests for the
    EXECUTE_MIGRATION_PLAN work request (the same thing the OCI Console's
    Work Requests panel shows) and logs its status + percent_complete.
    Termination detection is unchanged — it still uses the authoritative
    `plan.time_updated` bump + reference_to_rms_stack check.

    Args:
        clients: Dictionary of OCI clients
        plan_ocid: OCID of the migration plan
        output_stream: Optional output stream for logging

    Returns:
        rms_stack_ocid: OCID of the generated RMS stack
    """
    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 5/6] Generating Resource Manager Stack")
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, f"Migration Plan: {plan_ocid}")

    start_time = time.time()

    try:
        migration_client = clients['migration']

        # Capture plan state BEFORE executing so we can detect when THIS
        # execution finishes (vs. seeing leftover state from a prior one).
        # plan.time_updated bumps whenever OCM finishes processing an execution
        # and is the most reliable signal: lifecycle_state often stays ACTIVE
        # the whole time, so we can't rely on UPDATING transitions.
        prior_ref = None
        prior_time_updated = None
        compartment_id = None
        try:
            prior_plan = migration_client.get_migration_plan(plan_ocid)
            prior_ref = getattr(prior_plan.data, 'reference_to_rms_stack', None)
            prior_time_updated = getattr(prior_plan.data, 'time_updated', None)
            compartment_id = getattr(prior_plan.data, 'compartment_id', None)
        except Exception:
            pass

        if output_stream:
            if prior_ref:
                log_to_stream(output_stream, f"Plan currently references stack: {prior_ref}")
                log_to_stream(output_stream, "Re-executing migration plan to hydrate fresh volumes...")
            else:
                log_to_stream(output_stream, "Executing migration plan to generate RMS stack...")

        # Always re-execute. Each execution hydrates fresh boot/block volumes
        # from the latest replicated data; the stack from a prior execution
        # references volumes that no longer exist. Both test deploys and real
        # cutovers MUST re-execute or the APPLY job will fail with 409 Conflict
        # on volume references.
        migration_client.execute_migration_plan(
            migration_plan_id=plan_ocid,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        if output_stream:
            log_to_stream(output_stream, "Execution initiated, waiting for the plan to finish hydrating volumes (polling every 10 seconds)...")

        # Poll until plan.time_updated bumps past prior_time_updated AND
        # reference_to_rms_stack is set. The bump is the unambiguous signal
        # that THIS execution completed (not a leftover ref from before).
        check_count = 0
        max_wait_time = 1800  # 30 minutes timeout - OCM stack generation can be slow
        rms_stack_ocid = None
        last_wr_log_key = None

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            if elapsed > max_wait_time:
                raise OCMMigrationError(f"Timeout waiting for RMS stack generation (exceeded {max_wait_time}s)")

            plan_response = migration_client.get_migration_plan(plan_ocid)
            current_state = plan_response.data.lifecycle_state
            current_ref = plan_response.data.reference_to_rms_stack if hasattr(plan_response.data, 'reference_to_rms_stack') else None
            current_time_updated = getattr(plan_response.data, 'time_updated', None)
            lifecycle_details = getattr(plan_response.data, 'lifecycle_details', None) or ''

            # Check terminal failure states
            if current_state in ['FAILED', 'DELETED', 'NEEDS_ATTENTION']:
                raise OCMMigrationError(f"Plan execution entered {current_state} state: {lifecycle_details}")

            # Success criteria:
            #   - plan has a stack ref, AND
            #   - plan.time_updated has bumped past prior_time_updated
            #     (or there was no prior_time_updated at all)
            time_bumped = (
                prior_time_updated is None
                or (current_time_updated is not None and current_time_updated != prior_time_updated)
            )
            execution_complete = current_ref is not None and time_bumped and current_state == 'ACTIVE'

            if execution_complete:
                rms_stack_ocid = current_ref
                if output_stream:
                    log_to_stream(output_stream, f"Check #{check_count}: State = ACTIVE | RMS Stack = {rms_stack_ocid} | Elapsed: {format_elapsed_time(elapsed)}")
                break

            # Work request overlay — console-matching view.
            wr = None
            if compartment_id:
                wr = _fetch_latest_execute_plan_work_request(
                    migration_client, compartment_id, plan_ocid
                )

            if wr is not None:
                log_key = (wr['id'], wr['status'], int(wr['percent_complete']))
                if log_key != last_wr_log_key and output_stream:
                    log_to_stream(
                        output_stream,
                        f"Executing migration plan | {wr['status']} | {wr['percent_complete']:.0f}% | "
                        f"WR {_short_ocid(wr['id'])} | elapsed {format_elapsed_time(elapsed)}"
                    )
                    last_wr_log_key = log_key
                # Heartbeat every 10 checks even if WR state unchanged.
                elif check_count % 10 == 0 and output_stream:
                    log_to_stream(
                        output_stream,
                        f"Still executing plan | {wr['status']} | {wr['percent_complete']:.0f}% | "
                        f"WR {_short_ocid(wr['id'])} | elapsed {format_elapsed_time(elapsed)}"
                    )
            else:
                # Fall back to the legacy plan-state line if WR lookup
                # failed or compartment couldn't be resolved.
                if output_stream:
                    detail_suffix = f" | Details: {lifecycle_details}" if lifecycle_details else ''
                    ref_status = 'GENERATED' if current_ref else 'pending'
                    bump_status = 'bumped' if time_bumped else 'unchanged'
                    log_to_stream(output_stream, f"Check #{check_count}: State = {current_state} | Stack ref = {ref_status} | time_updated = {bump_status} | Elapsed: {format_elapsed_time(elapsed)}{detail_suffix}")

            time.sleep(10)

        elapsed = time.time() - start_time

        if not rms_stack_ocid:
            raise OCMMigrationError("RMS stack was not generated")

        if output_stream:
            log_to_stream(output_stream, f"SUCCESS: RMS stack generated")
            log_to_stream(output_stream, f"  RMS Stack OCID: {rms_stack_ocid}")
            log_to_stream(output_stream, f"  Plan Status: {current_state}")
            log_to_stream(output_stream, f"  Elapsed time: {format_elapsed_time(elapsed)}")
            log_to_stream(output_stream, "")
            log_to_stream(output_stream, "IMPORTANT: To customize VM shape, size, or other compute configurations:")
            log_to_stream(output_stream, "  1. Go to OCI Console > Developer Services > Resource Manager")
            log_to_stream(output_stream, "  2. Find the generated stack (OCID above)")
            log_to_stream(output_stream, "  3. Edit the stack and download/modify the Terraform configuration")
            log_to_stream(output_stream, "  4. Update instance shape, OCPU count, memory, etc. in the .tf files")
            log_to_stream(output_stream, "  5. Re-upload the modified configuration before deploying")

        return rms_stack_ocid

    except ServiceError as e:
        error_msg = f"Service error in Generate RMS Stack: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def step6_deploy_rms_stack(clients, rms_stack_ocid, poll_interval=30, output_stream=None, on_job_created=None):
    """
    Step 6: Deploy Resource Manager Stack

    Args:
        clients: Dictionary of OCI clients
        rms_stack_ocid: OCID of the RMS stack
        poll_interval: Seconds between status checks
        output_stream: Optional output stream for logging
        on_job_created: Optional callback invoked with the APPLY job OCID as
            soon as create_job returns, BEFORE polling starts. Lets callers
            persist the job id early so a mid-poll exception does not lose
            track of an in-flight job (which blocks cleanup).

    Returns:
        job_id: OCID of the deployment job
    """
    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "[Step 6/6] Deploying Resource Manager Stack")
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, f"RMS Stack: {rms_stack_ocid}")
        log_to_stream(output_stream, "NOTE: This typically takes 5-15 minutes...")

    start_time = time.time()

    try:
        rm_client = clients['resource_manager']

        # Create apply job
        if output_stream:
            log_to_stream(output_stream, "Creating apply job with auto-approval...")

        create_job_details = CreateJobDetails(
            stack_id=rms_stack_ocid,
            operation="APPLY",
            apply_job_plan_resolution=ApplyJobPlanResolution(
                is_auto_approved=True
            )
        )

        job_response = rm_client.create_job(
            create_job_details=create_job_details,
            retry_strategy=DEFAULT_RETRY_STRATEGY
        )

        job_id = job_response.data.id
        if output_stream:
            log_to_stream(output_stream, f"Apply job created: {job_id}")
            log_to_stream(output_stream, "Monitoring deployment progress...")

        # Notify caller the job exists BEFORE we start polling, so they can
        # persist it. A subsequent exception in the poll loop will not lose
        # track of the job.
        if on_job_created is not None:
            try:
                on_job_created(job_id)
            except Exception as e:
                if output_stream:
                    log_to_stream(output_stream, f"Warning: on_job_created callback raised: {e}")

        # Monitor deployment progress.
        # 2026-04-11 log overhaul: in addition to the lifecycle_state poll,
        # this loop now tails `get_job_logs(job_id)` and streams new Terraform
        # output lines into our log buffer. That mirrors what the OCI Console's
        # Job Logs tab shows: per-resource "Creating...", "Creation complete
        # after 1m30s", etc. Lets the user see forward progress during APPLY
        # instead of a multi-minute black box. Bug fix: the old code called
        # rm_client.list_job_logs on failure — that method doesn't exist on
        # ResourceManagerClient; the correct name is get_job_logs.
        last_status = None
        check_count = 0
        streamed_log_count = 0

        def _tail_job_logs(max_new=40):
            """
            Fetch job logs, stream any new entries since the last tick,
            return the updated counter. Silently tolerant of SDK errors.
            """
            nonlocal streamed_log_count
            try:
                logs_resp = rm_client.get_job_logs(job_id=job_id, limit=500)
                entries = list(logs_resp.data or [])
            except Exception:
                return
            if len(entries) <= streamed_log_count:
                return
            new_entries = entries[streamed_log_count:]
            # Cap per-tick output so a chatty Terraform run can't flood the log.
            if len(new_entries) > max_new:
                dropped = len(new_entries) - max_new
                new_entries = new_entries[:max_new]
                tail_note = f"  [tf] ...({dropped} more lines suppressed this tick)"
            else:
                tail_note = None
            for e in new_entries:
                msg = (getattr(e, 'message', '') or '').strip()
                if not msg:
                    continue
                # Trim to keep individual lines short — the full logs are
                # always available via the OCI Console if needed.
                if len(msg) > 240:
                    msg = msg[:237] + '...'
                if output_stream:
                    log_to_stream(output_stream, f"  [tf] {msg}")
            if tail_note and output_stream:
                log_to_stream(output_stream, tail_note)
            streamed_log_count = len(entries)

        while True:
            check_count += 1
            elapsed = time.time() - start_time

            job = rm_client.get_job(job_id)
            current_status = job.data.lifecycle_state

            # Log status changes
            if current_status != last_status:
                if output_stream:
                    log_to_stream(output_stream, f"Deployment Status: {current_status} | Elapsed: {format_elapsed_time(elapsed)}")
                last_status = current_status
            elif check_count % 10 == 0:  # Heartbeat every 10 checks
                if output_stream:
                    log_to_stream(output_stream, f"Still deploying: {current_status} | Elapsed: {format_elapsed_time(elapsed)}")

            # Live-tail Terraform output. Done on every tick so the user sees
            # per-resource creation lines streaming in as APPLY runs.
            _tail_job_logs()

            # Check terminal states
            if current_status == "SUCCEEDED":
                # Flush any trailing log lines before declaring success.
                _tail_job_logs(max_new=200)
                if output_stream:
                    log_to_stream(output_stream, f"SUCCESS: RMS stack deployed successfully")
                    log_to_stream(output_stream, f"  Job ID: {job_id}")
                    log_to_stream(output_stream, f"  Final Status: {current_status}")
                    log_to_stream(output_stream, f"  Total elapsed time: {format_elapsed_time(elapsed)}")
                return job_id
            elif current_status == "FAILED":
                error_msg = "Deployment failed"
                # Flush the final log lines so the failure reason is visible
                # in migration.logs, not just in the OCI Console.
                _tail_job_logs(max_new=200)
                try:
                    logs_resp = rm_client.get_job_logs(job_id=job_id, limit=500)
                    all_entries = list(logs_resp.data or [])
                    if all_entries and output_stream:
                        log_to_stream(output_stream, "Last job log lines:")
                        for log_entry in all_entries[-10:]:
                            msg = (getattr(log_entry, 'message', '') or '').strip()
                            if msg:
                                log_to_stream(output_stream, f"  {msg[:240]}")
                except Exception:
                    pass
                raise OCMMigrationError(error_msg)
            elif current_status == "CANCELED":
                raise OCMMigrationError("Deployment was canceled")

            # Wait before next check
            time.sleep(poll_interval)

    except ServiceError as e:
        error_msg = f"Service error in Deploy RMS Stack: {e.code} - {e.message}"
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {error_msg}")
        raise OCMMigrationError(error_msg)
    except Exception as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: {str(e)}")
        raise


def validate_stack_boot_volumes(clients, rms_stack_ocid, output_stream=None):
    """
    Pre-flight check: download the RMS stack's Terraform, find every
    `oci_core_instance.source_details.source_id` that references an existing
    boot volume, and confirm each one is in AVAILABLE state.

    Why: OCM caches the hydrated boot volume reference at the target-asset
    level. If that volume has been terminated (e.g., by an older Terraform
    DESTROY job), `execute_migration_plan` does NOT re-hydrate on repeat
    calls — it just returns Terraform pointing at the dead volume. A
    subsequent APPLY then wastes ~3 minutes creating an instance that
    ultimately fails with 409 Conflict on LaunchInstance. This helper
    catches that in seconds and fails with an actionable message.

    Raises OCMMigrationError if any referenced boot volume is not AVAILABLE.
    """
    import io
    import json as jsonlib
    import zipfile

    rm_client = clients['resource_manager']
    bs_client = clients.get('blockstorage')
    if bs_client is None:
        # init_clients may not always provide this; create lazily.
        from oci_clients import get_oci_client
        bs_client = get_oci_client(oci.core.BlockstorageClient)

    if output_stream:
        log_to_stream(output_stream, "Pre-flight: checking hydrated boot volumes referenced by the stack...")

    resp = rm_client.get_stack_tf_config(stack_id=rms_stack_ocid)
    zb = resp.data.content.read() if hasattr(resp.data.content, 'read') else resp.data.content
    zf = zipfile.ZipFile(io.BytesIO(zb))

    refs = []  # (resource_name, boot_volume_ocid)
    for name in zf.namelist():
        if not name.endswith('.tf.json'):
            continue
        try:
            doc = jsonlib.loads(zf.read(name).decode('utf-8'))
        except Exception:
            continue
        resources = doc.get('resource', {}) if isinstance(doc, dict) else {}
        instances = resources.get('oci_core_instance', {}) if isinstance(resources, dict) else {}
        if not isinstance(instances, dict):
            continue
        for inst_name, inst_body in instances.items():
            if not isinstance(inst_body, dict):
                continue
            sd = inst_body.get('source_details', {})
            if isinstance(sd, dict) and sd.get('source_type') == 'bootVolume':
                bv_ocid = sd.get('source_id')
                if bv_ocid:
                    refs.append((inst_name, bv_ocid))

    if not refs:
        if output_stream:
            log_to_stream(output_stream, "  No bootVolume-sourced instances in this stack - skipping check.")
        return

    bad = []
    for inst_name, bv_ocid in refs:
        try:
            bv = bs_client.get_boot_volume(boot_volume_id=bv_ocid).data
            state = bv.lifecycle_state
        except ServiceError as e:
            if e.status == 404:
                state = 'NOT_FOUND'
            else:
                if output_stream:
                    log_to_stream(output_stream, f"  WARNING: could not check {bv_ocid}: {e.code} - {e.message}")
                continue
        if output_stream:
            log_to_stream(output_stream, f"  {inst_name}: boot volume state = {state}")
        if state not in ('AVAILABLE',):
            bad.append((inst_name, bv_ocid, state))

    if bad:
        details = "; ".join(f"{n}: {bv} is {state}" for n, bv, state in bad)
        raise OCMMigrationError(
            "Stack references hydrated boot volumes that are not AVAILABLE ("
            + details + "). This usually means an earlier cleanup terminated the volume. "
            "Click Sync Now to run a fresh replication cycle and produce a new hydrated volume, then retry."
        )

    if output_stream:
        log_to_stream(output_stream, "  All referenced boot volumes are AVAILABLE. Proceeding with APPLY.")


def terminate_test_instances(clients, test_apply_job_id, poll_interval=10, output_stream=None):
    """
    Cleanly remove a test VM without going through Terraform DESTROY.

    Why: Terraform DESTROY on oci_core_instance defaults to preserve_boot_volume=false,
    which terminates the OCM-hydrated boot volume. OCM does NOT re-hydrate on repeat
    execute_migration_plan calls against the same plan (observed: ~20s no-op returning
    the same dead stack). Any subsequent APPLY fails with 409 Conflict on volumes.

    Instead: list the instance OCIDs produced by the test APPLY (via RMS job outputs),
    call TerminateInstance(preserve_boot_volume=True) directly on each. The stack,
    its Terraform, and the hydrated boot volume all survive. The next APPLY on the
    stack refreshes state, detects the missing instance, and relaunches a new one
    against the preserved boot volume.

    Returns a list of (instance_ocid, final_state) tuples.
    """
    rm_client = clients['resource_manager']
    compute_client = clients['compute']

    if output_stream:
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, "Terminating Test VM Instances (preserving boot volumes)")
        log_to_stream(output_stream, "=" * 80)
        log_to_stream(output_stream, f"Source APPLY job: {test_apply_job_id}")

    # 1) Collect instance OCIDs from the APPLY job outputs.
    try:
        outputs_resp = rm_client.list_job_outputs(job_id=test_apply_job_id)
    except ServiceError as e:
        if output_stream:
            log_to_stream(output_stream, f"ERROR: could not list job outputs: {e.code} - {e.message}")
        raise OCMMigrationError(f"Failed to list APPLY job outputs: {e.message}")

    instance_ocids = []
    for item in outputs_resp.data.items:
        value = getattr(item, 'output_value', None)
        if isinstance(value, str) and value.startswith('ocid1.instance.'):
            instance_ocids.append(value)

    if output_stream:
        log_to_stream(output_stream, f"Found {len(instance_ocids)} instance(s) to terminate:")
        for ocid in instance_ocids:
            log_to_stream(output_stream, f"  - {ocid}")

    if not instance_ocids:
        if output_stream:
            log_to_stream(output_stream, "No instance outputs found; nothing to terminate.")
        return []

    # 2) Terminate each instance, preserving its boot volume.
    results = []
    for ocid in instance_ocids:
        if output_stream:
            log_to_stream(output_stream, f"\nTerminating {ocid} (preserve_boot_volume=True)...")
        try:
            compute_client.terminate_instance(
                instance_id=ocid,
                preserve_boot_volume=True
            )
        except ServiceError as e:
            if e.status == 404:
                if output_stream:
                    log_to_stream(output_stream, f"  Instance already gone (404); counting as terminated.")
                results.append((ocid, 'TERMINATED'))
                continue
            if output_stream:
                log_to_stream(output_stream, f"  ERROR: {e.code} - {e.message}")
            raise OCMMigrationError(f"terminate_instance failed for {ocid}: {e.message}")

        # 3) Poll until the instance reaches TERMINATED.
        start = time.time()
        max_wait = 600  # 10 minutes per instance
        last_state = None
        while True:
            elapsed = time.time() - start
            if elapsed > max_wait:
                raise OCMMigrationError(
                    f"Timeout waiting for {ocid} to terminate (exceeded {max_wait}s)"
                )
            try:
                inst = compute_client.get_instance(instance_id=ocid).data
                state = inst.lifecycle_state
            except ServiceError as e:
                if e.status == 404:
                    state = 'TERMINATED'
                else:
                    raise

            if state != last_state:
                if output_stream:
                    log_to_stream(output_stream, f"  State: {state} | Elapsed: {format_elapsed_time(elapsed)}")
                last_state = state

            if state == 'TERMINATED':
                results.append((ocid, state))
                break

            time.sleep(poll_interval)

    if output_stream:
        log_to_stream(output_stream, f"\nSUCCESS: {len(results)} instance(s) terminated; boot volumes preserved.")
    return results


