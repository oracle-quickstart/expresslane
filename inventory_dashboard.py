#!/usr/bin/env python3
# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane

Pre-Flight Inventory Dashboard Module (v3 - Sales Intelligence Edition)

This module provides functions to fetch and aggregate discovered assets
from Oracle Cloud Migrations (OCM) Cloud Bridge for pre-migration sizing.
Designed for Sales and Services teams to quickly size customer environments,
estimate costs, and identify migration risks before a project starts.

v3 Enhancements:
- Complexity Scoring with heatmap visualization
- "Zombie" VM Detection for cost savings opportunities
- License Upsell Opportunity flagging (Windows/Oracle)
- Enhanced Bill of Materials for deal pricing
- Risk assessment for migration planning
"""

import oci
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime
from collections import Counter
from enum import Enum
from config import config

# Presales Sizing Engine
from migration_sizer import MigrationSizer
from asset_specs_extractor import get_aws_specs_from_instance_type, AWS_INSTANCE_SPECS


class ComplexityLevel(Enum):
    """Complexity level for migration assessment."""
    STANDARD = "standard"  # Score 1-2: Green
    MODERATE = "moderate"  # Score 3-5: Yellow
    COMPLEX = "complex"    # Score 6+: Red


@dataclass
class InventoryAsset:
    """Represents a discovered VM asset for the inventory dashboard."""
    # Core identification
    asset_id: str
    hostname: str

    # Compute specs
    vcpu_count: int
    memory_gb: float
    memory_mb: int
    storage_gb: float

    # Infrastructure details
    instance_type: str
    architecture: str
    disk_count: int
    os_type: str
    source_type: str  # VMware, AWS, etc.
    power_state: str
    primary_ip: str

    # Metadata
    specs_from_lookup: bool

    # Sales Intelligence Fields
    complexity_score: int
    complexity_level: str  # "standard", "moderate", "complex"
    is_zombie: bool  # Stopped/PoweredOff VM = potential savings
    has_license_opportunity: bool  # Windows/Oracle = license optimization
    license_type: str  # "Windows", "Oracle", "Linux", "Other"
    intelligence_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class InstanceTypeDistribution:
    """Distribution of assets by instance type for cost analysis."""
    instance_type: str
    count: int
    total_vcpus: int
    total_memory_gb: float
    total_storage_gb: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class ComplexityDistribution:
    """Distribution of assets by complexity level."""
    standard_count: int  # Green: Score 1-2
    moderate_count: int  # Yellow: Score 3-5
    complex_count: int   # Red: Score 6+
    standard_percentage: float
    moderate_percentage: float
    complex_percentage: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class InventorySummary:
    """Summary statistics for the inventory dashboard."""
    # Core metrics
    total_vms: int
    total_vcpus: int
    total_ram_gb: float
    total_ram_tb: float
    total_storage_gb: float
    total_storage_tb: float
    total_disks: int

    # Source breakdown
    vmware_count: int
    aws_count: int
    other_count: int

    # Power state
    powered_on_count: int
    powered_off_count: int

    # Complexity metrics
    multi_disk_vms: int
    complexity_distribution: Dict[str, Any] = field(default_factory=dict)

    # Sales Intelligence metrics
    zombie_count: int = 0
    zombie_potential_savings_vcpus: int = 0
    zombie_potential_savings_ram_gb: float = 0
    license_opportunity_count: int = 0  # Commercial licenses (Windows + RHEL)
    windows_count: int = 0
    rhel_count: int = 0  # Red Hat Enterprise Linux (paid)
    linux_count: int = 0  # Free Linux (Oracle Linux, Ubuntu, CentOS, etc.)

    # Distributions
    instance_type_distribution: List[Dict[str, Any]] = field(default_factory=list)
    architecture_distribution: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


def calculate_complexity_score(asset_data: Dict[str, Any]) -> Tuple[int, str, List[str]]:
    """
    Calculate complexity score for migration assessment.

    Scoring Logic:
    - Base score: 1
    - +5 if disk_count > 1 (multi-disk complexity)
    - +2 if architecture is not x86_64 (ARM/other compatibility)
    - +1 if has Windows license (commercial licensing)
    - +1 if has RHEL license (commercial licensing)
    - +2 if storage > 500GB (large data migration)
    - +1 if memory > 64GB (large instance sizing)

    Note: Oracle Linux is FREE on OCI, so it does not add complexity.

    Returns:
        Tuple of (score, level, reasons)
    """
    score = 1  # Base score
    reasons = []

    # Multi-disk complexity (+5)
    disk_count = asset_data.get('disk_count', 1)
    if disk_count > 1:
        score += 5
        reasons.append(f"Multi-disk ({disk_count} disks)")

    # Architecture compatibility (+2)
    architecture = asset_data.get('architecture', 'x86_64').lower()
    if 'x86' not in architecture and architecture != 'unknown':
        score += 2
        reasons.append(f"Non-x86 architecture ({architecture})")

    # License complexity (commercial licenses add complexity)
    os_type = asset_data.get('os_type', '').lower()
    if 'windows' in os_type:
        score += 1
        reasons.append("Windows licensing")
    if ('red hat' in os_type or 'rhel' in os_type) and 'oracle' not in os_type:
        score += 1
        reasons.append("RHEL licensing")

    # Large storage (+2 if > 500GB)
    storage_gb = asset_data.get('storage_gb', 0)
    if storage_gb > 500:
        score += 2
        reasons.append(f"Large storage ({storage_gb:.0f} GB)")

    # Large memory (+1 if > 64GB)
    memory_gb = asset_data.get('memory_gb', 0)
    if memory_gb > 64:
        score += 1
        reasons.append(f"Large memory ({memory_gb:.0f} GB)")

    # Determine level
    if score <= 2:
        level = ComplexityLevel.STANDARD.value
    elif score <= 5:
        level = ComplexityLevel.MODERATE.value
    else:
        level = ComplexityLevel.COMPLEX.value

    return score, level, reasons


def detect_zombie_vm(power_state: str) -> bool:
    """
    Detect if a VM is a "zombie" (stopped/powered off).
    These represent potential cost savings opportunities.
    """
    if not power_state:
        return False

    zombie_states = [
        'stopped', 'poweroff', 'poweredoff', 'powered off', 'powered_off',
        'deallocated', 'shutdown', 'suspended', 'terminated', 'off'
    ]

    return power_state.lower().strip() in zombie_states


def detect_license_opportunity(os_type: str) -> Tuple[bool, str]:
    """
    Detect if a VM has commercial license costs.
    Windows and Red Hat (RHEL) require paid licenses.
    Oracle Linux is FREE on OCI and should NOT be counted.

    Returns:
        Tuple of (has_commercial_license, license_type)
    """
    if not os_type:
        return False, "Unknown"

    os_lower = os_type.lower()

    # Windows - commercial license
    if 'windows' in os_lower:
        return True, "Windows"
    # Red Hat Enterprise Linux - commercial license
    # Check for RHEL but exclude Oracle Linux
    elif ('red hat' in os_lower or 'rhel' in os_lower) and 'oracle' not in os_lower:
        return True, "RHEL"
    # Oracle Linux is FREE on OCI - do not count as commercial
    elif 'oracle' in os_lower:
        return False, "Oracle Linux"
    # Other free Linux distributions
    elif any(linux in os_lower for linux in ['linux', 'ubuntu', 'centos', 'debian', 'suse', 'amazon', 'alma', 'rocky']):
        return False, "Linux"
    else:
        return False, "Other"


def init_oci_clients() -> Dict[str, Any]:
    """
    Initialize OCI clients for inventory queries.

    Returns:
        Dictionary containing initialized OCI clients
    """
    from oci_clients import get_oci_client, get_oci_config

    inventory_client = get_oci_client(oci.cloud_bridge.InventoryClient)
    discovery_client = get_oci_client(oci.cloud_bridge.DiscoveryClient)

    return {
        'inventory': inventory_client,
        'discovery': discovery_client,
        'config': get_oci_config()
    }


@dataclass
class AssetSourceInfo:
    """Information about a connected asset source (bridge)."""
    source_id: str
    name: str
    source_type: str  # VMWARE, AWS, etc.
    lifecycle_state: str
    asset_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'name': self.name,
            'source_type': self.source_type,
            'lifecycle_state': self.lifecycle_state,
            'asset_count': self.asset_count
        }


def list_all_asset_sources(compartment_id: Optional[str] = None) -> List[AssetSourceInfo]:
    """
    Get ALL connected asset sources (bridges) in the compartment.
    This includes VMware vCenter connectors, AWS Discovery agents, etc.

    Returns:
        List of AssetSourceInfo objects for each active bridge
    """
    clients = init_oci_clients()
    discovery_client = clients['discovery']

    if not compartment_id:
        compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

    if not compartment_id:
        raise ValueError("Compartment ID is required. Set OCM_TARGET_COMPARTMENT_OCID in config.")

    sources = []
    next_page = None

    while True:
        kwargs = {
            'compartment_id': compartment_id,
            'lifecycle_state': 'ACTIVE',
            'limit': 100
        }
        if next_page:
            kwargs['page'] = next_page

        try:
            response = discovery_client.list_asset_sources(**kwargs)

            for source in response.data.items:
                source_type = getattr(source, 'type', 'UNKNOWN')
                # Normalize source type for display
                if source_type == 'VMWARE':
                    display_type = 'VMware'
                elif source_type == 'AWS':
                    display_type = 'AWS'
                else:
                    display_type = source_type

                sources.append(AssetSourceInfo(
                    source_id=source.id,
                    name=getattr(source, 'display_name', 'Unknown'),
                    source_type=display_type,
                    lifecycle_state=getattr(source, 'lifecycle_state', 'UNKNOWN')
                ))

            if hasattr(response, 'next_page') and response.next_page:
                next_page = response.next_page
            else:
                break

        except Exception as e:
            print(f"Warning: Error listing asset sources: {e}")
            break

    # If no sources found via discovery API, fall back to config
    if not sources:
        # Check for configured asset sources
        primary_source = config.get('OCM_ASSET_SOURCE_OCID')
        secondary_source = config.get('OCM_ASSET_SOURCE_OCID_2') or config.get('AWS_ASSET_SOURCE_OCID')

        if primary_source:
            sources.append(AssetSourceInfo(
                source_id=primary_source,
                name='Primary Source',
                source_type='VMware',
                lifecycle_state='ACTIVE'
            ))
        if secondary_source:
            sources.append(AssetSourceInfo(
                source_id=secondary_source,
                name='Secondary Source',
                source_type='AWS',
                lifecycle_state='ACTIVE'
            ))

    print(f"[Multi-Bridge] Found {len(sources)} active asset source(s)")
    for src in sources:
        print(f"  - {src.name} ({src.source_type}): {src.source_id[:20]}...")

    return sources


def fetch_all_inventory(compartment_id: Optional[str] = None) -> Tuple[List[InventoryAsset], List[AssetSourceInfo]]:
    """
    Fetch inventory from ALL connected bridges and merge into master list.

    This is the main entry point for multi-bridge aggregation.
    It automatically discovers all asset sources and fetches from each.

    Returns:
        Tuple of (merged_assets, source_info_list)
    """
    if not compartment_id:
        compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

    # Get all connected asset sources
    sources = list_all_asset_sources(compartment_id)

    if not sources:
        print("[Multi-Bridge] No asset sources found. Fetching all assets without filtering...")
        # Fall back to fetching all assets without source filtering
        assets = fetch_inventory_assets(compartment_id, asset_source_id=None)
        return assets, []

    # Aggregate assets from all sources
    all_assets: List[InventoryAsset] = []
    seen_asset_ids = set()  # Prevent duplicates

    for source in sources:
        print(f"[Multi-Bridge] Fetching from: {source.name} ({source.source_type})...")
        try:
            source_assets = fetch_inventory_assets(
                compartment_id=compartment_id,
                asset_source_id=source.source_id
            )

            # Add unique assets only
            new_count = 0
            for asset in source_assets:
                if asset.asset_id not in seen_asset_ids:
                    seen_asset_ids.add(asset.asset_id)
                    all_assets.append(asset)
                    new_count += 1

            source.asset_count = new_count
            print(f"  -> Found {new_count} unique assets")

        except Exception as e:
            print(f"  -> Error: {e}")
            continue

    print(f"[Multi-Bridge] Total aggregated assets: {len(all_assets)}")
    return all_assets, sources


def get_asset_detailed_info(inventory_client, asset_id: str) -> Tuple[float, int, str]:
    """
    Get detailed asset information including storage, disk count, and architecture.

    Args:
        inventory_client: OCI Cloud Bridge InventoryClient
        asset_id: Asset OCID

    Returns:
        Tuple of (total_storage_gb, disk_count, architecture)
    """
    total_storage_gb = 0.0
    disk_count = 0
    architecture = 'Unknown'

    try:
        full_asset = inventory_client.get_asset(asset_id).data

        # Check for VM properties with disks
        if hasattr(full_asset, 'vm') and full_asset.vm:
            vm_props = full_asset.vm
            if hasattr(vm_props, 'disks') and vm_props.disks:
                disk_count = len(vm_props.disks)
                for disk in vm_props.disks:
                    if hasattr(disk, 'size_in_mbs') and disk.size_in_mbs:
                        total_storage_gb += disk.size_in_mbs / 1024
                    elif hasattr(disk, 'size_in_gbs') and disk.size_in_gbs:
                        total_storage_gb += disk.size_in_gbs

        # Check compute properties
        if hasattr(full_asset, 'compute') and full_asset.compute:
            compute = full_asset.compute

            if hasattr(compute, 'hardware_version') and compute.hardware_version:
                hw_version = str(compute.hardware_version).lower()
                if 'arm' in hw_version:
                    architecture = 'arm64'
                else:
                    architecture = 'x86_64'

            if disk_count == 0 and hasattr(compute, 'disks') and compute.disks:
                disk_count = len(compute.disks)
                for disk in compute.disks:
                    if hasattr(disk, 'size_in_mbs') and disk.size_in_mbs:
                        total_storage_gb += disk.size_in_mbs / 1024
                    elif hasattr(disk, 'size_in_gbs') and disk.size_in_gbs:
                        total_storage_gb += disk.size_in_gbs

            if total_storage_gb == 0 and hasattr(compute, 'storage_in_mbs') and compute.storage_in_mbs:
                total_storage_gb = compute.storage_in_mbs / 1024
                if disk_count == 0:
                    disk_count = 1

            if disk_count == 0 and hasattr(compute, 'disks_count') and compute.disks_count:
                disk_count = compute.disks_count

        # AWS-specific architecture
        if hasattr(full_asset, 'aws_ec2') and full_asset.aws_ec2:
            aws_ec2 = full_asset.aws_ec2

            if hasattr(aws_ec2, 'architecture') and aws_ec2.architecture:
                architecture = aws_ec2.architecture
            elif hasattr(aws_ec2, 'platform_details') and aws_ec2.platform_details:
                platform = str(aws_ec2.platform_details).lower()
                if 'arm' in platform or 'graviton' in platform:
                    architecture = 'arm64'
                else:
                    architecture = 'x86_64'

            if hasattr(aws_ec2, 'block_device_mappings') and aws_ec2.block_device_mappings:
                if disk_count == 0:
                    disk_count = len(aws_ec2.block_device_mappings)

        # Infer architecture from instance type
        if architecture == 'Unknown':
            instance_type = _get_instance_type_from_asset(full_asset)
            if instance_type:
                instance_lower = instance_type.lower()
                if any(prefix in instance_lower for prefix in ['a1.', 't4g.', 'm6g.', 'c6g.', 'r6g.', 'm7g.', 'c7g.', 'r7g.']):
                    architecture = 'arm64'
                else:
                    architecture = 'x86_64'

        if architecture == 'Unknown':
            architecture = 'x86_64'

    except Exception as e:
        print(f"Warning: Could not get detailed info for asset {asset_id}: {e}")

    return round(total_storage_gb, 2), max(disk_count, 1), architecture


def _get_instance_type_from_asset(asset) -> Optional[str]:
    """Extract instance type from asset object."""
    instance_type = None

    if hasattr(asset, 'compute') and asset.compute:
        hardware_type = getattr(asset.compute, 'hardware_type', None)
        if hardware_type and isinstance(hardware_type, str) and hardware_type.strip():
            instance_type = hardware_type.strip()

    if not instance_type and hasattr(asset, 'aws_ec2') and asset.aws_ec2:
        aws_instance_type = getattr(asset.aws_ec2, 'instance_type', None)
        if aws_instance_type and isinstance(aws_instance_type, str) and aws_instance_type.strip():
            instance_type = aws_instance_type.strip()

    if not instance_type and hasattr(asset, 'freeform_tags') and asset.freeform_tags:
        tags = asset.freeform_tags
        tag_keys = ['aws_instance_type', 'instance_type', 'instanceType']
        for key in tag_keys:
            if key in tags and tags[key]:
                instance_type = str(tags[key]).strip()
                break

    return instance_type


def get_enhanced_vm_specs(inventory_client, asset_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract enhanced VM specifications including Sales Intelligence fields.
    """
    try:
        asset = inventory_client.get_asset(asset_id).data
    except Exception as e:
        print(f"Warning: Could not fetch asset {asset_id}: {e}")
        return None

    specs = {
        'name': asset.display_name,
        'source_type': 'Unknown',
        'cpu_count': None,
        'memory_gb': None,
        'memory_mb': None,
        'instance_type': None,
        'cpu_model': None,
        'operating_system': None,
        'primary_ip': None,
        'power_state': None,
        'specs_from_lookup': False,
        'disk_count': 1,
        'architecture': 'x86_64'
    }

    external_key = getattr(asset, 'external_asset_key', '')
    if external_key.startswith('vol-'):
        return None

    # Determine source type
    is_aws = False
    if external_key.startswith('i-') or external_key.startswith('ami-'):
        is_aws = True

    if hasattr(asset, 'asset_source_ids') and asset.asset_source_ids:
        for source_id in asset.asset_source_ids:
            if source_id and 'aws' in source_id.lower():
                is_aws = True
                break

    source_key = getattr(asset, 'source_key', '').lower()
    if any(region in source_key for region in ['us-east', 'us-west', 'eu-', 'ap-', 'sa-', 'ca-', 'me-', 'af-']):
        is_aws = True

    asset_type = getattr(asset, 'asset_type', '')
    if asset_type == 'AWS_EC2':
        is_aws = True

    if is_aws:
        specs['source_type'] = 'AWS'
    elif asset_type == 'VMWARE_VM':
        specs['source_type'] = 'VMware'
    elif asset_type == 'VM':
        specs['source_type'] = 'Generic VM'
    else:
        specs['source_type'] = str(asset_type) if asset_type else 'Unknown'

    # Get compute specs
    compute = getattr(asset, 'compute', None)
    if compute:
        specs['cpu_count'] = getattr(compute, 'cores_count', None)

        memory_mb = getattr(compute, 'memory_in_mbs', None)
        if memory_mb:
            specs['memory_mb'] = int(memory_mb)
            specs['memory_gb'] = round(memory_mb / 1024, 2)

        specs['cpu_model'] = getattr(compute, 'cpu_model', None)
        specs['operating_system'] = getattr(compute, 'operating_system', None)
        specs['primary_ip'] = getattr(compute, 'primary_ip', None)
        specs['power_state'] = getattr(compute, 'power_state', None)

        if hasattr(compute, 'disks') and compute.disks:
            specs['disk_count'] = len(compute.disks)
        elif hasattr(compute, 'disks_count') and compute.disks_count:
            specs['disk_count'] = compute.disks_count

    # AWS-specific handling
    if specs['source_type'] == 'AWS':
        specs['instance_type'] = _get_instance_type_from_asset(asset)

        if specs['instance_type'] and (specs['cpu_count'] is None or specs['memory_gb'] is None):
            aws_specs = get_aws_specs_from_instance_type(specs['instance_type'])
            if aws_specs:
                if specs['cpu_count'] is None:
                    specs['cpu_count'] = aws_specs['cpu']
                    specs['specs_from_lookup'] = True
                if specs['memory_gb'] is None:
                    specs['memory_gb'] = aws_specs['ram']
                    specs['memory_mb'] = int(aws_specs['ram'] * 1024)
                    specs['specs_from_lookup'] = True

        if specs['instance_type']:
            instance_lower = specs['instance_type'].lower()
            if any(prefix in instance_lower for prefix in ['a1.', 't4g.', 'm6g.', 'c6g.', 'r6g.', 'm7g.', 'c7g.', 'r7g.']):
                specs['architecture'] = 'arm64'
            else:
                specs['architecture'] = 'x86_64'

    if hasattr(asset, 'aws_ec2') and asset.aws_ec2:
        aws_ec2 = asset.aws_ec2
        if hasattr(aws_ec2, 'architecture') and aws_ec2.architecture:
            specs['architecture'] = aws_ec2.architecture

        if hasattr(aws_ec2, 'block_device_mappings') and aws_ec2.block_device_mappings:
            specs['disk_count'] = max(specs['disk_count'], len(aws_ec2.block_device_mappings))

    if specs['source_type'] == 'VMware':
        if hasattr(asset, 'vm') and asset.vm:
            vm_props = asset.vm
            if hasattr(vm_props, 'disks') and vm_props.disks:
                specs['disk_count'] = len(vm_props.disks)

    return specs


def fetch_inventory_assets(
    compartment_id: Optional[str] = None,
    asset_source_id: Optional[str] = None
) -> List[InventoryAsset]:
    """
    Fetch all discovered VM assets with Sales Intelligence analysis.
    """
    clients = init_oci_clients()
    inventory_client = clients['inventory']

    if not compartment_id:
        compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')
    if not asset_source_id:
        asset_source_id = config.get('OCM_ASSET_SOURCE_OCID')

    if not compartment_id:
        raise ValueError("Compartment ID is required. Set OCM_TARGET_COMPARTMENT_OCID in config.")

    # Fetch all active assets with pagination
    all_assets = []
    next_page = None

    while True:
        kwargs = {
            'compartment_id': compartment_id,
            'lifecycle_state': "ACTIVE",
            'limit': 100
        }
        if next_page:
            kwargs['page'] = next_page

        response = inventory_client.list_assets(**kwargs)
        all_assets.extend(response.data.items)

        if hasattr(response, 'next_page') and response.next_page:
            next_page = response.next_page
        else:
            break

    # Filter by asset source if provided
    if asset_source_id:
        filtered_assets = []
        for asset in all_assets:
            if hasattr(asset, 'asset_source_ids') and asset_source_id in (asset.asset_source_ids or []):
                filtered_assets.append(asset)
        all_assets = filtered_assets

    # Process each asset
    inventory_list = []

    for asset in all_assets:
        try:
            external_key = getattr(asset, 'external_asset_key', '')
            if external_key.startswith('vol-'):
                continue

            specs = get_enhanced_vm_specs(inventory_client, asset.id)
            if not specs:
                continue

            storage_gb, disk_count, architecture = get_asset_detailed_info(inventory_client, asset.id)
            final_disk_count = max(specs.get('disk_count', 1), disk_count)

            # Prepare data for complexity calculation
            asset_data = {
                'disk_count': final_disk_count,
                'architecture': specs.get('architecture', architecture) or architecture,
                'os_type': specs.get('operating_system', 'Unknown') or 'Unknown',
                'storage_gb': storage_gb if storage_gb > 0 else 0,
                'memory_gb': specs.get('memory_gb', 0) or 0
            }

            # Calculate complexity
            complexity_score, complexity_level, complexity_reasons = calculate_complexity_score(asset_data)

            # Detect zombie VM
            power_state = specs.get('power_state', 'Unknown') or 'Unknown'
            is_zombie = detect_zombie_vm(power_state)

            # Detect license opportunity
            os_type = specs.get('operating_system', 'Unknown') or 'Unknown'
            has_license_opportunity, license_type = detect_license_opportunity(os_type)

            # Build intelligence flags
            intelligence_flags = []
            if is_zombie:
                intelligence_flags.append("zombie")
            if has_license_opportunity:
                intelligence_flags.append("license_opportunity")
            if final_disk_count > 1:
                intelligence_flags.append("multi_disk")
            if complexity_level == ComplexityLevel.COMPLEX.value:
                intelligence_flags.append("complex_migration")

            # Create InventoryAsset
            inv_asset = InventoryAsset(
                asset_id=asset.id,
                hostname=specs.get('name', 'Unknown'),
                os_type=os_type,
                vcpu_count=specs.get('cpu_count', 0) or 0,
                memory_gb=specs.get('memory_gb', 0) or 0,
                memory_mb=specs.get('memory_mb', 0) or 0,
                storage_gb=storage_gb if storage_gb > 0 else 0,
                source_type=specs.get('source_type', 'Unknown'),
                power_state=power_state,
                primary_ip=specs.get('primary_ip', '') or '',
                instance_type=specs.get('instance_type', '') or '',
                specs_from_lookup=specs.get('specs_from_lookup', False),
                disk_count=final_disk_count,
                architecture=specs.get('architecture', architecture) or architecture,
                complexity_score=complexity_score,
                complexity_level=complexity_level,
                is_zombie=is_zombie,
                has_license_opportunity=has_license_opportunity,
                license_type=license_type,
                intelligence_flags=intelligence_flags
            )

            inventory_list.append(inv_asset)

        except Exception as e:
            print(f"Warning: Error processing asset {asset.id}: {e}")
            continue

    return inventory_list


def get_tshirt_size(vcpus: int, ram_gb: float, source_type: str) -> str:
    """
    Calculate T-Shirt sizing for VMware assets without instance type.

    - Small: vCPUs < 2
    - Large: vCPUs >= 8 OR RAM >= 32
    - Medium: All others
    """
    if source_type != 'VMware':
        return 'Unknown'

    if vcpus < 2:
        return 'General (Small)'
    elif vcpus >= 8 or ram_gb >= 32:
        return 'Memory Opt (Large)'
    else:
        return 'General (Medium)'


def get_display_instance_type(asset: InventoryAsset) -> str:
    """Get display instance type, applying T-Shirt sizing for VMware if needed."""
    # If instance type exists and is not N/A, use it
    if asset.instance_type and asset.instance_type not in ('N/A', '', 'Unknown', 'VMware'):
        return asset.instance_type

    # Apply T-Shirt sizing for VMware
    return get_tshirt_size(asset.vcpu_count, asset.memory_gb, asset.source_type)


def calculate_instance_type_distribution(assets: List[InventoryAsset]) -> List[InstanceTypeDistribution]:
    """Calculate the distribution of assets by instance type (with T-Shirt sizing for VMware)."""
    type_groups: Dict[str, List[InventoryAsset]] = {}

    for asset in assets:
        instance_type = get_display_instance_type(asset)
        if instance_type not in type_groups:
            type_groups[instance_type] = []
        type_groups[instance_type].append(asset)

    distribution = []
    for instance_type, group in type_groups.items():
        dist = InstanceTypeDistribution(
            instance_type=instance_type,
            count=len(group),
            total_vcpus=sum(a.vcpu_count for a in group),
            total_memory_gb=round(sum(a.memory_gb for a in group), 2),
            total_storage_gb=round(sum(a.storage_gb for a in group), 2)
        )
        distribution.append(dist)

    distribution.sort(key=lambda x: x.count, reverse=True)
    return distribution


def calculate_complexity_distribution(assets: List[InventoryAsset]) -> ComplexityDistribution:
    """Calculate distribution of assets by complexity level."""
    standard_count = sum(1 for a in assets if a.complexity_level == ComplexityLevel.STANDARD.value)
    moderate_count = sum(1 for a in assets if a.complexity_level == ComplexityLevel.MODERATE.value)
    complex_count = sum(1 for a in assets if a.complexity_level == ComplexityLevel.COMPLEX.value)

    total = len(assets) if assets else 1  # Avoid division by zero

    return ComplexityDistribution(
        standard_count=standard_count,
        moderate_count=moderate_count,
        complex_count=complex_count,
        standard_percentage=round((standard_count / total) * 100, 1),
        moderate_percentage=round((moderate_count / total) * 100, 1),
        complex_percentage=round((complex_count / total) * 100, 1)
    )


def calculate_inventory_summary(assets: List[InventoryAsset]) -> InventorySummary:
    """Calculate comprehensive summary statistics with Sales Intelligence."""
    total_vcpus = sum(a.vcpu_count for a in assets)
    total_ram_gb = sum(a.memory_gb for a in assets)
    total_storage_gb = sum(a.storage_gb for a in assets)
    total_disks = sum(a.disk_count for a in assets)

    vmware_count = sum(1 for a in assets if a.source_type == 'VMware')
    aws_count = sum(1 for a in assets if a.source_type == 'AWS')
    other_count = len(assets) - vmware_count - aws_count

    powered_on = sum(1 for a in assets if a.power_state and 'on' in a.power_state.lower())
    powered_off = len(assets) - powered_on

    multi_disk_vms = sum(1 for a in assets if a.disk_count > 1)

    # Zombie (cost savings) analysis
    zombies = [a for a in assets if a.is_zombie]
    zombie_count = len(zombies)
    zombie_potential_savings_vcpus = sum(a.vcpu_count for a in zombies)
    zombie_potential_savings_ram_gb = sum(a.memory_gb for a in zombies)

    # Commercial license analysis (Windows + RHEL are paid licenses)
    license_opportunity_count = sum(1 for a in assets if a.has_license_opportunity)
    windows_count = sum(1 for a in assets if a.license_type == 'Windows')
    rhel_count = sum(1 for a in assets if a.license_type == 'RHEL')
    linux_count = sum(1 for a in assets if a.license_type in ['Linux', 'Oracle Linux'])

    # Distributions
    instance_type_dist = calculate_instance_type_distribution(assets)
    complexity_dist = calculate_complexity_distribution(assets)
    arch_counter = Counter(a.architecture for a in assets)
    arch_distribution = dict(arch_counter)

    return InventorySummary(
        total_vms=len(assets),
        total_vcpus=total_vcpus,
        total_ram_gb=round(total_ram_gb, 2),
        total_ram_tb=round(total_ram_gb / 1024, 2),
        total_storage_gb=round(total_storage_gb, 2),
        total_storage_tb=round(total_storage_gb / 1024, 2),
        total_disks=total_disks,
        vmware_count=vmware_count,
        aws_count=aws_count,
        other_count=other_count,
        powered_on_count=powered_on,
        powered_off_count=powered_off,
        multi_disk_vms=multi_disk_vms,
        complexity_distribution=complexity_dist.to_dict(),
        zombie_count=zombie_count,
        zombie_potential_savings_vcpus=zombie_potential_savings_vcpus,
        zombie_potential_savings_ram_gb=round(zombie_potential_savings_ram_gb, 2),
        license_opportunity_count=license_opportunity_count,
        windows_count=windows_count,
        rhel_count=rhel_count,
        linux_count=linux_count,
        instance_type_distribution=[d.to_dict() for d in instance_type_dist],
        architecture_distribution=arch_distribution
    )


def get_inventory_dashboard_data(
    compartment_id: Optional[str] = None,
    asset_source_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get complete dashboard data with Sales Intelligence and OCI Sizing.

    Uses Multi-Bridge Aggregation to automatically discover and fetch
    from ALL connected asset sources (VMware, AWS, etc.).

    Includes Presales Sizing Engine for OCI cost comparison.
    """
    # Use multi-bridge aggregation to get ALL sources
    assets, sources = fetch_all_inventory(compartment_id)
    summary = calculate_inventory_summary(assets)

    # Build source breakdown for metadata
    source_breakdown = []
    for src in sources:
        source_breakdown.append({
            'name': src.name,
            'type': src.source_type,
            'asset_count': src.asset_count
        })

    # Convert assets to dicts for sizing
    asset_dicts = [a.to_dict() for a in assets]

    # Enrich with OCI Sizing (Presales Engine)
    try:
        sizer = MigrationSizer()
        enriched_assets = sizer.enrich_inventory(asset_dicts)
        sizing_summary = sizer.calculate_summary(enriched_assets)
        quick_wins = sizer.get_quick_wins(enriched_assets)
        top_savings = sizer.get_top_savings_opportunities(enriched_assets, top_n=10)

        sizing_data = {
            'summary': sizing_summary.to_dict(),
            'quick_wins_count': len(quick_wins),
            'top_savings': [
                {
                    'hostname': a.get('hostname'),
                    'source_type': a.get('source_type'),
                    'instance_type': a.get('instance_type'),
                    'oci_shape': a.get('oci_shape'),
                    'monthly_savings': a.get('monthly_savings'),
                    'savings_percentage': a.get('savings_percentage')
                }
                for a in top_savings
            ]
        }
    except Exception as e:
        print(f"[Sizing] Warning: Could not calculate OCI sizing: {e}")
        enriched_assets = asset_dicts
        sizing_data = None

    return {
        'assets': enriched_assets,
        'summary': summary.to_dict(),
        'sizing': sizing_data,
        'metadata': {
            'generated_at': datetime.now().isoformat(),
            'compartment_id': compartment_id or config.get('OCM_TARGET_COMPARTMENT_OCID'),
            'region': config.get('OCM_REGION', 'us-ashburn-1'),
            'version': '3.2 - Presales Sizing Engine',
            'sources_count': len(sources),
            'source_breakdown': source_breakdown
        }
    }


def export_to_csv(assets: List[InventoryAsset]) -> str:
    """Export inventory assets to CSV with all Sales Intelligence fields."""
    import csv
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        'Hostname',
        'Source Type',
        'Instance Type',
        'Architecture',
        'vCPU Count',
        'Memory (GB)',
        'Storage (GB)',
        'Disk Count',
        'OS Type',
        'Power State',
        'Flags',
        'Primary IP',
        'Asset ID'
    ])

    # Data rows
    for asset in assets:
        flags = []
        if asset.is_zombie:
            flags.append('Zombie')
        if asset.has_license_opportunity:
            flags.append(asset.license_type)
        if asset.disk_count > 1:
            flags.append('Multi-Disk')

        writer.writerow([
            asset.hostname,
            asset.source_type,
            asset.instance_type or 'N/A',
            asset.architecture,
            asset.vcpu_count,
            asset.memory_gb,
            asset.storage_gb,
            asset.disk_count,
            asset.os_type,
            asset.power_state,
            '; '.join(flags),
            asset.primary_ip,
            asset.asset_id
        ])

    return output.getvalue()


def generate_pdf_report(assets: List[InventoryAsset], summary: InventorySummary) -> bytes:
    """Generate PDF report with Sales Intelligence data."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from io import BytesIO

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), topMargin=0.5*inch, bottomMargin=0.5*inch)

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=18, spaceAfter=12, textColor=colors.HexColor('#312d2a'))
        subtitle_style = ParagraphStyle('CustomSubtitle', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#6b6969'), spaceAfter=20)
        section_style = ParagraphStyle('SectionTitle', parent=styles['Heading2'], fontSize=14, spaceBefore=15, spaceAfter=10, textColor=colors.HexColor('#312d2a'))

        elements = []

        # Title
        elements.append(Paragraph("ExpressLane Pre-Flight Inventory Report", title_style))
        elements.append(Paragraph("Sales Intelligence Edition", subtitle_style))
        elements.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Region: {config.get('OCM_REGION', 'us-ashburn-1')}", subtitle_style))

        # Executive Summary
        summary_data = [
            ['Total VMs', 'Total vCPUs', 'Total RAM', 'Total Storage', 'Zombie VMs', 'License Opps'],
            [
                str(summary.total_vms),
                str(summary.total_vcpus),
                f"{summary.total_ram_gb:.0f} GB" if summary.total_ram_tb < 1 else f"{summary.total_ram_tb:.2f} TB",
                f"{summary.total_storage_gb:.0f} GB" if summary.total_storage_tb < 1 else f"{summary.total_storage_tb:.2f} TB",
                str(summary.zombie_count),
                str(summary.license_opportunity_count)
            ]
        ]

        summary_table = Table(summary_data, colWidths=[1.3*inch]*6)
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#312d2a')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f5f3ef')),
            ('FONTSIZE', (0, 1), (-1, -1), 12),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica-Bold'),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e9e8e6')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e9e8e6')),
        ]))
        elements.append(summary_table)
        elements.append(Spacer(1, 20))

        # Complexity Distribution
        elements.append(Paragraph("Migration Complexity Analysis", section_style))
        complexity = summary.complexity_distribution
        complexity_data = [
            ['Standard (Green)', 'Moderate (Yellow)', 'Complex (Red)'],
            [
                f"{complexity.get('standard_count', 0)} ({complexity.get('standard_percentage', 0):.1f}%)",
                f"{complexity.get('moderate_count', 0)} ({complexity.get('moderate_percentage', 0):.1f}%)",
                f"{complexity.get('complex_count', 0)} ({complexity.get('complex_percentage', 0):.1f}%)"
            ]
        ]
        complexity_table = Table(complexity_data, colWidths=[2*inch]*3)
        complexity_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#22c55e')),
            ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#eab308')),
            ('BACKGROUND', (2, 0), (2, 0), colors.HexColor('#ef4444')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e9e8e6')),
        ]))
        elements.append(complexity_table)
        elements.append(Spacer(1, 20))

        # Asset Details
        elements.append(Paragraph("Asset Details", section_style))
        table_data = [['Hostname', 'Instance Type', 'vCPU', 'RAM', 'Disks', 'Complexity', 'Flags']]

        for asset in assets[:100]:
            flags = []
            if asset.is_zombie:
                flags.append('Zombie')
            if asset.has_license_opportunity:
                flags.append('License')
            if asset.disk_count > 1:
                flags.append('Multi-Disk')

            table_data.append([
                asset.hostname[:18] if len(asset.hostname) > 18 else asset.hostname,
                asset.instance_type[:12] if asset.instance_type else 'N/A',
                str(asset.vcpu_count),
                f"{asset.memory_gb:.0f}",
                str(asset.disk_count),
                asset.complexity_level.title(),
                ', '.join(flags) if flags else '-'
            ])

        asset_table = Table(table_data, colWidths=[1.5*inch, 1*inch, 0.5*inch, 0.6*inch, 0.5*inch, 0.8*inch, 1.2*inch])

        table_style_commands = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#312d2a')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (2, 0), (4, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('FONTSIZE', (0, 1), (-1, -1), 7),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e9e8e6')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f8fa')]),
        ]

        # Color code complexity column
        for i, asset in enumerate(assets[:100], start=1):
            if asset.complexity_level == 'standard':
                table_style_commands.append(('BACKGROUND', (5, i), (5, i), colors.HexColor('#dcfce7')))
            elif asset.complexity_level == 'moderate':
                table_style_commands.append(('BACKGROUND', (5, i), (5, i), colors.HexColor('#fef3c7')))
            else:
                table_style_commands.append(('BACKGROUND', (5, i), (5, i), colors.HexColor('#fee2e2')))

        asset_table.setStyle(TableStyle(table_style_commands))
        elements.append(asset_table)

        doc.build(elements)
        return buffer.getvalue()

    except ImportError:
        raise ImportError("PDF export requires 'reportlab' package. Install with: pip install reportlab")


# CLI usage
if __name__ == '__main__':
    import json

    print("=" * 60)
    print("ExpressLane Inventory Dashboard - Multi-Bridge Aggregation")
    print("=" * 60)

    try:
        data = get_inventory_dashboard_data()
        summary = data['summary']
        metadata = data['metadata']

        # Show connected sources
        print(f"\n=== CONNECTED BRIDGES ({metadata['sources_count']}) ===")
        for src in metadata.get('source_breakdown', []):
            print(f"  [{src['type']}] {src['name']}: {src['asset_count']} assets")

        print(f"\n=== EXECUTIVE SUMMARY (ALL SOURCES) ===")
        print(f"Total VMs: {summary['total_vms']}")
        print(f"Total vCPUs: {summary['total_vcpus']}")
        print(f"Total RAM: {summary['total_ram_gb']:.2f} GB ({summary['total_ram_tb']:.2f} TB)")
        print(f"Total Storage: {summary['total_storage_gb']:.2f} GB ({summary['total_storage_tb']:.2f} TB)")

        print(f"\n=== SALES INTELLIGENCE ===")
        print(f"Zombie VMs (Potential Savings): {summary['zombie_count']}")
        print(f"  - Idle vCPUs: {summary['zombie_potential_savings_vcpus']}")
        print(f"  - Idle RAM: {summary['zombie_potential_savings_ram_gb']:.2f} GB")
        print(f"Commercial License Count: {summary['license_opportunity_count']}")
        print(f"  - Windows: {summary['windows_count']}")
        print(f"  - RHEL: {summary['rhel_count']}")

        print(f"\n=== COMPLEXITY ANALYSIS ===")
        complexity = summary['complexity_distribution']
        print(f"Standard (Green): {complexity['standard_count']} ({complexity['standard_percentage']:.1f}%)")
        print(f"Moderate (Yellow): {complexity['moderate_count']} ({complexity['moderate_percentage']:.1f}%)")
        print(f"Complex (Red): {complexity['complex_count']} ({complexity['complex_percentage']:.1f}%)")

        print(f"\n=== INSTANCE TYPE DISTRIBUTION ===")
        for dist in summary['instance_type_distribution'][:10]:
            print(f"  {dist['instance_type']}: {dist['count']} instances")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
