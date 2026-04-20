# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane

Database models for Oracle Cloud Migrations (OCM) tracking.
This module contains SQLAlchemy models for tracking OCM migration jobs.
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class OCMMigration(db.Model):
    """Model for tracking Oracle Cloud Migrations (OCM) to OCI."""

    id = db.Column(db.Integer, primary_key=True)
    vm_name = db.Column(db.String(255), nullable=False)  # Primary VM name (first VM if batch)
    source_type = db.Column(db.String(50), nullable=False)  # VMware, AWS, etc.
    asset_source_id = db.Column(db.String(500))  # Cloud Bridge Asset Source OCID
    status = db.Column(db.String(50), default='Pending')  # Pending, Running, Completed, Failed, In-Sync, Cutting-Over, Cancelled
    # Note: Test-* statuses removed in the awesomeworking engine transplant (2026-04-11).
    # Test migration state now lives in the sidecar test_status column below.
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    logs = db.Column(db.Text(length=16777215), default='')  # MEDIUMTEXT

    # Track migration progress (6 steps for OCM)
    current_step = db.Column(db.Integer, default=1)  # 1-6
    last_completed_step = db.Column(db.Integer, default=0)  # For resume functionality
    can_resume = db.Column(db.Boolean, default=False)  # Whether migration can be resumed

    # OCM-specific OCIDs for each step
    project_ocid = db.Column(db.String(500))  # Step 1: Migration Project
    plan_ocid = db.Column(db.String(500))     # Step 2: Migration Plan
    asset_ocid = db.Column(db.String(500))    # Step 3: Migration Asset (deprecated - use asset_ocids_json)
    rms_stack_ocid = db.Column(db.String(500))  # Step 5: Resource Manager Stack
    rms_job_id = db.Column(db.String(500))    # Step 6: RMS Deployment Job

    # Batch migration support
    is_batch = db.Column(db.Boolean, default=False)  # True if migrating multiple VMs
    vm_count = db.Column(db.Integer, default=1)  # Number of VMs in this migration
    vms_json = db.Column(db.Text)  # JSON array of all VMs: [{"vm_name": "...", "inventory_asset_id": "...", "source_type": "..."}, ...]
    asset_ocids_json = db.Column(db.Text)  # JSON array of asset OCIDs created for each VM

    # Original inventory asset info (for single VM migrations - kept for backward compatibility)
    inventory_asset_id = db.Column(db.String(500))  # Cloud Bridge inventory asset OCID

    # VM configuration
    vm_config_json = db.Column(db.Text)  # Store VM config as JSON

    # Sync Now sidecar — set to 'Running' while a manual delta sync monitor
    # thread is attached to the asset's active replication work request, and
    # cleared back to None when the monitor exits. The detail page reads this
    # to paint step 4 with a spinner during the sync, purely as a visual
    # indicator — last_completed_step is never touched.
    sync_status = db.Column(db.String(20), default=None)  # None | Running

    # Warm Migration (Scheduled Cutover) support
    is_scheduled = db.Column(db.Boolean, default=False)  # If True, pause after replication for manual cutover
    schedule_type = db.Column(db.String(20), default='IMMEDIATE')  # IMMEDIATE, ONCE, DAILY, or WEEKLY
    start_hour = db.Column(db.Integer, default=2)  # Start hour for DAILY/WEEKLY schedules (0-23 UTC)
    replication_schedule_id = db.Column(db.String(500), nullable=True)  # OCM Replication Schedule OCID (for native scheduling)

    # Test Migration (pre-cutover validation) — awesomeworking sidecar model.
    # Transplanted 2026-04-11. Independent of main 6-step progress: the main
    # `status` column stays clean (Running/Completed/etc.) and test lifecycle
    # is tracked entirely via test_status below.
    test_status = db.Column(db.String(30), default=None)  # None | Deploying | Running | Cleaning Up | Cleaned Up | Failed
    test_rms_job_id = db.Column(db.String(500))           # APPLY job id for test VM
    test_cleanup_job_id = db.Column(db.String(500))       # DESTROY job id (last cleanup) — legacy field, unused by terminate_test_instances path
    test_start_time = db.Column(db.DateTime)
    test_end_time = db.Column(db.DateTime)
    test_logs = db.Column(db.Text(length=16777215), default='')  # Separate log stream for test + cleanup

    # --- Legacy V1.6 test-migration columns (kept for DB backwards-compat) ---
    # These are no longer read or written by the engine as of 2026-04-11. They
    # remain on the table so existing SQLite databases don't need a destructive
    # migration; new rows leave them NULL.
    test_rms_stack_ocid = db.Column(db.String(500))
    test_destroy_job_id = db.Column(db.String(500))
    test_instance_ocid = db.Column(db.String(500))
    test_cleanup_required = db.Column(db.Boolean, default=False)
    test_started_at = db.Column(db.DateTime)
    test_deployed_at = db.Column(db.DateTime)
    test_completed_at = db.Column(db.DateTime)
    test_migration_count = db.Column(db.Integer, default=0)
