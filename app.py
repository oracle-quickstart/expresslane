# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane
"""

import os
import subprocess
import sys
import json
import time
import functools
import logging
import tempfile
import threading
import shutil
import secrets
from io import StringIO
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf.csrf import CSRFProtect, CSRFError
from collections import defaultdict
import oci
from config import config, is_oci_cli_configured
from oci_clients import get_oci_client, get_oci_config
from models import db, OCMMigration
from version import __version__
import upgrade_check  # lightweight, opt-out version check — see upgrade_check.py

logger = logging.getLogger(__name__)


def _oci_error_response(e, context='OCI API call'):
    """Return a structured JSON error for OCI ServiceError exceptions.

    Returns HTTP 503 (retryable) for auth-propagation errors (401 / 400+SignatureNotValid),
    HTTP 502 with the real OCI message for everything else.
    """
    if e.status == 401 or (e.status == 400 and 'SignatureNotValid' in str(e.message)):
        logger.warning(f'{context}: auth not yet propagated (status={e.status})')
        return jsonify({
            'error': 'API key not yet recognized. Retrying automatically...',
            'retryable': True
        }), 503
    logger.error(f'{context}: OCI ServiceError {e.status} - {e.message}')
    return jsonify({'error': f'OCI API error: {e.message}', 'retryable': False}), 502


# ── In-memory OCI response cache ──────────────────────────────
# Per-worker process (gunicorn preload); at most N_WORKERS initial OCI calls.
_oci_cache = {}                    # {cache_key: {'data': ..., 'time': float}}
_oci_cache_lock = threading.Lock()
_OCI_CACHE_TTL = 300               # 5 minutes


def _cache_get(key):
    """Return cached data if TTL not expired, else None."""
    with _oci_cache_lock:
        entry = _oci_cache.get(key)
        if entry and (time.time() - entry['time']) < _OCI_CACHE_TTL:
            return entry['data']
    return None


def _cache_get_stale(key):
    """Return cached data ignoring TTL (error fallback only)."""
    with _oci_cache_lock:
        entry = _oci_cache.get(key)
        return entry['data'] if entry else None


def _cache_set(key, data):
    """Store data in cache."""
    with _oci_cache_lock:
        _oci_cache[key] = {'data': data, 'time': time.time()}


app = Flask(__name__)

# Session secret key — auto-generate and persist in config.json
if not config.get('SECRET_KEY'):
    config.set('SECRET_KEY', secrets.token_hex(32))
    config.save_config()
app.secret_key = config.get('SECRET_KEY')

# CSRF protection
csrf = CSRFProtect(app)

# Simple rate limiting (replaces Flask-Limiter)
_rate_limit_store = defaultdict(list)

def rate_limit(max_calls, period_seconds, methods=None):
    """Decorator: limit requests per IP. Only checks specified HTTP methods."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if methods and request.method not in methods:
                return f(*args, **kwargs)
            key = request.remote_addr
            now = time.time()
            _rate_limit_store[key] = [t for t in _rate_limit_store[key] if now - t < period_seconds]
            if len(_rate_limit_store[key]) >= max_calls:
                return jsonify({"error": "Too many requests"}), 429
            _rate_limit_store[key].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# Session cookie hardening
# Set SECURE_COOKIES=true if running behind an HTTPS load balancer / reverse proxy
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SECURE_COOKIES', '').lower() == 'true'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Database config - uses DATABASE_URL env var or defaults to SQLite
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///ocm_migrations.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)


@app.context_processor
def inject_version():
    return dict(version=__version__)


@app.context_processor
def inject_upgrade_status():
    # Surfaces the background upgrade-check result to every template.
    # Schema is stable; templates can key on `upgrade_status.available`.
    return dict(upgrade_status=upgrade_check.get_status())


# Kick off the background upgrade check on the first real request.
# Gunicorn runs with preload=True, so threads started at import time would
# not survive the fork. The fast-path flag here avoids the lock on hot paths;
# upgrade_check.start_background_check() is itself idempotent and thread-safe.
_upgrade_check_started = False


@app.before_request
def _start_upgrade_check_once():
    global _upgrade_check_started
    if not _upgrade_check_started:
        _upgrade_check_started = True
        upgrade_check.start_background_check(__version__)


@app.route('/api/upgrade-check', methods=['GET'])
def api_upgrade_check():
    """Return the cached upgrade-check status as JSON.

    Does not trigger a live refresh — the background thread populates the
    status once per 24h (+ jitter). Included `checked_at` lets consumers
    tell how fresh the result is.
    """
    status = upgrade_check.get_status()
    return jsonify({
        'current_version': __version__,
        'latest_version': status.get('latest'),
        'is_newer': bool(status.get('available')),
        'release_notes_url': status.get('notes_url'),
        'download_url': status.get('download_url'),
        'checked_at': status.get('checked_at'),
    })


@app.after_request
def set_security_headers(response):
    """Add security headers to every response"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    if os.environ.get('SECURE_COOKIES', '').lower() == 'true':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' cdnjs.cloudflare.com fonts.googleapis.com; "
        "font-src 'self' cdnjs.cloudflare.com fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    return response

def _migrate_schema():
    """Add columns that db.create_all() won't add to an existing table.

    Each migration is idempotent: it checks whether the column already exists
    before issuing ALTER TABLE, so it is safe to run on every startup.
    """
    import sqlalchemy

    migrations = [
        # Test Migration support (v1.3) — legacy V1.6 columns kept for DB back-compat
        ('ocm_migration', 'test_rms_stack_ocid',  'VARCHAR(500)'),
        ('ocm_migration', 'test_rms_job_id',      'VARCHAR(500)'),
        ('ocm_migration', 'test_destroy_job_id',   'VARCHAR(500)'),
        ('ocm_migration', 'test_instance_ocid',    'VARCHAR(500)'),
        ('ocm_migration', 'test_cleanup_required', 'BOOLEAN DEFAULT 0'),
        ('ocm_migration', 'test_started_at',       'DATETIME'),
        ('ocm_migration', 'test_deployed_at',      'DATETIME'),
        ('ocm_migration', 'test_completed_at',     'DATETIME'),
        ('ocm_migration', 'test_migration_count',  'INTEGER DEFAULT 0'),
        # Awesomeworking engine transplant (2026-04-11): sidecar test state
        ('ocm_migration', 'test_status',           'VARCHAR(30)'),
        ('ocm_migration', 'test_cleanup_job_id',   'VARCHAR(500)'),
        ('ocm_migration', 'test_start_time',       'DATETIME'),
        ('ocm_migration', 'test_end_time',         'DATETIME'),
        ('ocm_migration', 'test_logs',             'MEDIUMTEXT'),
        # Sync Now sidecar (2026-04-11): drives step 4 spinner overlay.
        ('ocm_migration', 'sync_status',           'VARCHAR(20)'),
    ]

    with db.engine.begin() as conn:
        for table, column, col_type in migrations:
            # Check if column already exists
            result = conn.execute(sqlalchemy.text(
                f"PRAGMA table_info({table})"
            ))
            existing_columns = {row[1] for row in result}
            if column not in existing_columns:
                logger.info(f'Schema migration: adding {table}.{column} ({col_type})')
                # SQLite doesn't support MEDIUMTEXT; fall back to TEXT.
                sqlite_type = col_type.replace('MEDIUMTEXT', 'TEXT')
                conn.execute(sqlalchemy.text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}"
                ))

        # One-time normalization: any rows still carrying V1.6-era Test-* main
        # statuses are unreachable in the new UI. Rewrite to the closest
        # equivalent so they stop showing up as "stuck" rows.
        try:
            conn.execute(sqlalchemy.text(
                "UPDATE ocm_migration SET status='Failed' "
                "WHERE status IN ('Test-Deploy-Failed','Test-Cleanup-Failed')"
            ))
            conn.execute(sqlalchemy.text(
                "UPDATE ocm_migration SET status='Running' "
                "WHERE status IN ('Test-Deploying','Test-Deployed','Test-Cleanup')"
            ))
        except Exception as e:
            logger.warning(f'Test-* status normalization skipped: {e}')


# Create tables and run schema migrations
with app.app_context():
    db.create_all()
    _migrate_schema()

migration_lock = threading.Lock()
MAX_CONCURRENT_MIGRATIONS = 5  # Maximum number of concurrent migrations allowed


@app.before_request
def require_login():
    """Global auth gate — redirect unauthenticated requests to /login"""
    open_endpoints = {'login', 'logout', 'setup', 'static'}
    if not config.is_admin_configured():
        return None
    if request.endpoint in open_endpoints:
        return None
    if not session.get('logged_in'):
        if request.path.startswith('/api/'):
            return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
        return redirect(url_for('login'))


class PrefixedStream:
    """
    Line-prefixing writer that forwards to an underlying stream.

    Used by the test/cleanup workers so every line emitted by the reused
    step5/step6/destroy functions is prefixed with e.g. '[TEST] ' before
    landing in the test log buffer. This keeps the main detail page's
    per-step log router (which greps for '[Step N/6]' in migration.logs)
    unaffected, since test output goes into a separate test_logs column.

    (Awesomeworking engine transplant 2026-04-11)
    """

    def __init__(self, underlying, prefix):
        self._underlying = underlying
        self._prefix = prefix
        self._buffer = ''

    def write(self, s):
        if not s:
            return
        self._buffer += s
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            if line:
                self._underlying.write(self._prefix + line + '\n')
            else:
                self._underlying.write('\n')

    def flush(self):
        if self._buffer:
            self._underlying.write(self._prefix + self._buffer)
            self._buffer = ''
        if hasattr(self._underlying, 'flush'):
            self._underlying.flush()

    def getvalue(self):
        return self._underlying.getvalue()


def run_ocm_migration(migration_id, dest_compartment=None, dest_vcn=None, dest_subnet=None):
    """Background worker for OCM (Oracle Cloud Migrations) migration"""
    from io import StringIO
    import ocm_migration

    with app.app_context():
        migration = OCMMigration.query.get(migration_id)
        if not migration:
            return

        output = StringIO()
        done_event = threading.Event()

        # Background thread to update logs periodically
        def update_logs():
            while not done_event.is_set():
                time.sleep(2)
                with app.app_context():
                    m = OCMMigration.query.get(migration_id)
                    if m:
                        m.logs = output.getvalue()
                        db.session.commit()

        update_thread = threading.Thread(target=update_logs, daemon=True)
        update_thread.start()

        try:
            migration.status = 'Running'
            migration.start_time = datetime.now()
            db.session.commit()

            # Initialize clients
            clients = ocm_migration.init_clients(output_stream=output)

            # Get configuration values
            # Source compartment (for project, plan, assets) - always from config/settings
            source_compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

            # Destination compartment (for deployed VMs) - from UI selection or fallback to source compartment
            dest_compartment_id = dest_compartment or source_compartment_id
            # VCN and Subnet must be provided from UI (no defaults)
            vcn_ocid = dest_vcn
            subnet_ocid = dest_subnet

            bucket_ocid = config.get('OCM_REPLICATION_BUCKET_OCID')
            poll_interval = int(config.get('OCM_POLL_INTERVAL_SECONDS', 30))

            # Get Object Storage namespace and bucket name
            namespace = ocm_migration.get_namespace(clients['object_storage'])
            bucket_name = ocm_migration.extract_bucket_name(
                clients['object_storage'],
                bucket_ocid,
                namespace,
                source_compartment_id,
                output_stream=output
            )

            # Determine starting step (for resume functionality)
            start_step = migration.last_completed_step + 1 if migration.last_completed_step > 0 else 1

            # Parse VMs list for batch migrations
            vms_list = []
            if migration.is_batch and migration.vms_json:
                vms_list = json.loads(migration.vms_json)
            else:
                # Single VM migration (backward compatibility)
                vms_list = [{
                    'vm_name': migration.vm_name,
                    'inventory_asset_id': migration.inventory_asset_id,
                    'source_type': migration.source_type
                }]

            # Parse VM configuration (for advanced migrations)
            vm_config = None
            if migration.vm_config_json:
                try:
                    vm_config = json.loads(migration.vm_config_json)
                    output.write(f"\nAdvanced Configuration Detected:\n")
                    batch_config = vm_config.get('batch_config', {})
                    if batch_config:
                        output.write(f"  Batch Shape: {batch_config.get('shape', 'Default')}\n")
                        if batch_config.get('ocpus'):
                            output.write(f"  Batch OCPUs: {batch_config.get('ocpus')}\n")
                        if batch_config.get('memory_gb'):
                            output.write(f"  Batch Memory: {batch_config.get('memory_gb')} GB\n")
                        output.write(f"  Volume Performance: {batch_config.get('volume_performance', 'BALANCED')}\n")
                    vm_overrides = vm_config.get('vm_overrides', {})
                    custom_count = sum(1 for v in vm_overrides.values() if not v.get('use_batch', True))
                    if custom_count > 0:
                        output.write(f"  VMs with custom config: {custom_count}\n")
                except Exception as e:
                    output.write(f"\nWarning: Could not parse vm_config_json: {e}\n")
                    vm_config = None

            output.write(f"\n{'='*80}\n")
            output.write(f"OCM MIGRATION STARTED\n")
            output.write(f"{'='*80}\n")
            if migration.is_batch:
                output.write(f"Batch Migration: {migration.vm_count} VMs\n")
                output.write(f"Primary VM: {migration.vm_name}\n")
                for i, vm in enumerate(vms_list):
                    output.write(f"  [{i+1}] {vm['vm_name']} ({vm['source_type']})\n")
            else:
                output.write(f"VM Name: {migration.vm_name}\n")
                output.write(f"Source Type: {migration.source_type}\n")
            output.write(f"Starting from Step: {start_step}\n")
            output.write(f"\nSource Compartment (for Project/Plan): {source_compartment_id}\n")
            output.write(f"Destination Compartment (for VM): {dest_compartment_id}\n")
            output.write(f"Destination VCN: {vcn_ocid}\n")
            output.write(f"Destination Subnet: {subnet_ocid}\n")
            output.write(f"{'='*80}\n\n")

            # Step 1: Create or Reuse Migration Project
            if start_step <= 1:
                migration.current_step = 1
                migration.can_resume = False
                db.session.commit()

                # Check if we have an existing migration to reuse (from Advanced config)
                if migration.project_ocid and migration.project_ocid.startswith('ocid1.ocmmigration'):
                    # Reuse existing migration (already has correct name from Advanced config)
                    output.write(f"\n{'='*80}\n")
                    output.write(f"[Step 1/6] Reusing Existing Migration Project\n")
                    output.write(f"{'='*80}\n")
                    output.write(f"Migration already created from Advanced configuration\n")
                    output.write(f"Project OCID: {migration.project_ocid}\n")

                    project_ocid = migration.project_ocid
                    migration.last_completed_step = 1
                    migration.can_resume = True
                    db.session.commit()
                else:
                    # Create new project (normal flow)
                    project_name = migration.vm_name
                    if migration.is_batch and migration.vm_count > 1:
                        additional_count = migration.vm_count - 1
                        project_name = f"{migration.vm_name} (+ {additional_count} Additional)"

                    project_ocid = ocm_migration.step1_create_project(
                        clients, project_name, source_compartment_id, output_stream=output
                    )
                    migration.project_ocid = project_ocid
                    migration.last_completed_step = 1
                    migration.can_resume = True
                    db.session.commit()
            else:
                output.write(f"\nSkipping Step 1 (already completed)\n")
                output.write(f"Project OCID: {migration.project_ocid}\n\n")

            # Step 2: Create Migration Plan
            if start_step <= 2:
                migration.current_step = 2
                migration.can_resume = False
                db.session.commit()

                # Create plan name with batch info
                plan_name = migration.vm_name
                if migration.is_batch and migration.vm_count > 1:
                    additional_count = migration.vm_count - 1
                    plan_name = f"{migration.vm_name} (+ {additional_count} Additional)"

                plan_ocid = ocm_migration.step2_create_plan(
                    clients, migration.project_ocid, plan_name,
                    source_compartment_id, dest_compartment_id, vcn_ocid, subnet_ocid,
                    vm_config=vm_config, vms_list=vms_list, output_stream=output
                )
                migration.plan_ocid = plan_ocid
                migration.last_completed_step = 2
                migration.can_resume = True
                db.session.commit()
            else:
                output.write(f"\nSkipping Step 2 (already completed)\n")
                output.write(f"Plan OCID: {migration.plan_ocid}\n\n")

            # Step 2.5: Apply Advanced Configuration to Target Assets
            if vm_config and start_step <= 3:
                output.write(f"\n{'='*80}\n")
                output.write(f"[Step 2.5/6] Applying Advanced Configuration to Target Assets\n")
                output.write(f"{'='*80}\n")

                vm_configs = vm_config.get('vm_configs', {})

                if vm_configs:
                    output.write(f"Found custom configurations for {len(vm_configs)} VM(s)\n\n")

                    try:
                        # Get target assets from the plan
                        target_assets = clients['migration'].list_target_assets(migration_plan_id=migration.plan_ocid)

                        for ta in target_assets.data.items:
                            # Get target asset details
                            ta_detail = clients['migration'].get_target_asset(target_asset_id=ta.id)

                            # Get the source asset ID from the migration asset
                            if hasattr(ta_detail.data, 'migration_asset') and hasattr(ta_detail.data.migration_asset, 'source_asset_id'):
                                source_id = ta_detail.data.migration_asset.source_asset_id

                                # Find matching VM in our list
                                vm_name = None
                                for vm in vms_list:
                                    if vm['inventory_asset_id'] == source_id:
                                        vm_name = vm['vm_name']
                                        break

                                if source_id in vm_configs:
                                    config_data = vm_configs[source_id]
                                    output.write(f"\nConfiguring target asset for {vm_name}:\n")

                                    try:
                                        # Build user_spec with custom configuration
                                        user_spec = None

                                        if 'shape' in config_data:
                                            user_spec = oci.cloud_migrations.models.LaunchInstanceDetails(
                                                shape=config_data['shape']
                                            )

                                            # Add flex shape config if applicable
                                            if 'ocpus' in config_data and 'memory_gb' in config_data:
                                                user_spec.shape_config = oci.cloud_migrations.models.LaunchInstanceShapeConfigDetails(
                                                    ocpus=float(config_data['ocpus']),
                                                    memory_in_gbs=float(config_data['memory_gb'])
                                                )
                                                output.write(f"  - Shape: {config_data['shape']}\n")
                                                output.write(f"  - OCPUs: {config_data['ocpus']}\n")
                                                output.write(f"  - Memory: {config_data['memory_gb']} GB\n")
                                            else:
                                                output.write(f"  - Shape: {config_data['shape']}\n")

                                        # Build update details
                                        update_details = oci.cloud_migrations.models.UpdateVmTargetAssetDetails(
                                            type='INSTANCE',
                                            user_spec=user_spec
                                        )

                                        # Update the target asset
                                        clients['migration'].update_target_asset(
                                            target_asset_id=ta.id,
                                            update_target_asset_details=update_details
                                        )
                                        output.write(f"  ✓ Custom configuration applied to target asset\n")

                                    except Exception as e:
                                        output.write(f"  Warning: Could not apply configuration: {e}\n")
                                        import traceback
                                        output.write(f"  Error details: {traceback.format_exc()}\n")

                    except Exception as e:
                        output.write(f"Warning: Could not apply target asset configurations: {e}\n")
                        import traceback
                        output.write(f"Error details: {traceback.format_exc()}\n")

                    output.write(f"\n✓ Advanced configuration complete - target assets updated\n")

            # Step 3: Add Asset(s) to Project (or use existing from temp migration)
            if start_step <= 3:
                migration.current_step = 3
                migration.can_resume = False
                db.session.commit()

                output.write(f"\n{'='*80}\n")
                output.write(f"[Step 3/6] Verifying Assets in Project\n")
                output.write(f"{'='*80}\n")

                # Get or create replication schedule for warm migrations
                replication_schedule_id = None
                schedule_type = getattr(migration, 'schedule_type', 'IMMEDIATE')
                start_hour = getattr(migration, 'start_hour', 2)  # Default to 2 AM UTC

                if schedule_type == 'ONCE':
                    # Run Once & Pause: No automatic schedule, just initial sync then pause
                    output.write(f"\nWarm Migration Mode: Run Once & Pause\n")
                    output.write(f"  - Initial replication will complete, then pause in In-Sync state\n")
                    output.write(f"  - Use 'Sync Now' for manual delta syncs when needed\n\n")
                elif schedule_type in ['DAILY', 'WEEKLY']:
                    # DAILY/WEEKLY: Create OCM replication schedule
                    output.write(f"\nConfiguring {schedule_type} replication schedule for warm migration...\n")
                    output.write(f"  Start Hour: {str(start_hour).zfill(2)}:00 UTC\n")
                    replication_schedule_id = ocm_migration.get_or_create_schedule(
                        clients, schedule_type, source_compartment_id,
                        target_hour=start_hour, output_stream=output
                    )
                    # Store the schedule ID in the migration record
                    migration.replication_schedule_id = replication_schedule_id
                    db.session.commit()
                    output.write(f"✓ Replication schedule configured\n\n")

                # Check if assets already exist from temp migration
                existing_assets = {}
                try:
                    mig_assets = clients['migration'].list_migration_assets(migration_id=migration.project_ocid)
                    for ma in mig_assets.data.items:
                        ma_detail = clients['migration'].get_migration_asset(migration_asset_id=ma.id)
                        if hasattr(ma_detail.data, 'source_asset_id'):
                            existing_assets[ma_detail.data.source_asset_id] = ma.id

                    if existing_assets:
                        output.write(f"Found {len(existing_assets)} existing asset(s) from temp migration - will reuse them\n")
                except Exception as e:
                    output.write(f"Note: Could not check for existing assets: {e}\n")

                asset_ocids = []
                # Process each VM
                for i, vm_data in enumerate(vms_list):
                    inventory_asset_id = vm_data['inventory_asset_id']
                    vm_name = vm_data['vm_name']

                    # Check if asset already exists
                    if inventory_asset_id in existing_assets:
                        # Asset exists - reuse it
                        asset_ocid = existing_assets[inventory_asset_id]
                        output.write(f"\n--- Asset {i+1}/{len(vms_list)}: {vm_name} ---\n")
                        output.write(f"✓ Reusing existing asset from temp migration\n")
                        output.write(f"Asset OCID: {asset_ocid}\n")
                        asset_ocids.append({
                            'vm_name': vm_name,
                            'asset_ocid': asset_ocid
                        })
                    else:
                        # Asset doesn't exist - add it
                        output.write(f"\n--- Adding Asset {i+1}/{len(vms_list)}: {vm_name} ---\n")
                        asset_ocid = ocm_migration.step3_add_asset(
                            clients, migration.project_ocid, vm_name,
                            inventory_asset_id, bucket_name, source_compartment_id,
                            replication_schedule_id=replication_schedule_id,
                            output_stream=output
                        )
                        asset_ocids.append({
                            'vm_name': vm_name,
                            'asset_ocid': asset_ocid
                        })
                        output.write(f"Asset OCID: {asset_ocid}\n")

                # Store all asset OCIDs
                migration.asset_ocids_json = json.dumps(asset_ocids)
                # Keep first asset in old field for backward compatibility
                migration.asset_ocid = asset_ocids[0]['asset_ocid'] if asset_ocids else None
                migration.last_completed_step = 3
                migration.can_resume = True
                db.session.commit()

                output.write(f"\n✓ All {len(asset_ocids)} asset(s) ready\n")
            else:
                output.write(f"\nSkipping Step 3 (already completed)\n")
                if migration.asset_ocids_json:
                    asset_ocids = json.loads(migration.asset_ocids_json)
                    for asset in asset_ocids:
                        output.write(f"  - {asset['vm_name']}: {asset['asset_ocid']}\n")
                else:
                    output.write(f"Asset OCID: {migration.asset_ocid}\n")
                output.write(f"\n")

            # Configuration already applied in Step 1.5 before plan creation

            # Step 4: Replicate Asset(s) (LONGEST STEP - can take hours)
            if start_step <= 4:
                migration.current_step = 4
                migration.can_resume = True  # Can resume even during replication
                db.session.commit()

                # Get asset OCIDs to replicate
                if migration.asset_ocids_json:
                    asset_ocids = json.loads(migration.asset_ocids_json)
                else:
                    # Backward compatibility for single asset migrations
                    asset_ocids = [{'vm_name': migration.vm_name, 'asset_ocid': migration.asset_ocid}]

                # Replicate each asset
                for i, asset_data in enumerate(asset_ocids):
                    output.write(f"\n--- Replicating Asset {i+1}/{len(asset_ocids)}: {asset_data['vm_name']} ---\n")
                    ocm_migration.step4_replicate_asset(
                        clients, asset_data['asset_ocid'], poll_interval, output_stream=output
                    )
                    output.write(f"✓ Replication complete for {asset_data['vm_name']}\n")

                migration.last_completed_step = 4
                db.session.commit()
                output.write(f"\n✓ Successfully replicated {len(asset_ocids)} asset(s)\n")

                # Check if this is a warm migration (scheduled cutover)
                if migration.is_scheduled and migration.status != 'Cutting-Over':
                    output.write(f"\n{'='*80}\n")
                    output.write(f"WARM MIGRATION - ENTERING IN-SYNC MODE\n")
                    output.write(f"{'='*80}\n")
                    output.write(f"Replication complete. Data is now synchronized.\n")
                    output.write(f"The VM will remain in-sync until you trigger the cutover.\n")
                    output.write(f"\nTo complete the migration:\n")
                    output.write(f"  1. Schedule a maintenance window\n")
                    output.write(f"  2. Click the 'Cutover' button to deploy the VM\n")
                    output.write(f"{'='*80}\n")

                    migration.status = 'In-Sync'
                    migration.end_time = datetime.now()  # Stop the duration timer
                    migration.logs = output.getvalue()
                    db.session.commit()

                    done_event.set()
                    return  # Exit thread - wait for manual cutover

            else:
                output.write(f"\nSkipping Step 4 (already completed)\n")
                output.write(f"Asset replication already complete\n\n")

            # Step 5: Generate RMS Stack
            # Always re-execute the plan, even if migration.rms_stack_ocid is
            # already set from a prior test deployment. OCM hydrates fresh
            # boot/block volumes on each execution; the volumes referenced
            # by an older stack are gone and APPLY would fail with 409 Conflict.
            # (Awesomeworking engine transplant 2026-04-11)
            if start_step <= 5:
                migration.current_step = 5
                migration.can_resume = False
                db.session.commit()

                rms_stack_ocid = ocm_migration.step5_generate_rms_stack(
                    clients, migration.plan_ocid, output_stream=output
                )
                migration.rms_stack_ocid = rms_stack_ocid
                migration.last_completed_step = 5
                migration.can_resume = True
                db.session.commit()
            else:
                output.write(f"\nSkipping Step 5 (already completed)\n")
                output.write(f"RMS Stack OCID: {migration.rms_stack_ocid}\n\n")

            # Step 6: Deploy RMS Stack
            # Awesomeworking engine transplant 2026-04-11: adds boot-volume
            # pre-flight and uses the on_job_created callback (fires BEFORE
            # polling so we never lose track of an in-flight APPLY).
            if start_step <= 6:
                migration.current_step = 6
                migration.can_resume = False
                db.session.commit()

                # Pre-flight: verify hydrated boot volumes are AVAILABLE.
                ocm_migration.validate_stack_boot_volumes(
                    clients, migration.rms_stack_ocid, output_stream=output
                )

                def _save_rms_job_id(job_id):
                    with app.app_context():
                        m = OCMMigration.query.get(migration_id)
                        if m:
                            m.rms_job_id = job_id
                            db.session.commit()
                            logger.info(f"Persisted RMS apply job ID to DB: {job_id}")

                rms_job_id = ocm_migration.step6_deploy_rms_stack(
                    clients, migration.rms_stack_ocid, poll_interval,
                    output_stream=output,
                    on_job_created=_save_rms_job_id
                )
                migration.rms_job_id = rms_job_id
                migration.last_completed_step = 6
                db.session.commit()
            else:
                output.write(f"\nSkipping Step 6 (already completed)\n")
                output.write(f"RMS Job ID: {migration.rms_job_id}\n\n")

            # Migration completed successfully
            output.write(f"\n{'='*80}\n")
            output.write(f"✓✓✓ OCM MIGRATION COMPLETED SUCCESSFULLY! ✓✓✓\n")
            output.write(f"{'='*80}\n")
            if migration.is_batch:
                output.write(f"Batch Migration: {migration.vm_count} VMs\n")
                output.write(f"Primary VM: {migration.vm_name}\n")
                if migration.vms_json:
                    vms = json.loads(migration.vms_json)
                    output.write(f"\nMigrated VMs:\n")
                    for i, vm in enumerate(vms):
                        output.write(f"  [{i+1}] {vm['vm_name']}\n")
            else:
                output.write(f"VM Name: {migration.vm_name}\n")
            output.write(f"\nProject OCID: {migration.project_ocid}\n")
            output.write(f"Plan OCID: {migration.plan_ocid}\n")
            if migration.asset_ocids_json:
                output.write(f"Asset OCIDs:\n")
                asset_ocids = json.loads(migration.asset_ocids_json)
                for asset in asset_ocids:
                    output.write(f"  - {asset['vm_name']}: {asset['asset_ocid']}\n")
            else:
                output.write(f"Asset OCID: {migration.asset_ocid}\n")
            output.write(f"RMS Stack OCID: {migration.rms_stack_ocid}\n")
            output.write(f"RMS Job ID: {migration.rms_job_id}\n")
            output.write(f"{'='*80}\n")
            output.write(f"\nNext Steps:\n")
            if migration.is_batch:
                output.write(f"1. Verify all {migration.vm_count} migrated VMs in OCI Console\n")
            else:
                output.write(f"1. Verify the migrated VM in OCI Console\n")
            output.write(f"2. Check Compute > Instances for the new instance(s)\n")
            output.write(f"3. Test VM connectivity and functionality\n")

            migration.status = 'Completed'
            migration.end_time = datetime.now()
            migration.can_resume = False
            db.session.commit()

            # No cleanup needed - migration project is the actual final project

        except ocm_migration.OCMMigrationError as e:
            migration.status = 'Failed'
            migration.end_time = datetime.now()
            output.write(f"\nERROR: OCM migration failed: {str(e)}\n")
            # Keep can_resume = True so user can retry from last completed step
            db.session.commit()

        except Exception as e:
            migration.status = 'Failed'
            migration.end_time = datetime.now()
            output.write(f"\nERROR: Unexpected error: {str(e)}\n")
            import traceback
            output.write(traceback.format_exc())
            db.session.commit()

        finally:
            # Final log update
            with app.app_context():
                m = OCMMigration.query.get(migration_id)
                if m:
                    m.logs = output.getvalue()
                    db.session.commit()

            # Signal update thread to stop
            done_event.set()
            update_thread.join()


def run_test_migration(migration_id):
    """
    Background worker that deploys a TEST VM from an In-Sync migration.

    Runs step5 (if needed) + step6 directly against the existing OCM plan,
    producing a real OCI VM that the user can validate. All output is
    captured into migration.test_logs (separate from migration.logs) and
    the 6-step progress tracker is NEVER advanced.

    (Awesomeworking engine transplant 2026-04-11 — replaces the legacy
    Test-* status state-machine with a clean sidecar test_status model.)
    """
    from io import StringIO
    import ocm_migration

    with app.app_context():
        migration = OCMMigration.query.get(migration_id)
        if not migration:
            return

        raw_buf = StringIO()
        test_buf = PrefixedStream(raw_buf, '[TEST] ')
        done_event = threading.Event()

        def flush_test_logs():
            while not done_event.is_set():
                time.sleep(2)
                with app.app_context():
                    m = OCMMigration.query.get(migration_id)
                    if m:
                        m.test_logs = raw_buf.getvalue()
                        db.session.commit()

        flush_thread = threading.Thread(target=flush_test_logs, daemon=True)
        flush_thread.start()

        try:
            test_buf.write(f"\n{'='*80}\n")
            test_buf.write(f"TEST MIGRATION STARTED at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            test_buf.write(f"{'='*80}\n")
            test_buf.write(f"Migration Plan: {migration.plan_ocid}\n\n")

            clients = ocm_migration.init_clients(output_stream=test_buf)
            poll_interval = int(config.get('OCM_POLL_INTERVAL_SECONDS', 30))

            # Step 5: Always re-execute the plan to hydrate volumes.
            rms_stack_ocid = ocm_migration.step5_generate_rms_stack(
                clients, migration.plan_ocid, output_stream=test_buf
            )
            migration.rms_stack_ocid = rms_stack_ocid
            db.session.commit()

            # Pre-flight: confirm the stack's hydrated boot volume is still
            # AVAILABLE. Catches the "dead volume from an earlier destroy"
            # scenario in seconds instead of wasting 3 minutes on a doomed APPLY.
            ocm_migration.validate_stack_boot_volumes(
                clients, migration.rms_stack_ocid, output_stream=test_buf
            )

            # Step 6: Deploy (APPLY) the stack -> creates the test VM.
            # Save test_rms_job_id via callback the instant the APPLY job is
            # created, so a mid-poll exception does not leave us unable to
            # identify instances for cleanup.
            def _save_test_job_id(job_id):
                with app.app_context():
                    m = OCMMigration.query.get(migration_id)
                    if m:
                        m.test_rms_job_id = job_id
                        db.session.commit()

            rms_job_id = ocm_migration.step6_deploy_rms_stack(
                clients, migration.rms_stack_ocid, poll_interval,
                output_stream=test_buf,
                on_job_created=_save_test_job_id
            )
            migration.test_rms_job_id = rms_job_id
            migration.test_status = 'Running'
            db.session.commit()

            test_buf.write(f"\n{'='*80}\n")
            test_buf.write(f"TEST VM DEPLOYED SUCCESSFULLY\n")
            test_buf.write(f"{'='*80}\n")
            test_buf.write(f"You can now validate the VM in the OCI Console.\n")
            test_buf.write(f"Click 'Clean Up Test VM' when you're ready to proceed.\n")

            db.session.commit()

        except Exception as e:
            import traceback
            test_buf.write(f"\nERROR: Test deployment failed: {str(e)}\n")
            test_buf.write(traceback.format_exc())
            migration.test_status = 'Failed'
            migration.test_end_time = datetime.now()
            db.session.commit()

        finally:
            test_buf.flush()
            with app.app_context():
                m = OCMMigration.query.get(migration_id)
                if m:
                    m.test_logs = raw_buf.getvalue()
                    db.session.commit()
            done_event.set()
            flush_thread.join()


def run_cleanup_test_vm(migration_id):
    """
    Background worker that cleans up a test VM by calling OCI Compute's
    TerminateInstance(preserve_boot_volume=True) on every instance the test
    APPLY job created. The RMS stack, its Terraform, and the OCM-hydrated
    boot volume all stay alive so the real cutover's subsequent APPLY can
    refresh state and relaunch the instance from the same preserved volume.

    NOT a Terraform DESTROY: that would default-terminate the boot volume,
    and OCM does not re-hydrate on repeat execute_migration_plan calls.

    (Awesomeworking engine transplant 2026-04-11.)
    """
    from io import StringIO
    import ocm_migration

    with app.app_context():
        migration = OCMMigration.query.get(migration_id)
        if not migration:
            return

        raw_buf = StringIO()
        # Append to any existing test_logs so deploy + cleanup history stays together
        if migration.test_logs:
            raw_buf.write(migration.test_logs)
        cleanup_buf = PrefixedStream(raw_buf, '[TEST CLEANUP] ')
        done_event = threading.Event()

        def flush_test_logs():
            while not done_event.is_set():
                time.sleep(2)
                with app.app_context():
                    m = OCMMigration.query.get(migration_id)
                    if m:
                        m.test_logs = raw_buf.getvalue()
                        db.session.commit()

        flush_thread = threading.Thread(target=flush_test_logs, daemon=True)
        flush_thread.start()

        try:
            cleanup_buf.write(f"\n{'='*80}\n")
            cleanup_buf.write(f"TEST VM CLEANUP STARTED at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            cleanup_buf.write(f"{'='*80}\n")

            if not migration.test_rms_job_id:
                cleanup_buf.write("No APPLY job recorded; nothing to terminate.\n")
                migration.test_status = 'Cleaned Up'
                migration.test_end_time = datetime.now()
                db.session.commit()
                return

            clients = ocm_migration.init_clients(output_stream=cleanup_buf)
            poll_interval = int(config.get('OCM_POLL_INTERVAL_SECONDS', 10))

            results = ocm_migration.terminate_test_instances(
                clients,
                migration.test_rms_job_id,
                poll_interval=min(poll_interval, 10),
                output_stream=cleanup_buf
            )

            migration.test_status = 'Cleaned Up'
            migration.test_end_time = datetime.now()
            migration.test_rms_job_id = None
            # Keep rms_stack_ocid intact - the stack and its preserved boot
            # volume are reused by the real cutover's APPLY.
            db.session.commit()

            cleanup_buf.write(f"\n{'='*80}\n")
            cleanup_buf.write(f"TEST VM CLEANUP COMPLETE\n")
            cleanup_buf.write(f"{'='*80}\n")
            cleanup_buf.write(f"The RMS stack, Terraform state, and hydrated boot volume are all retained.\n")
            cleanup_buf.write(f"The next Cutover Now will refresh Terraform state and relaunch the instance from the same boot volume.\n")

        except Exception as e:
            import traceback
            cleanup_buf.write(f"\nERROR: Cleanup failed: {str(e)}\n")
            cleanup_buf.write(traceback.format_exc())
            migration.test_status = 'Failed'
            migration.test_end_time = datetime.now()
            db.session.commit()

        finally:
            cleanup_buf.flush()
            with app.app_context():
                m = OCMMigration.query.get(migration_id)
                if m:
                    m.test_logs = raw_buf.getvalue()
                    db.session.commit()
            done_event.set()
            flush_thread.join()


# Routes

@app.route('/login', methods=['GET', 'POST'])
@rate_limit(max_calls=10, period_seconds=60, methods=["POST"])
def login():
    """Login page"""
    if not config.is_admin_configured():
        return redirect(url_for('setup'))
    if session.get('logged_in'):
        return redirect(url_for('root'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if (username == config.get('ADMIN_USERNAME') and
                check_password_hash(config.get('ADMIN_PASSWORD_HASH', ''), password)):
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('root'))
        return render_template('login.html', error='Invalid username or password')

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Clear session and redirect to login"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def root():
    """Root route - redirect to setup if not configured, otherwise New Migration"""
    if not config.is_configured():
        return redirect(url_for('setup'))
    return redirect(url_for('ocm_index'))


@app.route('/setup', methods=['GET', 'POST'])
@rate_limit(max_calls=10, period_seconds=60, methods=["POST"])
def setup():
    """Setup wizard for first-run configuration"""
    # If admin is already configured, require authentication to access setup
    if config.is_admin_configured() and not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            # Save admin credentials from Step 0 if provided
            admin_username = request.form.get('admin_username', '').strip()
            admin_password = request.form.get('admin_password', '')
            if admin_username and admin_password:
                if len(admin_password) < 8:
                    return render_template('setup.html', error='Password must be at least 8 characters', config=config.get_display_config(), oci_cli_configured=is_oci_cli_configured(), admin_configured=config.is_admin_configured())
                config.set('ADMIN_USERNAME', admin_username)
                config.set('ADMIN_PASSWORD_HASH', generate_password_hash(admin_password))

            updates = {
                'OCM_REGION': request.form.get('ocm_region', 'us-ashburn-1').strip(),
                'OCM_TARGET_COMPARTMENT_OCID': request.form.get('ocm_target_compartment_ocid', '').strip(),
                'OCM_ASSET_SOURCE_OCID': request.form.get('ocm_asset_source_ocid', '').strip(),
                'OCM_REPLICATION_BUCKET_OCID': request.form.get('ocm_replication_bucket_ocid', '').strip(),
            }
            logger.info('Setup wizard saving config: compartment=%s, asset_source=%s, bucket=%s',
                        updates['OCM_TARGET_COMPARTMENT_OCID'][:20] if updates['OCM_TARGET_COMPARTMENT_OCID'] else '(empty)',
                        updates['OCM_ASSET_SOURCE_OCID'][:20] if updates['OCM_ASSET_SOURCE_OCID'] else '(empty)',
                        updates['OCM_REPLICATION_BUCKET_OCID'] or '(empty)')
            config.update(updates)
            config.save_config()

            # Auto-login after setup
            session.permanent = True
            session['logged_in'] = True

            return redirect(url_for('ocm_index'))
        except Exception as e:
            logger.exception('Setup configuration failed')
            return render_template('setup.html', error='Configuration error. Please check your settings and try again.', config=config.get_display_config(), oci_cli_configured=is_oci_cli_configured(), admin_configured=config.is_admin_configured())

    return render_template('setup.html', config=config.get_display_config(), oci_cli_configured=is_oci_cli_configured(), admin_configured=config.is_admin_configured())



def cache_oci_namespace():
    """Background task to fetch and cache OCI Object Storage namespace"""
    try:
        if config.get('OCM_NAMESPACE'):
            return

        # Use a new client instance for thread safety
        object_storage_client = get_oci_client(oci.object_storage.ObjectStorageClient)
        namespace = object_storage_client.get_namespace().data

        config.set('OCM_NAMESPACE', namespace)
        config.save_config()
        logger.info(f"Background: Cached OCI namespace: {namespace}")
    except Exception as e:
        logger.warning(f"Background: Failed to cache OCI namespace: {e}")


@app.route('/api/setup/auth-mode', methods=['GET'])
def get_auth_mode():
    """Return current OCI auth mode so the setup wizard can adapt."""
    from oci_clients import get_auth_mode as _get_auth_mode, get_oci_config as _get_cfg
    mode = _get_auth_mode()
    result = {'auth_mode': mode, 'configured': mode in ('instance_principal', 'config_file')}
    if mode == 'instance_principal':
        try:
            cfg = _get_cfg()
            identity = get_oci_client(oci.identity.IdentityClient)
            tenancy = identity.get_tenancy(cfg['tenancy']).data
            result['tenancy_name'] = tenancy.name
            result['region'] = cfg.get('region', '')
        except Exception as e:
            logger.warning(f'Could not fetch tenancy name for auth-mode: {e}')
    return jsonify(result)


@app.route('/api/setup/oci-config', methods=['GET'])
def setup_oci_config():
    """Test existing ~/.oci/config with a real API call."""
    # Require authentication if admin is already configured
    if config.is_admin_configured() and not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    try:
        from oci_clients import reset_auth, get_auth_mode
        # Force re-detection so a freshly written ~/.oci/config is picked up
        reset_auth()

        no_retry = oci.retry.NoneRetryStrategy()
        identity_client = get_oci_client(oci.identity.IdentityClient, retry_strategy=no_retry)
        oci_cfg = get_oci_config()

        tenancy_id = oci_cfg.get('tenancy', '')
        tenancy = identity_client.get_tenancy(tenancy_id).data

        # Success — start background thread to pre-fetch namespace
        threading.Thread(target=cache_oci_namespace).start()

        return jsonify({'status': 'ok', 'tenancy_name': tenancy.name, 'region': oci_cfg.get('region', '')})
    except oci.exceptions.ConfigFileNotFound:
        return jsonify({'status': 'error', 'message': 'OCI config file not found. Run "oci setup config" first.'})
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            # Tell frontend to retry — new API keys can take minutes to propagate
            logger.info('OCI auth not yet propagated (401), signalling frontend to retry')
            return jsonify({'status': 'retry', 'message': 'API key not yet recognized. New keys can take a few minutes to propagate.'})
        logger.exception('OCI API error during validation')
        return jsonify({'status': 'error', 'message': f'OCI API error: {e.message}'})
    except Exception as e:
        logger.exception('OCI config validation failed')
        return jsonify({'status': 'error', 'message': f'Configuration error: {str(e)}'})


@app.route('/ocm', methods=['GET', 'POST'])
def ocm_index():
    """OCM (Oracle Cloud Migrations) migration page - list VMs from Cloud Bridge and start migrations"""
    import ocm_migration

    # Initialize OCI clients to list available VMs
    try:
        clients = ocm_migration.init_clients()

        # List inventory assets from Cloud Bridge
        compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')
        asset_source_id = config.get('OCM_ASSET_SOURCE_OCID')

        inventory_assets = ocm_migration.list_inventory_assets(
            clients, compartment_id, asset_source_id
        )

        # Filter by source type (VMware or AWS)
        vms = []
        for asset in inventory_assets:
            # Determine source type from asset
            # Note: asset.asset_type may show UNKNOWN_ENUM_VALUE if SDK doesn't recognize it
            # We need to infer from external_asset_key and source_key

            external_key = getattr(asset, 'external_asset_key', '')
            source_key = getattr(asset, 'source_key', '')
            asset_type_str = str(getattr(asset, 'asset_type', 'Unknown'))

            # Skip volumes/disks - they can't be migrated as VMs
            if external_key.startswith('vol-'):
                continue  # Skip AWS EBS volumes

            # Determine source type by examining keys
            source_type = 'Unknown'
            display_type = 'Unknown'

            # Check if it's AWS (instance IDs start with i-, ami-, etc. or source_key contains AWS region)
            if (external_key.startswith('i-') or
                external_key.startswith('ami-') or
                'us-east' in source_key or 'us-west' in source_key or
                'eu-' in source_key or 'ap-' in source_key):
                source_type = 'AWS'
                if external_key.startswith('i-'):
                    display_type = 'AWS EC2 Instance'
                elif external_key.startswith('ami-'):
                    display_type = 'AWS AMI'
                else:
                    display_type = 'AWS Resource'
            # Check if it's VMware
            elif asset_type_str == 'VMWARE_VM' or 'vmware' in asset_type_str.lower():
                source_type = 'VMware'
                display_type = 'VMware VM'
            # Fallback to asset_type if available
            elif asset_type_str != 'UNKNOWN_ENUM_VALUE' and asset_type_str != 'Unknown':
                source_type = asset_type_str
                display_type = asset_type_str

            vms.append({
                'id': asset.id,
                'name': asset.display_name,
                'source_type': source_type,
                'asset_type': display_type,
                'lifecycle_state': asset.lifecycle_state
            })

        # Sort VMs alphabetically by name
        vms.sort(key=lambda x: x['name'].lower())
    except Exception as e:
        vms = []
        # Note: Error will be shown in template if vms is empty

    migrations = OCMMigration.query.order_by(OCMMigration.id.desc()).all()

    if request.method == 'POST':
        # Handle JSON request for multiple VMs
        if request.is_json:
            data = request.get_json()
            vm_list = data.get('vms', [])
            dest_compartment = data.get('destination_compartment')
            dest_vcn = data.get('destination_vcn')
            dest_subnet = data.get('destination_subnet')

            if not vm_list:
                return jsonify({'error': 'No VMs selected'}), 400

            # Check if any VMs are already in active migrations
            try:
                migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)
                compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

                # Get asset IDs from vm_list
                asset_ids = [vm['inventory_asset_id'] for vm in vm_list]

                # Check all existing migrations
                all_migrations = migration_client.list_migrations(compartment_id=compartment_id)
                vms_in_migrations = {}  # Map asset_id -> migration_name

                for mig in all_migrations.data.items:
                    # Skip deleted/deleting/failed migrations
                    lifecycle_state = mig.lifecycle_state if hasattr(mig, 'lifecycle_state') else None
                    if lifecycle_state in ['DELETING', 'DELETED', 'FAILED']:
                        continue

                    # Skip migrations without plans (being prepared for advanced config)
                    has_plan = False
                    try:
                        plans = migration_client.list_migration_plans(compartment_id=compartment_id)
                        for plan in plans.data.items:
                            if hasattr(plan, 'migration_id') and plan.migration_id == mig.id:
                                has_plan = True
                                break
                    except Exception:
                        pass

                    if not has_plan:
                        continue

                    try:
                        mig_assets = migration_client.list_migration_assets(migration_id=mig.id)

                        for ma in mig_assets.data.items:
                            ma_detail = migration_client.get_migration_asset(migration_asset_id=ma.id)

                            if hasattr(ma_detail.data, 'source_asset_id'):
                                source_id = ma_detail.data.source_asset_id

                                if source_id in asset_ids:
                                    vms_in_migrations[source_id] = mig.display_name
                    except Exception:
                        continue

                # If any VMs are in migrations, return error
                if vms_in_migrations:
                    conflicting_vms = []
                    for vm in vm_list:
                        if vm['inventory_asset_id'] in vms_in_migrations:
                            migration_name = vms_in_migrations[vm['inventory_asset_id']]
                            conflicting_vms.append(f"{vm['vm_name']} → {migration_name}")

                    error_msg = f"{len(conflicting_vms)} VM(s) already in active migrations:\n\n" + "\n".join(conflicting_vms)
                    error_msg += "\n\nPlease remove these VMs from your selection or wait for their migrations to complete."

                    return jsonify({'error': error_msg}), 400

            except Exception as e:
                # Log error but don't block migration if check fails
                print(f"Warning: Could not check for migration conflicts: {e}")

            with migration_lock:
                try:
                    # Check total running OCM migrations
                    ocm_running = OCMMigration.query.filter(OCMMigration.status.in_(['Pending', 'Running'])).with_for_update().count()

                    if ocm_running >= MAX_CONCURRENT_MIGRATIONS:
                        db.session.rollback()
                        return jsonify({'error': f'Maximum concurrent migrations limit reached ({MAX_CONCURRENT_MIGRATIONS}). Please wait for a migration to complete.'}), 400

                    # Create ONE batch migration record for all selected VMs
                    is_batch = len(vm_list) > 1
                    primary_vm = vm_list[0]

                    new_migration = OCMMigration(
                        vm_name=primary_vm['vm_name'],
                        source_type=primary_vm['source_type'],
                        asset_source_id=config.get('OCM_ASSET_SOURCE_OCID'),
                        inventory_asset_id=primary_vm['inventory_asset_id'],  # Keep for backward compatibility
                        status='Pending',
                        current_step=1,
                        last_completed_step=0,
                        can_resume=False,
                        is_batch=is_batch,
                        vm_count=len(vm_list),
                        vms_json=json.dumps(vm_list)  # Store all VMs
                    )
                    db.session.add(new_migration)
                    db.session.flush()  # Get the ID
                    migration_id = new_migration.id

                    db.session.commit()

                    # Start ONE migration thread that handles all VMs
                    thread = threading.Thread(
                        target=run_ocm_migration,
                        args=(migration_id, dest_compartment, dest_vcn, dest_subnet)
                    )
                    thread.start()

                    return jsonify({
                        'success': True,
                        'message': f'Migration started for {len(vm_list)} VM(s)',
                        'migration_id': migration_id,
                        'vm_count': len(vm_list)
                    })

                except Exception as e:
                    db.session.rollback()
                    logger.exception('Failed to start batch migrations')
                    return jsonify({'error': 'An internal error occurred'}), 500

        # Handle legacy form POST (single VM)
        else:
            inventory_asset_id = request.form['inventory_asset_id']
            vm_name = request.form['vm_name']
            source_type = request.form['source_type']

            with migration_lock:
                try:
                    # Check total running OCM migrations
                    ocm_running = OCMMigration.query.filter(OCMMigration.status.in_(['Pending', 'Running'])).with_for_update().count()

                    if ocm_running >= MAX_CONCURRENT_MIGRATIONS:
                        db.session.rollback()
                        return jsonify({'error': f'Maximum concurrent migrations limit reached ({MAX_CONCURRENT_MIGRATIONS}). Please wait for a migration to complete.'}), 400

                    # Create new OCM migration record
                    new_migration = OCMMigration(
                        vm_name=vm_name,
                        source_type=source_type,
                        asset_source_id=config.get('OCM_ASSET_SOURCE_OCID'),
                        inventory_asset_id=inventory_asset_id,
                        status='Pending',
                        current_step=1,
                        last_completed_step=0,
                        can_resume=False
                    )
                    db.session.add(new_migration)
                    db.session.commit()

                    # Start migration thread
                    thread = threading.Thread(target=run_ocm_migration, args=(new_migration.id,))
                    thread.start()

                except Exception as e:
                    db.session.rollback()
                    logger.exception('Failed to start single migration')
                    return jsonify({'error': 'An internal error occurred'}), 500

            return redirect(url_for('ocm_index'))

    return render_template('ocm_index.html', vms=vms, migrations=migrations, config=config)


@app.route('/ocm/migration/<int:id>')
def view_ocm_migration(id):
    """View OCM migration details with 6-step progress tracking"""
    migration = OCMMigration.query.get_or_404(id)
    migration_data = {
        'id': migration.id,
        'vm_name': migration.vm_name,
        'source_type': migration.source_type,
        'status': migration.status,
        'logs': migration.logs or '',
        'start_timestamp': migration.start_time.timestamp() * 1000 if migration.start_time else None,
        'end_timestamp': migration.end_time.timestamp() * 1000 if migration.end_time else None,
        'current_step': migration.current_step,
        'last_completed_step': migration.last_completed_step,
        'can_resume': migration.can_resume,
        'project_ocid': migration.project_ocid,
        'plan_ocid': migration.plan_ocid,
        'asset_ocid': migration.asset_ocid,
        'rms_stack_ocid': migration.rms_stack_ocid,
        'rms_job_id': migration.rms_job_id,
        'is_batch': migration.is_batch,
        'vm_count': migration.vm_count,
        'vms': json.loads(migration.vms_json) if migration.vms_json else None,
        # Sidecar test-migration state (awesomeworking engine 2026-04-11)
        'test_status': migration.test_status,
        'test_rms_job_id': migration.test_rms_job_id,
        'test_cleanup_job_id': migration.test_cleanup_job_id,
        'test_start_time': migration.test_start_time.isoformat() if migration.test_start_time else None,
        'test_end_time': migration.test_end_time.isoformat() if migration.test_end_time else None,
        'test_logs': migration.test_logs or '',
        # Sync Now sidecar (2026-04-11) — drives step 4 spinner overlay.
        'sync_status': migration.sync_status,
        'is_scheduled': migration.is_scheduled,
        'schedule_type': migration.schedule_type
    }
    return render_template('ocm_migration_detail.html', migration=migration, migration_data=migration_data, now=datetime.now())


@app.route('/ocm/cancel/<int:id>', methods=['POST'])
def cancel_ocm_migration(id):
    """Cancel a running, pending, or warm migration (In-Sync/Cutting-Over)"""
    try:
        migration = OCMMigration.query.get_or_404(id)

        allowed_states = ['Running', 'Pending', 'In-Sync', 'Cutting-Over']
        if migration.status not in allowed_states:
            return jsonify({
                'success': False,
                'error': f'Migration cannot be cancelled (current status: {migration.status})'
            }), 400

        cancel_logs = '\n\n=== MIGRATION CANCELLED BY USER ===\n'
        cancel_logs += f'Cancellation initiated at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'

        # For warm migrations (In-Sync/Cutting-Over), clean up OCI resources
        if migration.status in ['In-Sync', 'Cutting-Over']:
            cancel_logs += '\nCleaning up OCI resources for warm migration...\n'

            try:
                # Initialize OCI client
                migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)

                # Delete migration assets (stops replication and billing)
                # NOTE: We do NOT delete the replication schedule as it's a shared resource
                asset_ocids = []
                if migration.asset_ocids_json:
                    asset_ocids = json.loads(migration.asset_ocids_json)
                elif migration.asset_ocid:
                    asset_ocids = [{'vm_name': migration.vm_name, 'asset_ocid': migration.asset_ocid}]

                for asset_data in asset_ocids:
                    asset_ocid = asset_data.get('asset_ocid')
                    vm_name = asset_data.get('vm_name', 'Unknown')

                    if asset_ocid:
                        try:
                            cancel_logs += f'  Deleting migration asset for {vm_name}...\n'
                            migration_client.delete_migration_asset(migration_asset_id=asset_ocid)
                            cancel_logs += f'    ✓ Asset deleted: {asset_ocid[:50]}...\n'
                        except oci.exceptions.ServiceError as e:
                            if e.status == 404:
                                cancel_logs += f'    - Asset already deleted or not found\n'
                            else:
                                cancel_logs += f'    ✗ Error deleting asset: {e.message}\n'

                # Note about replication schedule
                if migration.replication_schedule_id:
                    cancel_logs += f'\nNote: Replication schedule retained (shared resource)\n'
                    cancel_logs += f'  Schedule OCID: {migration.replication_schedule_id[:50]}...\n'

            except Exception as e:
                cancel_logs += f'\nWarning: Error during OCI cleanup: {str(e)}\n'
                cancel_logs += 'Migration record will still be marked as Cancelled.\n'

        cancel_logs += '\nMigration cancelled successfully.\n'

        # Mark migration as Cancelled
        migration.status = 'Cancelled'
        migration.end_time = datetime.now()
        migration.logs = (migration.logs or '') + cancel_logs
        db.session.commit()

        return jsonify({'success': True, 'message': 'Migration cancelled'})

    except Exception as e:
        logger.exception('Failed to cancel migration')
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500


@app.route('/ocm/resume/<int:id>', methods=['POST'])
def resume_ocm_migration(id):
    """Resume a failed or interrupted OCM migration"""
    migration = OCMMigration.query.get_or_404(id)

    if migration.status not in ['Failed']:
        return jsonify({'error': 'Migration cannot be resumed (not in Failed state)'}), 400

    if not migration.can_resume:
        return jsonify({'error': 'Migration cannot be resumed (no resumable checkpoint)'}), 400

    with migration_lock:
        try:
            # Check for existing running migrations
            ocm_running = OCMMigration.query.filter(OCMMigration.status.in_(['Pending', 'Running', 'Cutting-Over'])).with_for_update().count()

            if ocm_running > 0:
                db.session.rollback()
                return jsonify({'error': 'A migration is already in progress'}), 400

            # Reset migration status to retry from last completed step
            migration.status = 'Pending'
            migration.end_time = None
            db.session.commit()

            # Start migration thread (will resume from last_completed_step + 1)
            thread = threading.Thread(target=run_ocm_migration, args=(migration.id,))
            thread.start()

        except Exception as e:
            db.session.rollback()
            logger.exception('Failed to resume migration')
            return jsonify({'error': 'An internal error occurred'}), 500

    return redirect(url_for('view_ocm_migration', id=id))


@app.route('/ocm/cutover/<int:id>', methods=['POST'])
def cutover_ocm_migration(id):
    """Trigger cutover for a warm migration that is in In-Sync state"""
    try:
        migration = OCMMigration.query.get_or_404(id)

        if migration.status != 'In-Sync':
            return jsonify({
                'success': False,
                'error': f'Migration is not in a cutover-ready state (current status: {migration.status})'
            }), 400

        # Hard guard: block cutover if a test VM is still live.
        # Awesomeworking sidecar model — test_status tracks the test lifecycle
        # independently of the main status rail.
        if migration.test_status == 'Running':
            return jsonify({
                'success': False,
                'error': 'A test VM is still running. Clean it up before cutover.'
            }), 400
        if migration.test_status in ('Deploying', 'Cleaning Up'):
            return jsonify({
                'success': False,
                'error': f'Test operation in progress ({migration.test_status}); please wait.'
            }), 400

        with migration_lock:
            try:
                # Check for other running migrations
                running_count = OCMMigration.query.filter(
                    OCMMigration.status.in_(['Running', 'Cutting-Over']),
                    OCMMigration.id != id
                ).with_for_update().count()

                if running_count >= MAX_CONCURRENT_MIGRATIONS:
                    db.session.rollback()
                    return jsonify({
                        'success': False,
                        'error': f'Maximum concurrent migrations limit reached ({MAX_CONCURRENT_MIGRATIONS}). Please wait for a migration to complete.'
                    }), 400

                # Update migration status
                migration.status = 'Cutting-Over'
                migration.end_time = None  # Restart the duration timer for cutover phase
                migration.is_scheduled = False  # Prevent pausing again
                migration.logs = (migration.logs or '') + '\n\n' + '='*80 + '\n'
                migration.logs += 'CUTOVER INITIATED BY USER\n'
                migration.logs += '='*80 + '\n'
                migration.logs += f'Cutover started at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
                migration.logs += 'Proceeding with deployment (Steps 5-6)...\n\n'
                db.session.commit()

                # Restart migration thread - it will continue from step 5
                thread = threading.Thread(
                    target=run_ocm_migration,
                    args=(migration.id,)
                )
                thread.start()

                return jsonify({
                    'success': True,
                    'message': 'Cutover initiated. Deployment starting...',
                    'migration_id': migration.id
                })

            except Exception as e:
                db.session.rollback()
                logger.exception('Failed to initiate cutover')
                return jsonify({
                    'success': False,
                    'error': 'An internal error occurred'
                }), 500

    except Exception as e:
        logger.exception('Cutover request failed')
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500


def _monitor_sync_completion(migration_id, assets):
    """
    Background thread: poll replication progress until all assets complete
    or timeout.

    2026-04-11 log overhaul: mirrors step4's work-request-driven logging so
    a Sync Now click shows the same console-matching progress the main
    replication step does. Per-tick WR state is appended to migration.logs
    and committed every tick, so the detail page's 3-second poll picks up
    live progress.
    """
    import ocm_migration

    poll_interval = 30
    timeout = 3600  # 1 hour max
    start_time = time.time()

    def _append_log(line):
        """Commit a single log line to migration.logs, rollback-safe."""
        try:
            m = OCMMigration.query.get(migration_id)
            if m:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                m.logs = (m.logs or '') + f"[{timestamp}] {line}\n"
                db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(f"Failed to append sync log for migration {migration_id}")

    def _clear_sync_status():
        """Unset the sync-in-progress sidecar. Always runs at monitor exit."""
        try:
            m = OCMMigration.query.get(migration_id)
            if m and m.sync_status == 'Running':
                m.sync_status = None
                db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.exception(f"Failed to clear sync_status for migration {migration_id}")

    with app.app_context():
        try:
            migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)

            # Resolve compartment from the first asset. list_work_requests
            # needs it, and all assets in a single migration share it.
            compartment_id = None
            try:
                first_asset_ocid = assets[0]['asset_ocid']
                asset_resp = migration_client.get_migration_asset(first_asset_ocid)
                compartment_id = getattr(asset_resp.data, 'compartment_id', None)
            except Exception as e:
                logger.warning(f"sync monitor: could not resolve compartment: {e}")

            pending = {a['asset_ocid']: a['vm_name'] for a in assets}
            completed = []
            failed = []

            # Per-asset log dedupe key: (vm_name) -> (wr_id, status, int(pct))
            last_log_key = {}
            # Per-asset WR id tracking for phase transition detection.
            last_wr_id = {}
            check_count = 0

            while pending and (time.time() - start_time) < timeout:
                check_count += 1
                elapsed = time.time() - start_time
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

                for asset_ocid, vm_name in list(pending.items()):
                    try:
                        # Primary termination signal: the work request's OWN
                        # status. Not get_replication_progress.last_replication_status,
                        # which is the result of the *previous* completed
                        # cycle and is stale the instant a delta sync starts
                        # against an asset that has ever completed a cycle.
                        #
                        # WR.status transitions ACCEPTED -> IN_PROGRESS ->
                        # SUCCEEDED / FAILED / CANCELED. Terminal states on
                        # the WR are the same thing the OCI Console reads.
                        wr = None
                        if compartment_id:
                            wr = ocm_migration._fetch_active_replication_work_request(
                                migration_client, compartment_id, asset_ocid
                            )

                        if wr is not None:
                            # Phase transition detection.
                            prev_wr = last_wr_id.get(vm_name)
                            if prev_wr is not None and wr['id'] != prev_wr:
                                _append_log(
                                    f"Sync {vm_name}: --- phase transition: new work request "
                                    f"{ocm_migration._short_ocid(wr['id'])} ---"
                                )
                            last_wr_id[vm_name] = wr['id']

                            log_key = (wr['id'], wr['status'], int(wr['percent_complete']))
                            if log_key != last_log_key.get(vm_name):
                                _append_log(
                                    f"Sync {vm_name} | {wr['status']} | "
                                    f"{wr['percent_complete']:.0f}% | "
                                    f"WR {ocm_migration._short_ocid(wr['id'])} | "
                                    f"elapsed {elapsed_str}"
                                )
                                last_log_key[vm_name] = log_key

                            # Terminal WR status = this run is actually done.
                            if wr['status'] == 'SUCCEEDED':
                                _append_log(f"Sync {vm_name}: COMPLETED (elapsed {elapsed_str})")
                                completed.append(vm_name)
                                del pending[asset_ocid]
                                continue
                            elif wr['status'] in ('FAILED', 'CANCELED'):
                                _append_log(
                                    f"Sync {vm_name}: {wr['status']} (elapsed {elapsed_str})"
                                )
                                failed.append(vm_name)
                                del pending[asset_ocid]
                                continue
                            # Non-terminal (ACCEPTED / IN_PROGRESS / WAITING /
                            # NEEDS_ATTENTION / CANCELING): keep polling.

                        else:
                            # Fallback path: couldn't list work requests.
                            # Use get_replication_progress but be careful —
                            # capture last_replication_time at monitor start
                            # and only treat COMPLETED as real if that
                            # timestamp has advanced. On the very first tick
                            # with a stale-cached COMPLETED, this refuses
                            # to declare success in 0s.
                            progress = migration_client.get_replication_progress(
                                migration_asset_id=asset_ocid
                            )
                            status = getattr(progress.data, 'status', 'UNKNOWN')
                            last_status = getattr(progress.data, 'last_replication_status', None)
                            last_repl_time = getattr(progress.data, 'last_replication_time', None)

                            # One-time capture of the baseline replication time.
                            if vm_name not in last_wr_id:
                                last_wr_id[vm_name] = ('__baseline__', last_repl_time)

                            baseline = last_wr_id.get(vm_name)
                            baseline_time = baseline[1] if isinstance(baseline, tuple) else None

                            log_key = ('fallback', status, last_status)
                            if log_key != last_log_key.get(vm_name):
                                _append_log(
                                    f"Sync {vm_name} (fallback) | progress={status} | "
                                    f"last_cycle={last_status} | elapsed {elapsed_str}"
                                )
                                last_log_key[vm_name] = log_key

                            # Only accept terminal last_replication_status
                            # if the timestamp has actually advanced — i.e.
                            # THIS run produced it, not a prior one.
                            time_advanced = (
                                baseline_time is None
                                or (last_repl_time is not None and last_repl_time != baseline_time)
                            )
                            if time_advanced and (last_status == 'COMPLETED' or status == 'COMPLETED'):
                                _append_log(f"Sync {vm_name}: COMPLETED (elapsed {elapsed_str})")
                                completed.append(vm_name)
                                del pending[asset_ocid]
                                continue
                            elif time_advanced and last_status == 'FAILED':
                                _append_log(f"Sync {vm_name}: FAILED (elapsed {elapsed_str})")
                                failed.append(vm_name)
                                del pending[asset_ocid]
                                continue

                    except Exception as e:
                        logger.warning(f"Error polling sync progress for {vm_name}: {e}")

                if pending:
                    time.sleep(poll_interval)

            # Final summary line.
            elapsed = time.time() - start_time
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            summary = f"Manual sync finished ({elapsed_str}): "
            parts = []
            if completed:
                parts.append(f"{len(completed)} completed")
            if failed:
                parts.append(f"{len(failed)} failed")
            if pending:
                parts.append(f"{len(pending)} timed out (still running in OCI)")
            summary += ", ".join(parts) if parts else "no activity"
            _append_log(summary)
            logger.info(
                f"Manual sync for migration {migration_id} finished: "
                f"{len(completed)} completed, {len(failed)} failed, "
                f"{len(pending)} timed out"
            )

        except Exception as e:
            logger.exception(f"Error monitoring sync completion for migration {migration_id}")
            _append_log(f"Manual sync monitoring error: {str(e)}")
        finally:
            # Always clear the sync-in-progress sidecar so the UI spinner
            # stops even if we hit an exception or a timeout.
            _clear_sync_status()


@app.route('/api/ocm/sync-now/<int:id>', methods=['POST'])
def sync_now_ocm_migration(id):
    """
    Trigger immediate delta replication for a warm migration in In-Sync state.
    This initiates a manual sync cycle outside of the scheduled intervals.
    """
    try:
        migration = OCMMigration.query.get_or_404(id)

        if migration.status != 'In-Sync':
            return jsonify({
                'success': False,
                'error': f'Migration is not in In-Sync state (current status: {migration.status}). Only In-Sync migrations can be manually synced.'
            }), 400
        # Block sync while a test VM lifecycle is active — prevents test
        # cleanup and a delta sync from stepping on each other.
        if migration.test_status in ('Deploying', 'Running', 'Cleaning Up'):
            return jsonify({
                'success': False,
                'error': f'Sync is disabled while a test migration is in progress (test_status={migration.test_status}). Complete the test lifecycle first.'
            }), 400

        # Get asset OCIDs
        asset_ocids = []
        if migration.asset_ocids_json:
            asset_ocids = json.loads(migration.asset_ocids_json)
        elif migration.asset_ocid:
            asset_ocids = [{'vm_name': migration.vm_name, 'asset_ocid': migration.asset_ocid}]

        if not asset_ocids:
            return jsonify({
                'success': False,
                'error': 'No migration assets found for this migration'
            }), 400

        # Initialize OCI client
        migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)

        # Track sync results
        sync_results = []
        errors = []

        for asset_data in asset_ocids:
            asset_ocid = asset_data['asset_ocid']
            vm_name = asset_data.get('vm_name', 'Unknown')

            try:
                # Start asset replication (delta sync)
                migration_client.start_asset_replication(
                    migration_asset_id=asset_ocid
                )
                sync_results.append({
                    'vm_name': vm_name,
                    'asset_ocid': asset_ocid,
                    'status': 'initiated'
                })
            except oci.exceptions.ServiceError as e:
                if e.status == 409 and 'ongoing replication' in e.message.lower():
                    # Replication already in progress - not an error
                    sync_results.append({
                        'vm_name': vm_name,
                        'asset_ocid': asset_ocid,
                        'status': 'already_running'
                    })
                else:
                    errors.append({
                        'vm_name': vm_name,
                        'error': f'{e.code}: {e.message}'
                    })

        # Update migration logs — just the "click" line. The background
        # monitor thread appends live per-tick WR progress from here on.
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"\n[{timestamp}] " + "=" * 60 + "\n"
        log_entry += f"[{timestamp}] Manual sync initiated by user\n"
        log_entry += f"[{timestamp}] (Live progress below mirrors the OCI Console Work Requests panel.)\n"
        status_labels = {
            'initiated': 'sync started',
            'already_running': 'sync already in progress (monitoring existing run)',
        }
        for result in sync_results:
            label = status_labels.get(result['status'], result['status'])
            log_entry += f"[{timestamp}]   - {result['vm_name']}: {label}\n"
        for error in errors:
            log_entry += f"[{timestamp}]   - {error['vm_name']}: ERROR - {error['error']}\n"

        migration.logs = (migration.logs or '') + log_entry
        db.session.commit()

        if errors and not sync_results:
            return jsonify({
                'success': False,
                'error': 'Failed to initiate sync',
                'details': errors
            }), 500

        # Monitor sync completion in background thread
        initiated_assets = [r for r in sync_results if r['status'] in ('initiated', 'already_running')]
        if initiated_assets:
            # Set the sync-in-progress sidecar BEFORE spawning the thread so
            # the detail page's next 3-second poll picks it up and starts
            # painting step 4 with a spinner. The monitor clears it on exit.
            migration.sync_status = 'Running'
            db.session.commit()

            thread = threading.Thread(
                target=_monitor_sync_completion,
                args=(migration.id, initiated_assets)
            )
            thread.start()

        return jsonify({
            'success': True,
            'message': f'Delta sync initiated for {len(sync_results)} asset(s)',
            'results': sync_results,
            'errors': errors if errors else None
        })

    except Exception as e:
        logger.exception('Failed to trigger manual sync')
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500


@app.route('/ocm/test-migration/<int:id>', methods=['POST'])
def test_ocm_migration(id):
    """
    Deploy a TEST VM for a migration currently paused in In-Sync state.

    Runs the same steps 5+6 the real cutover runs, but:
    - status stays 'In-Sync' (main rail never advances)
    - output goes into test_logs, not migration.logs
    - test_status tracks the sidecar lifecycle

    User can validate the VM, then call /ocm/cleanup-test-vm/<id> before cutover.

    (Awesomeworking engine transplant 2026-04-11 — replaces the legacy
    /ocm/test-start, /ocm/test-cleanup, /ocm/test-recover trio.)
    """
    try:
        migration = OCMMigration.query.get_or_404(id)

        if migration.status != 'In-Sync':
            return jsonify({
                'success': False,
                'error': f'Migration is not in In-Sync state (current status: {migration.status})'
            }), 400

        # Require a clean slate: no active test, and no un-cleaned failed test
        # (partial Terraform state from a failed APPLY must be DESTROYed first).
        if migration.test_status not in (None, 'Cleaned Up'):
            return jsonify({
                'success': False,
                'error': f'Cannot start a test while test_status is {migration.test_status}. '
                         f'Clean up the previous test first.'
            }), 400

        with migration_lock:
            try:
                running_count = OCMMigration.query.filter(
                    OCMMigration.status.in_(['Running', 'Cutting-Over']),
                    OCMMigration.id != id
                ).with_for_update().count()

                if running_count >= MAX_CONCURRENT_MIGRATIONS:
                    db.session.rollback()
                    return jsonify({
                        'success': False,
                        'error': f'Maximum concurrent migrations limit reached ({MAX_CONCURRENT_MIGRATIONS}).'
                    }), 400

                migration.test_status = 'Deploying'
                migration.test_start_time = datetime.now()
                migration.test_end_time = None
                migration.test_logs = ''
                migration.test_rms_job_id = None
                migration.test_cleanup_job_id = None
                # Test activity is intentionally NOT written to migration.logs —
                # it all lives in migration.test_logs and renders in the test
                # sidecar card. Keeps the main migration log window focused on
                # the real cutover timeline.
                db.session.commit()

                thread = threading.Thread(
                    target=run_test_migration,
                    args=(migration.id,)
                )
                thread.start()

                return jsonify({
                    'success': True,
                    'message': 'Test VM deployment started.',
                    'migration_id': migration.id
                })

            except Exception:
                db.session.rollback()
                logger.exception('Failed to start test migration')
                return jsonify({'success': False, 'error': 'An internal error occurred'}), 500

    except Exception:
        logger.exception('Test migration request failed')
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500


@app.route('/ocm/cleanup-test-vm/<int:id>', methods=['POST'])
def cleanup_test_vm(id):
    """
    Destroy the test VM's Terraform resources (keeps the RMS stack for reapply).
    """
    try:
        migration = OCMMigration.query.get_or_404(id)

        if migration.status != 'In-Sync':
            return jsonify({
                'success': False,
                'error': f'Migration is not in In-Sync state (current status: {migration.status})'
            }), 400

        if migration.test_status not in ('Running', 'Failed'):
            return jsonify({
                'success': False,
                'error': f'No test VM to clean up (test_status={migration.test_status}).'
            }), 400

        migration.test_status = 'Cleaning Up'
        migration.logs = (migration.logs or '') + (
            f"\n[TEST CLEANUP] Cleanup initiated by user at "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        db.session.commit()

        thread = threading.Thread(
            target=run_cleanup_test_vm,
            args=(migration.id,)
        )
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Test VM cleanup started.',
            'migration_id': migration.id
        })

    except Exception:
        logger.exception('Cleanup test VM request failed')
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500


@app.route('/ocm/plan', methods=['GET'])
def ocm_plan():
    """Migration plan configuration page"""
    return render_template('ocm_advanced.html')


@app.route('/api/ocm/prepare-advanced-config', methods=['POST'])
def prepare_advanced_config():
    """
    Create temporary migration project to extract VM specs from source_asset_data
    This is called when user opens Advanced configuration page
    """
    try:
        data = request.get_json()
        asset_ids = data.get('asset_ids', [])

        if not asset_ids:
            return jsonify({'error': 'No asset IDs provided'}), 400

        # Initialize OCI clients
        compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

        migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)
        inventory_client = get_oci_client(oci.cloud_bridge.InventoryClient)
        identity_client = get_oci_client(oci.identity.IdentityClient)

        # Get availability domain
        ads = identity_client.list_availability_domains(compartment_id=compartment_id)
        availability_domain = ads.data[0].name if ads.data else "KoMy:US-ASHBURN-AD-1"

        # Get bucket name
        bucket_name = config.get('OCM_REPLICATION_BUCKET_OCID', 'ocm_replication')

        # First, check which assets are already in migrations
        print("Checking for assets already in migrations...")
        all_migrations = migration_client.list_migrations(compartment_id=compartment_id)

        asset_to_migration = {}  # Map of asset_id -> (migration_id, migration_asset_id, migration_name)
        assets_in_real_migrations = {}  # Map of asset_id -> migration_name
        existing_temp_migration_id = None

        for mig in all_migrations.data.items:
            try:
                # Skip deleted/deleting/failed migrations
                lifecycle_state = mig.lifecycle_state if hasattr(mig, 'lifecycle_state') else None
                print(f"Checking migration '{mig.display_name}' (state: {lifecycle_state})")

                if lifecycle_state in ['DELETING', 'DELETED', 'FAILED']:
                    print(f"  Skipping migration in {lifecycle_state} state")
                    continue

                mig_assets = migration_client.list_migration_assets(migration_id=mig.id)

                for ma in mig_assets.data.items:
                    ma_detail = migration_client.get_migration_asset(migration_asset_id=ma.id)

                    if hasattr(ma_detail.data, 'source_asset_id'):
                        source_id = ma_detail.data.source_asset_id

                        if source_id in asset_ids:
                            # Check if this migration has a plan - if not, it's just being prepared for advanced config
                            has_plan = False
                            try:
                                plans = migration_client.list_migration_plans(compartment_id=compartment_id)
                                for plan in plans.data.items:
                                    if hasattr(plan, 'migration_id') and plan.migration_id == mig.id:
                                        has_plan = True
                                        break
                            except Exception:
                                pass

                            if not has_plan:
                                # Migration without plan - it's being prepared for advanced config
                                asset_to_migration[source_id] = (mig.id, ma.id, mig.display_name)
                                existing_temp_migration_id = mig.id
                                print(f"  Asset {source_id} found in migration being prepared: {mig.display_name}")
                            else:
                                # Asset is in a real migration with a plan - can't use it
                                assets_in_real_migrations[source_id] = mig.display_name
                                print(f"  Asset {source_id} found in ACTIVE real migration: {mig.display_name} (state: {lifecycle_state})")
            except Exception as e:
                print(f"  Error checking migration {mig.display_name}: {e}")
                continue

        # Remove assets that are in real migrations
        available_asset_ids = [aid for aid in asset_ids if aid not in assets_in_real_migrations]

        if assets_in_real_migrations:
            print(f"Warning: {len(assets_in_real_migrations)} assets already in real migrations and will be skipped")

        # Get VM names to build proper project name
        vm_names = []
        for aid in available_asset_ids:
            try:
                inv_asset = inventory_client.get_asset(asset_id=aid)
                vm_names.append(inv_asset.data.display_name)
            except Exception:
                pass

        # Build proper project name (same logic as final migration)
        if len(vm_names) == 1:
            project_name = f"{vm_names[0]}-Project"
        elif len(vm_names) > 1:
            additional_count = len(vm_names) - 1
            project_name = f"{vm_names[0]} (+ {additional_count} Additional)-Project"
        else:
            project_name = f"advanced_config_{int(time.time())}"

        # Check if migration with this name already exists
        existing_migration_id = None
        for mig in all_migrations.data.items:
            if mig.display_name == project_name:
                lifecycle_state = mig.lifecycle_state if hasattr(mig, 'lifecycle_state') else None
                if lifecycle_state not in ['DELETING', 'DELETED', 'FAILED']:
                    existing_migration_id = mig.id
                    print(f"Found existing migration with same name: {project_name}")
                    break

        # Use existing migration if found, otherwise create new one
        if existing_migration_id or existing_temp_migration_id:
            migration_id = existing_migration_id or existing_temp_migration_id
            print(f"Reusing existing migration: {migration_id}")
        else:
            # Create migration project with final name
            print(f"Creating migration project: {project_name}")

            migration_details = oci.cloud_migrations.models.CreateMigrationDetails(
                compartment_id=compartment_id,
                display_name=project_name,
                is_completed=False
            )

            migration_response = migration_client.create_migration(migration_details)
            migration_id = migration_response.data.id

            print(f"Created migration project: {migration_id}")

            # Wait for project to become active
            time.sleep(2)

        asset_specs = {}
        skipped_assets = []

        # Add each asset to the migration to get source_asset_data
        for asset_id in asset_ids:
            try:
                # Skip assets in real migrations
                if asset_id in assets_in_real_migrations:
                    inv_asset = inventory_client.get_asset(asset_id=asset_id)
                    asset_name = inv_asset.data.display_name
                    migration_name = assets_in_real_migrations[asset_id]
                    skipped_assets.append({
                        'asset_id': asset_id,
                        'name': asset_name,
                        'migration_name': migration_name,
                        'reason': f'Already in migration: {migration_name}'
                    })
                    print(f"Skipping {asset_name} - already in migration: {migration_name}")
                    continue

                # Get inventory asset first
                inv_asset = inventory_client.get_asset(asset_id=asset_id)
                asset_name = inv_asset.data.display_name

                print(f"Processing asset {asset_name}...")

                # Check if we already have this asset in a migration
                migration_asset_id = None
                source_data = None

                if asset_id in asset_to_migration:
                    # Asset already exists in temp migration - reuse it
                    _, migration_asset_id, _ = asset_to_migration[asset_id]
                    print(f"Reusing existing migration asset: {migration_asset_id}")
                else:
                    # Need to create new migration asset
                    try:
                        # Try to create migration asset
                        migration_asset_details = oci.cloud_migrations.models.CreateMigrationAssetDetails(
                            inventory_asset_id=asset_id,
                            migration_id=migration_id,
                            availability_domain=availability_domain,
                            replication_compartment_id=compartment_id,
                            snap_shot_bucket_name=bucket_name
                        )

                        migration_asset_response = migration_client.create_migration_asset(migration_asset_details)
                        migration_asset_id = migration_asset_response.data.id

                        print(f"Created new migration asset: {migration_asset_id}")

                        # Wait for asset to be created
                        time.sleep(1)

                    except oci.exceptions.ServiceError as e:
                        if e.status == 409:  # Already exists in another migration
                            print(f"Asset already in a migration, searching for existing migration asset...")

                            # Search for existing migration asset with this inventory asset ID
                            # List all migrations in compartment
                            all_migrations2 = migration_client.list_migrations(compartment_id=compartment_id)

                            for mig in all_migrations2.data.items:
                                try:
                                    # List migration assets in this migration
                                    mig_assets = migration_client.list_migration_assets(migration_id=mig.id)

                                    for ma in mig_assets.data.items:
                                        # Check if this migration asset references our inventory asset
                                        ma_detail = migration_client.get_migration_asset(migration_asset_id=ma.id)

                                        if hasattr(ma_detail.data, 'source_asset_id') and ma_detail.data.source_asset_id == asset_id:
                                            migration_asset_id = ma.id
                                            print(f"Found existing migration asset: {migration_asset_id}")
                                            break

                                    if migration_asset_id:
                                        break
                                except Exception:
                                    continue

                            if not migration_asset_id:
                                raise Exception("Asset in another migration but couldn't find it")
                        else:
                            raise

                # Get migration asset details with source_asset_data
                migration_asset = migration_client.get_migration_asset(migration_asset_id=migration_asset_id)
                source_data = migration_asset.data.source_asset_data

                # Extract specs from source_asset_data
                specs = {
                    'display_name': asset_name,
                    'asset_type': inv_asset.data.asset_type,
                    'external_key': inv_asset.data.external_asset_key if hasattr(inv_asset.data, 'external_asset_key') else None,
                    'specs_available': False
                }

                if source_data and isinstance(source_data, dict):
                    # Check for compute specs
                    if 'compute' in source_data:
                        compute = source_data['compute']
                        specs['cpu_count'] = compute.get('coresCount')
                        specs['memory_mb'] = compute.get('memoryInMBs')
                        specs['operating_system'] = compute.get('operatingSystem')
                        specs['specs_available'] = True

                        # Determine source type
                        if 'awsEc2' in source_data:
                            specs['source'] = 'aws'
                            specs['instance_type'] = source_data['awsEc2'].get('instanceType')
                        elif 'vm' in source_data:
                            specs['source'] = 'vmware'
                        else:
                            specs['source'] = 'unknown'

                        print(f"Extracted specs for {asset_name}: CPU={specs['cpu_count']}, Memory={specs['memory_mb']}MB, Type={specs.get('instance_type', 'N/A')}")
                    else:
                        specs['note'] = 'No compute specs in source_asset_data'
                        print(f"No compute specs for {asset_name}")
                else:
                    specs['note'] = 'No source_asset_data available'
                    print(f"No source_asset_data for {asset_name}")

                asset_specs[asset_id] = specs

            except Exception as e:
                logger.exception('Error processing asset %s', asset_id)
                asset_specs[asset_id] = {
                    'specs_available': False,
                    'error': 'Failed to retrieve asset details'
                }

        # Store temp migration ID in session or return it
        response = {
            'assets': asset_specs,
            'temp_migration_id': migration_id,
            'skipped_assets': skipped_assets  # Assets already in real migrations
        }

        return jsonify(response)

    except Exception as e:
        logger.exception('Error in prepare_advanced_config')
        return jsonify({'error': 'An internal error occurred'}), 500


@app.route('/api/ocm/delete-temp-migration', methods=['POST'])
def delete_temp_migration():
    """
    Delete temporary migration project created for Advanced config
    Called when user cancels/navigates away from Advanced page
    Supports both regular fetch and navigator.sendBeacon requests
    """
    try:
        # Handle both regular JSON and sendBeacon requests
        if request.is_json:
            data = request.get_json()
        else:
            # sendBeacon might send as text/plain or application/octet-stream
            data = json.loads(request.data.decode('utf-8'))

        migration_id = data.get('migration_id')

        if not migration_id:
            return jsonify({'error': 'No migration ID provided'}), 400

        # Initialize OCI client
        migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)

        # Verify it's a temp migration before deleting
        try:
            migration = migration_client.get_migration(migration_id=migration_id)

            if not migration.data.display_name.startswith('temp_advanced_config_'):
                print(f"Skipping deletion - not a temp migration: {migration.data.display_name}")
                return jsonify({'error': 'Can only delete temp migrations'}), 403

            print(f"Deleting temp migration: {migration_id} ({migration.data.display_name})")

            # Delete the migration (this will also delete migration assets)
            migration_client.delete_migration(migration_id=migration_id)

            print(f"Temp migration deleted successfully")

            return jsonify({'success': True})

        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                # Migration already deleted - that's OK
                print(f"Temp migration already deleted: {migration_id}")
                return jsonify({'success': True, 'note': 'Already deleted'})
            else:
                raise

    except Exception as e:
        logger.exception('Error deleting temp migration')
        return jsonify({'error': 'An internal error occurred'}), 500


@app.route('/ocm/start-advanced-migration', methods=['POST'])
def start_advanced_migration():
    """Start OCM migration with advanced configuration"""
    try:
        data = request.get_json()
        vm_list = data.get('vms', [])
        migration_name = data.get('migration_name', '').strip()
        dest_compartment = data.get('destination_compartment')
        dest_vcn = data.get('destination_vcn')
        dest_subnet = data.get('destination_subnet')
        vm_config = data.get('vm_config', {})
        is_scheduled = data.get('is_scheduled', False)  # Warm migration flag
        schedule_type = data.get('schedule_type', 'IMMEDIATE')  # IMMEDIATE, ONCE, DAILY, or WEEKLY
        start_hour = data.get('start_hour', 2)  # Start hour for DAILY/WEEKLY schedules (0-23 UTC)

        if not vm_list:
            return jsonify({'error': 'No VMs selected'}), 400

        if not dest_compartment or not dest_vcn or not dest_subnet:
            return jsonify({'error': 'Destination compartment, VCN, and subnet are required'}), 400

        # Check if any VMs are already in active migrations
        try:
            migration_client = get_oci_client(oci.cloud_migrations.MigrationClient)
            compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

            # Get asset IDs from vm_list
            asset_ids = [vm['inventory_asset_id'] for vm in vm_list]

            # Check all existing migrations
            all_migrations = migration_client.list_migrations(compartment_id=compartment_id)
            vms_in_migrations = {}  # Map asset_id -> migration_name

            for mig in all_migrations.data.items:
                # Skip deleted/deleting/failed migrations
                lifecycle_state = mig.lifecycle_state if hasattr(mig, 'lifecycle_state') else None
                if lifecycle_state in ['DELETING', 'DELETED', 'FAILED']:
                    continue

                # Skip migrations without plans (being prepared for advanced config)
                has_plan = False
                try:
                    plans = migration_client.list_migration_plans(compartment_id=compartment_id)
                    for plan in plans.data.items:
                        if hasattr(plan, 'migration_id') and plan.migration_id == mig.id:
                            has_plan = True
                            break
                except Exception:
                    pass

                if not has_plan:
                    continue

                try:
                    mig_assets = migration_client.list_migration_assets(migration_id=mig.id)

                    for ma in mig_assets.data.items:
                        ma_detail = migration_client.get_migration_asset(migration_asset_id=ma.id)

                        if hasattr(ma_detail.data, 'source_asset_id'):
                            source_id = ma_detail.data.source_asset_id

                            if source_id in asset_ids:
                                vms_in_migrations[source_id] = mig.display_name
                except Exception:
                    continue

            # If any VMs are in migrations, return error
            if vms_in_migrations:
                conflicting_vms = []
                for vm in vm_list:
                    if vm['inventory_asset_id'] in vms_in_migrations:
                        migration_name = vms_in_migrations[vm['inventory_asset_id']]
                        conflicting_vms.append(f"{vm['vm_name']} → {migration_name}")

                error_msg = f"{len(conflicting_vms)} VM(s) already in active migrations:\n\n" + "\n".join(conflicting_vms)
                error_msg += "\n\nPlease remove these VMs from your selection or wait for their migrations to complete."

                return jsonify({'error': error_msg}), 400

        except Exception as e:
            # Log error but don't block migration if check fails
            print(f"Warning: Could not check for migration conflicts: {e}")

        with migration_lock:
            try:
                # Check total running OCM migrations
                ocm_running = OCMMigration.query.filter(OCMMigration.status.in_(['Pending', 'Running'])).with_for_update().count()

                if ocm_running >= MAX_CONCURRENT_MIGRATIONS:
                    db.session.rollback()
                    return jsonify({'error': f'Maximum concurrent migrations limit reached ({MAX_CONCURRENT_MIGRATIONS}). Please wait for a migration to complete.'}), 400

                # Create batch migration record with vm_config_json
                is_batch = len(vm_list) > 1
                primary_vm = vm_list[0]

                new_migration = OCMMigration(
                    vm_name=migration_name or primary_vm['vm_name'],
                    source_type=primary_vm['source_type'],
                    asset_source_id=config.get('OCM_ASSET_SOURCE_OCID'),
                    inventory_asset_id=primary_vm['inventory_asset_id'],
                    status='Pending',
                    current_step=1,
                    last_completed_step=0,
                    can_resume=False,
                    is_batch=is_batch,
                    vm_count=len(vm_list),
                    vms_json=json.dumps(vm_list),
                    vm_config_json=json.dumps(vm_config),  # Store advanced configuration
                    is_scheduled=is_scheduled,  # Warm migration flag (true for ONCE, DAILY, WEEKLY)
                    schedule_type=schedule_type,  # IMMEDIATE, ONCE, DAILY, or WEEKLY
                    start_hour=start_hour  # Start hour for DAILY/WEEKLY schedules (0-23 UTC)
                )
                db.session.add(new_migration)
                db.session.flush()  # Get the ID
                migration_id = new_migration.id

                db.session.commit()

                # Start migration thread
                thread = threading.Thread(
                    target=run_ocm_migration,
                    args=(migration_id, dest_compartment, dest_vcn, dest_subnet)
                )
                thread.start()

                return jsonify({
                    'success': True,
                    'message': f'Advanced migration started for {len(vm_list)} VM(s)',
                    'migration_id': migration_id,
                    'vm_count': len(vm_list)
                })

            except Exception as e:
                db.session.rollback()
                logger.exception('Failed to start advanced migration')
                return jsonify({'error': 'An internal error occurred'}), 500

    except Exception as e:
        logger.exception('Invalid advanced migration request')
        return jsonify({'error': 'Invalid request'}), 400




@app.route('/api/ocm/get-asset-details', methods=['POST'])
def get_asset_details():
    """Get detailed asset info - VMware VMs have full specs, AWS VMs use instance type lookup"""
    try:
        from asset_specs_extractor import get_vm_compute_specs, get_aws_specs_from_instance_type

        data = request.get_json()
        asset_ids = data.get('asset_ids', [])

        if not asset_ids:
            return jsonify({'error': 'No asset IDs provided'}), 400

        # Use Cloud Bridge InventoryClient
        inventory_client = get_oci_client(oci.cloud_bridge.InventoryClient)

        asset_details = {}

        for asset_id in asset_ids:
            try:
                # Use the improved extraction logic from asset_specs_extractor
                vm_specs = get_vm_compute_specs(inventory_client, asset_id)

                if vm_specs is None:
                    # Not a VM (e.g., volume)
                    asset_details[asset_id] = {
                        'specs_available': False,
                        'error': 'Not a VM asset (possibly a volume or other resource)'
                    }
                    continue

                # Convert to API response format
                specs = {
                    'display_name': vm_specs.get('name'),
                    'source': vm_specs.get('source_type', 'unknown').lower(),
                    'cpu_count': vm_specs.get('cpu_count'),
                    'memory_mb': int(vm_specs.get('memory_gb', 0) * 1024) if vm_specs.get('memory_gb') else None,
                    'operating_system': vm_specs.get('operating_system'),
                    'instance_type': vm_specs.get('instance_type'),
                    'specs_available': vm_specs.get('cpu_count') is not None and vm_specs.get('memory_gb') is not None,
                    'specs_from_lookup': vm_specs.get('specs_from_lookup', False)
                }

                # Add note for AWS VMs if specs came from lookup
                if specs['source'] == 'aws':
                    if specs['specs_from_lookup']:
                        specs['note'] = f"Specs derived from AWS instance type: {specs['instance_type']}"
                    elif not specs['specs_available']:
                        specs['note'] = 'AWS VM specs not available (unknown instance type)'

                asset_details[asset_id] = specs

            except Exception as e:
                logger.exception('Error fetching details for asset %s', asset_id)
                asset_details[asset_id] = {
                    'specs_available': False,
                    'error': 'Failed to retrieve asset details'
                }

        return jsonify({'assets': asset_details})

    except Exception as e:
        logger.exception('Failed to get asset details')
        return jsonify({'error': 'An internal error occurred'}), 500


def _utc_iso(dt):
    """Format a naive datetime as an ISO string with Z suffix so browsers
    parse it as UTC, not local time. Without the Z, JS Date.parse interprets
    the timestamp as the browser's local timezone, which breaks relative-time
    display for users behind UTC (the age goes negative → "just now")."""
    return (dt.isoformat() + 'Z') if dt else None


def _migration_to_dashboard_row(m):
    """Serialize a migration row for the dashboard UI (both server and JSON)."""
    return {
        'id': m.id,
        'vm_name': m.vm_name,
        'source_type': m.source_type,
        'status': m.status,
        'current_step': m.current_step,
        'last_completed_step': m.last_completed_step,
        'is_batch': bool(m.is_batch),
        'vm_count': m.vm_count,
        'test_status': m.test_status,
        'sync_status': m.sync_status,
        'start_time': _utc_iso(m.start_time),
        'end_time':   _utc_iso(m.end_time),
        'progress_pct': min(100, round((m.current_step or 0) / 6 * 100)),
    }


def _build_dashboard_data():
    """
    Compute the full command-center dashboard payload in one pass.

    Shared by the server-rendered /dashboard route and the JSON auto-refresh
    endpoint /api/ocm/dashboard so both always agree on what the user sees.

    Returns a dict with:
      kpis               — Total / Running / In-Sync / Completed / Failed / success_rate_pct
      active_migrations  — rows currently running or with an active sidecar
      attention          — failed migrations (for the Needs Attention panel)
      recent             — last 8 migrations (for the compact table)
      recent_activity    — last 10 derived events (started/completed/failed)
      trend              — 14-day daily series of started/completed/failed
      status_breakdown   — {label, count} for the donut chart
      source_breakdown   — {label, count} for the bar chart
      generated_at       — server time, ISO, for freshness check
    """
    migrations = OCMMigration.query.order_by(OCMMigration.start_time.desc()).all()

    total     = len(migrations)
    running   = sum(1 for m in migrations if m.status in ('Pending', 'Running', 'Cutting-Over'))
    in_sync   = sum(1 for m in migrations if m.status == 'In-Sync')
    completed = sum(1 for m in migrations if m.status == 'Completed')
    failed    = sum(1 for m in migrations if m.status == 'Failed')
    cancelled = sum(1 for m in migrations if m.status == 'Cancelled')

    # Success rate over migrations that have reached a terminal outcome.
    # None when no migrations have finished — the UI shows "—" in that case.
    finished = completed + failed
    success_rate_pct = round((completed / finished) * 100) if finished else None

    # Active: anything the user might want to act on — running work plus
    # In-Sync migrations waiting for user input (cutover, test, sync).
    def is_active(m):
        return (
            m.status in ('Pending', 'Running', 'Cutting-Over', 'In-Sync') or
            m.test_status in ('Deploying', 'Cleaning Up') or
            m.sync_status == 'Running'
        )
    active_migs = [m for m in migrations if is_active(m)]
    attention_migs = [m for m in migrations if m.status == 'Failed'][:10]
    # All migrations are returned for the dashboard's embedded Migration
    # History table — it filters and sorts client-side based on the active
    # KPI card and search input. Dashboard poll payload is still small
    # because each migration serializes to a few hundred bytes.
    recent_migs = migrations

    # 14-day trend — bucket start_time / end_time into days.
    today = datetime.now().date()
    day_list = [today - timedelta(days=i) for i in range(13, -1, -1)]  # oldest -> newest
    started_by   = {d.isoformat(): 0 for d in day_list}
    completed_by = {d.isoformat(): 0 for d in day_list}
    failed_by    = {d.isoformat(): 0 for d in day_list}
    trend_cutoff = datetime.combine(day_list[0], datetime.min.time())
    for m in migrations:
        if m.start_time and m.start_time >= trend_cutoff:
            k = m.start_time.date().isoformat()
            if k in started_by:
                started_by[k] += 1
        if m.end_time and m.end_time >= trend_cutoff:
            k = m.end_time.date().isoformat()
            if k in completed_by:
                if m.status == 'Completed':
                    completed_by[k] += 1
                elif m.status == 'Failed':
                    failed_by[k] += 1

    trend = {
        'labels':    [d.isoformat() for d in day_list],
        'started':   [started_by[d.isoformat()]   for d in day_list],
        'completed': [completed_by[d.isoformat()] for d in day_list],
        'failed':    [failed_by[d.isoformat()]    for d in day_list],
    }

    # Breakdowns for the donut + bar charts.
    status_counts = {}
    source_counts = {}
    for m in migrations:
        status_counts[m.status]       = status_counts.get(m.status, 0) + 1
        source_counts[m.source_type]  = source_counts.get(m.source_type, 0) + 1
    status_breakdown = [{'label': k, 'count': v} for k, v in
                        sorted(status_counts.items(), key=lambda kv: -kv[1])]
    source_breakdown = [{'label': k, 'count': v} for k, v in
                        sorted(source_counts.items(), key=lambda kv: -kv[1])]

    # Activity feed — derive events from start_time/end_time transitions.
    # We don't have structured per-step events; these are the useful ones.
    events = []
    for m in migrations:
        if m.start_time:
            events.append({
                'timestamp': _utc_iso(m.start_time),
                'vm_name': m.vm_name,
                'event': 'started',
                'status': m.status,
                'id': m.id,
                'step': None,
            })
        if m.end_time and m.status == 'Completed':
            events.append({
                'timestamp': _utc_iso(m.end_time),
                'vm_name': m.vm_name,
                'event': 'completed',
                'status': m.status,
                'id': m.id,
                'step': 6,
            })
        elif m.end_time and m.status == 'Failed':
            events.append({
                'timestamp': _utc_iso(m.end_time),
                'vm_name': m.vm_name,
                'event': 'failed',
                'status': m.status,
                'id': m.id,
                'step': m.current_step,
            })
    events.sort(key=lambda e: e['timestamp'], reverse=True)
    events = events[:10]

    return {
        'kpis': {
            'total':     total,
            'running':   running,
            'in_sync':   in_sync,
            'completed': completed,
            'failed':    failed,
            'cancelled': cancelled,
            'success_rate_pct': success_rate_pct,
        },
        'active_migrations':    [_migration_to_dashboard_row(m) for m in active_migs],
        'attention_migrations': [_migration_to_dashboard_row(m) for m in attention_migs],
        'migrations':           [_migration_to_dashboard_row(m) for m in recent_migs],
        'recent_activity':      events,
        'trend':                trend,
        'status_breakdown':     status_breakdown,
        'source_breakdown':     source_breakdown,
        'generated_at':         datetime.now().isoformat(),
    }


@app.route('/dashboard')
def dashboard():
    """Command center dashboard — KPI cards + live monitoring + charts + activity feed."""
    data = _build_dashboard_data()
    return render_template('dashboard.html', data=data)


@app.route('/api/ocm/dashboard')
def api_dashboard():
    """JSON version of the dashboard payload, used by the page's auto-refresh poller."""
    return jsonify(_build_dashboard_data())


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """Settings page for OCM configuration"""
    if request.method == 'POST':
        try:
            # Get form data
            updates = {
                # OCM settings
                'OCM_REGION': request.form.get('ocm_region', 'us-ashburn-1').strip(),
                'OCM_TARGET_COMPARTMENT_OCID': request.form.get('ocm_target_compartment_ocid', '').strip(),
                'OCM_ASSET_SOURCE_OCID': request.form.get('ocm_asset_source_ocid', '').strip(),
                'OCM_REPLICATION_BUCKET_OCID': request.form.get('ocm_replication_bucket_ocid', '').strip(),
                'OCM_POLL_INTERVAL_SECONDS': request.form.get('ocm_poll_interval_seconds', '30').strip(),

                # Destination network settings
                'DEFAULT_DEST_COMPARTMENT': request.form.get('default_dest_compartment', '').strip(),
                'DEFAULT_DEST_VCN': request.form.get('default_dest_vcn', '').strip(),
                'DEFAULT_DEST_SUBNET': request.form.get('default_dest_subnet', '').strip(),
            }

            # Update configuration
            config.update(updates)

            # Save to file
            if config.save_config():
                return render_template('settings.html',
                                     config=config.get_display_config(),
                                     message='Settings saved successfully!',
                                     success=True)
            else:
                return render_template('settings.html',
                                     config=config.get_display_config(),
                                     message='Error saving settings to file.',
                                     success=False)

        except Exception as e:
            logger.exception('Error updating settings')
            return render_template('settings.html',
                                 config=config.get_display_config(),
                                 message='Error updating settings. Please check your inputs and try again.',
                                 success=False)

    # GET request - show settings form
    return render_template('settings.html', config=config.get_display_config())


@app.route('/about')
def about():
    """About ExpressLane page"""
    return render_template('about.html')


# OCM API Routes
@app.route('/api/ocm/migrations', methods=['GET'])
def get_ocm_migrations():
    """Get list of all OCM migrations"""
    migrations = OCMMigration.query.order_by(OCMMigration.id.desc()).all()
    return jsonify([{
        'id': m.id,
        'vm_name': m.vm_name,
        'source_type': m.source_type,
        'status': m.status,
        'current_step': m.current_step,
        'start_time': m.start_time.isoformat() if m.start_time else None,
        'end_time': m.end_time.isoformat() if m.end_time else None,
        'can_resume': m.can_resume,
        'is_batch': m.is_batch,
        'vm_count': m.vm_count,
        # Sidecar test lifecycle — surfaced for the dashboard's "Test: <status>"
        # badge, which is only rendered while main status is 'In-Sync'.
        'test_status': m.test_status,
    } for m in migrations])


@app.route('/api/ocm/migration/<int:id>', methods=['GET'])
def get_ocm_migration(id):
    """Get details of a specific OCM migration"""
    migration = OCMMigration.query.get_or_404(id)
    return jsonify({
        'id': migration.id,
        'vm_name': migration.vm_name,
        'source_type': migration.source_type,
        'status': migration.status,
        'logs': migration.logs or '',
        'start_time': migration.start_time.isoformat() if migration.start_time else None,
        'end_time': migration.end_time.isoformat() if migration.end_time else None,
        'current_step': migration.current_step,
        'last_completed_step': migration.last_completed_step,
        'can_resume': migration.can_resume,
        'project_ocid': migration.project_ocid,
        'plan_ocid': migration.plan_ocid,
        'asset_ocid': migration.asset_ocid,
        'rms_stack_ocid': migration.rms_stack_ocid,
        'rms_job_id': migration.rms_job_id,
        'is_batch': migration.is_batch,
        'vm_count': migration.vm_count,
        'vms': json.loads(migration.vms_json) if migration.vms_json else None,
        # Sidecar test-migration state (awesomeworking engine 2026-04-11).
        # Legacy V1.6 test fields removed per merge-plan decision.
        'test_status': migration.test_status,
        'test_rms_job_id': migration.test_rms_job_id,
        'test_cleanup_job_id': migration.test_cleanup_job_id,
        'test_start_time': migration.test_start_time.isoformat() if migration.test_start_time else None,
        'test_end_time': migration.test_end_time.isoformat() if migration.test_end_time else None,
        'test_logs': migration.test_logs or '',
        # Sync Now sidecar (2026-04-11) — drives step 4 spinner overlay.
        'sync_status': migration.sync_status,
    })


# OCM Configuration API Endpoints
@app.route('/api/ocm/compartments', methods=['GET'])
def get_compartments():
    """Get list of compartments for OCM configuration"""
    cache_key = 'compartments'
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        identity_client = get_oci_client(oci.identity.IdentityClient)

        # Get tenancy from config
        tenancy_id = get_oci_config().get('tenancy', '')

        # List all compartments
        compartments = identity_client.list_compartments(
            tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE"
        ).data

        # Add root compartment
        root_compartment = identity_client.get_compartment(tenancy_id).data

        compartment_list = [{
            'id': root_compartment.id,
            'name': root_compartment.name + ' (root)',
            'description': root_compartment.description
        }]

        for comp in compartments:
            if comp.lifecycle_state == 'ACTIVE':
                compartment_list.append({
                    'id': comp.id,
                    'name': comp.name,
                    'description': comp.description
                })

        result = {'compartments': compartment_list}
        _cache_set(cache_key, result)
        return jsonify(result)
    except oci.exceptions.ServiceError as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list compartments: returning stale cache after OCI error')
            return jsonify(stale)
        return _oci_error_response(e, 'list compartments')
    except Exception as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list compartments: returning stale cache after internal error')
            return jsonify(stale)
        logger.exception('Failed to list compartments')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/vcns', methods=['GET'])
def get_vcns():
    """Get list of VCNs in a compartment"""
    try:
        compartment_id = request.args.get('compartment_id')
        region = request.args.get('region', config.get('OCM_REGION', 'us-ashburn-1'))

        if not compartment_id:
            return jsonify({'error': 'compartment_id required'}), 400

        network_client = get_oci_client(oci.core.VirtualNetworkClient, region=region)

        vcns = network_client.list_vcns(compartment_id=compartment_id).data

        vcn_list = []
        for vcn in vcns:
            if vcn.lifecycle_state == 'AVAILABLE':
                vcn_list.append({
                    'id': vcn.id,
                    'name': vcn.display_name,
                    'cidr_block': vcn.cidr_block
                })

        return jsonify({'vcns': vcn_list})
    except oci.exceptions.ServiceError as e:
        return _oci_error_response(e, 'list VCNs')
    except Exception as e:
        logger.exception('Failed to list VCNs')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/subnets', methods=['GET'])
def get_subnets():
    """Get list of subnets in a VCN"""
    try:
        compartment_id = request.args.get('compartment_id')
        vcn_id = request.args.get('vcn_id')
        region = request.args.get('region', config.get('OCM_REGION', 'us-ashburn-1'))

        if not compartment_id or not vcn_id:
            return jsonify({'error': 'compartment_id and vcn_id required'}), 400

        network_client = get_oci_client(oci.core.VirtualNetworkClient, region=region)

        subnets = network_client.list_subnets(
            compartment_id=compartment_id,
            vcn_id=vcn_id
        ).data

        subnet_list = []
        for subnet in subnets:
            if subnet.lifecycle_state == 'AVAILABLE':
                subnet_list.append({
                    'id': subnet.id,
                    'name': subnet.display_name,
                    'cidr_block': subnet.cidr_block,
                    'availability_domain': subnet.availability_domain
                })

        return jsonify({'subnets': subnet_list})
    except oci.exceptions.ServiceError as e:
        return _oci_error_response(e, 'list subnets')
    except Exception as e:
        logger.exception('Failed to list subnets')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/buckets', methods=['GET'])
def get_buckets():
    """Get list of object storage buckets"""
    compartment_id = request.args.get('compartment_id')
    region = request.args.get('region', config.get('OCM_REGION', 'us-ashburn-1'))

    if not compartment_id:
        return jsonify({'error': 'compartment_id required'}), 400

    cache_key = ('buckets', compartment_id, region)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        object_storage_client = get_oci_client(oci.object_storage.ObjectStorageClient, region=region)

        # namespace is immutable for a tenancy, so we can cache it
        namespace = config.get('OCM_NAMESPACE')
        if not namespace:
            namespace = object_storage_client.get_namespace().data
            config.set('OCM_NAMESPACE', namespace)
            config.save_config()

        buckets = object_storage_client.list_buckets(
            namespace_name=namespace,
            compartment_id=compartment_id
        ).data

        bucket_list = []
        for bucket in buckets:
            bucket_list.append({
                'name': bucket.name,
                'namespace': namespace
            })

        result = {'buckets': bucket_list}
        _cache_set(cache_key, result)
        return jsonify(result)
    except oci.exceptions.ServiceError as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list buckets: returning stale cache after OCI error')
            return jsonify(stale)
        return _oci_error_response(e, 'list buckets')
    except Exception as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list buckets: returning stale cache after internal error')
            return jsonify(stale)
        logger.exception('Failed to list buckets')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/work-request-status/<work_request_id>', methods=['GET'])
def get_work_request_status(work_request_id):
    """Get the status of a Cloud Bridge work request"""
    try:
        # Use CommonClient to get work request status
        common_client = get_oci_client(oci.cloud_bridge.CommonClient)

        # Get work request details
        work_request = common_client.get_work_request(work_request_id=work_request_id).data

        return jsonify({
            'status': work_request.status,
            'percent_complete': work_request.percent_complete,
            'time_started': work_request.time_started.isoformat() if work_request.time_started else None,
            'time_finished': work_request.time_finished.isoformat() if work_request.time_finished else None
        })

    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return jsonify({'error': f'Work request not found: {work_request_id}'}), 404
        elif e.status == 401 or (e.status == 400 and 'SignatureNotValid' in str(e.message)):
            return _oci_error_response(e, 'get work request status')
        else:
            return jsonify({'error': f'OCI Error: {e.message}'}), e.status
    except Exception as e:
        logger.exception('Failed to get work request status')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/refresh-asset-source', methods=['POST'])
def refresh_asset_source():
    """Trigger a discovery refresh for an asset source (VMware or AWS)"""
    try:
        data = request.get_json()
        asset_source_id = data.get('asset_source_id')

        if not asset_source_id:
            return jsonify({'error': 'asset_source_id is required'}), 400

        # Initialize Discovery client
        discovery_client = get_oci_client(oci.cloud_bridge.DiscoveryClient)

        # Trigger refresh
        response = discovery_client.refresh_asset_source(asset_source_id=asset_source_id)

        # Get work request ID from response headers
        work_request_id = response.headers.get('opc-work-request-id')

        return jsonify({
            'success': True,
            'message': 'Discovery refresh started',
            'work_request_id': work_request_id
        })

    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            logger.warning(f'refresh_asset_source 404: code={e.code} message={e.message}')
            if 'NotAuthorizedOrNotFound' in str(e.code):
                return jsonify({'error': 'Not authorized to refresh this asset source. '
                                         'Check that the dynamic group policy includes '
                                         '"manage ocb-asset-source-connectors".'}), 403
            return jsonify({'error': f'Asset source not found: {asset_source_id}'}), 404
        elif e.status == 409:
            return jsonify({'error': 'A refresh is already in progress. Please wait for it to complete.'}), 409
        elif e.status == 401 or (e.status == 400 and 'SignatureNotValid' in str(e.message)):
            return _oci_error_response(e, 'refresh asset source')
        else:
            return jsonify({'error': f'OCI Error: {e.message}'}), e.status
    except Exception as e:
        logger.exception('Failed to refresh asset source')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


@app.route('/api/ocm/asset-sources', methods=['GET'])
def get_asset_sources():
    """Get list of Cloud Bridge asset sources"""
    compartment_id = request.args.get('compartment_id')
    region = request.args.get('region', config.get('OCM_REGION', 'us-ashburn-1'))

    if not compartment_id:
        return jsonify({'error': 'compartment_id required'}), 400

    cache_key = ('asset_sources', compartment_id, region)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        # Initialize Cloud Bridge Discovery client (not Inventory client)
        cloud_bridge_client = get_oci_client(oci.cloud_bridge.DiscoveryClient, region=region)

        # List asset sources
        asset_sources = cloud_bridge_client.list_asset_sources(
            compartment_id=compartment_id
        ).data

        asset_source_list = []
        for source in asset_sources.items:
            if source.lifecycle_state == 'ACTIVE':
                # Map source type to friendly name
                source_type_str = str(source.type) if source.type else 'Unknown'

                # Determine friendly type display
                if source_type_str == 'VMWARE' or 'vmware' in source_type_str.lower():
                    friendly_type = 'VMware vCenter'
                elif 'aws' in source_type_str.lower():
                    friendly_type = 'AWS Discovery'
                elif source_type_str == 'UNKNOWN_ENUM_VALUE' or source_type_str == 'Unknown':
                    # Try to infer from display name
                    display_lower = source.display_name.lower()
                    if 'vmware' in display_lower or 'vcenter' in display_lower:
                        friendly_type = 'VMware vCenter'
                    elif 'aws' in display_lower:
                        friendly_type = 'AWS Discovery'
                    else:
                        friendly_type = 'Cloud Bridge'
                else:
                    friendly_type = source_type_str.replace('_', ' ').title()

                asset_source_list.append({
                    'id': source.id,
                    'name': source.display_name,
                    'type': friendly_type,
                    'lifecycle_state': source.lifecycle_state
                })

        result = {'asset_sources': asset_source_list}
        _cache_set(cache_key, result)
        return jsonify(result)
    except oci.exceptions.ServiceError as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list asset sources: returning stale cache after OCI error')
            return jsonify(stale)
        return _oci_error_response(e, 'list asset sources')
    except Exception as e:
        stale = _cache_get_stale(cache_key)
        if stale is not None:
            logger.warning('list asset sources: returning stale cache after internal error')
            return jsonify(stale)
        logger.exception('Failed to list asset sources')
        return jsonify({'error': 'An internal error occurred', 'retryable': False}), 500


# ============================================================================
# PRE-FLIGHT INVENTORY DASHBOARD
# ============================================================================

@app.route('/inventory')
def inventory_dashboard():
    """Pre-Flight Inventory Dashboard for Sales/Services teams."""
    return render_template('inventory_dashboard.html')


@app.route('/api/inventory/data', methods=['GET'])
def get_inventory_data():
    """
    API endpoint to fetch inventory data for the dashboard.
    Uses stale-while-revalidate caching for instant page loads.
    Returns summary statistics and asset list.
    """
    try:
        from inventory_cache import get_inventory_with_cache

        # Get data with cache (instant if cached, triggers background refresh)
        data, from_cache = get_inventory_with_cache()

        return jsonify(data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to load inventory data'}), 500


@app.route('/api/inventory/cache/status', methods=['GET'])
def get_inventory_cache_status():
    """
    Get current cache status for frontend polling.
    Returns cache age, refresh status, and whether new data is available.
    """
    try:
        from inventory_cache import get_cache_status
        status = get_cache_status()
        return jsonify(status)
    except Exception as e:
        logger.exception('Failed to get inventory cache status')
        return jsonify({'error': 'An internal error occurred'}), 500


@app.route('/api/inventory/cache/refresh', methods=['POST'])
def trigger_inventory_refresh():
    """
    Manually trigger a background refresh of inventory data.
    """
    try:
        from inventory_cache import trigger_background_refresh
        force = request.args.get('force', 'false').lower() == 'true'
        result = trigger_background_refresh(force=force)
        return jsonify(result)
    except Exception as e:
        logger.exception('Failed to trigger inventory cache refresh')
        return jsonify({'error': 'An internal error occurred'}), 500


@app.route('/api/inventory/cache/clear', methods=['POST'])
def clear_inventory_cache():
    """
    Clear the inventory cache.
    """
    try:
        from inventory_cache import clear_cache
        success = clear_cache()
        return jsonify({'success': success})
    except Exception as e:
        logger.exception('Failed to clear inventory cache')
        return jsonify({'error': 'An internal error occurred'}), 500


@app.route('/api/inventory/export/csv', methods=['GET'])
def export_inventory_csv():
    """Export inventory data as CSV file."""
    try:
        from inventory_dashboard import fetch_inventory_assets, export_to_csv
        from flask import Response

        # Get optional filters
        compartment_id = request.args.get('compartment_id')
        asset_source_id = request.args.get('asset_source_id')

        assets = fetch_inventory_assets(
            compartment_id=compartment_id,
            asset_source_id=asset_source_id
        )

        csv_content = export_to_csv(assets)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"ocm_inventory_{timestamp}.csv"

        return Response(
            csv_content,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        logger.exception('Failed to export inventory CSV')
        return jsonify({'error': 'An internal error occurred'}), 500



@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return render_template('404.html'), 404


@app.errorhandler(400)
def bad_request(e):
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'error': str(e.description) if hasattr(e, 'description') else 'Bad request'}), 400
    return render_template('500.html'), 400

@app.errorhandler(CSRFError)
def csrf_error(e):
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'error': 'CSRF token missing or invalid. Please reload the page and try again.'}), 400
    return render_template('500.html'), 400

@app.errorhandler(500)
def internal_error(e):
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('500.html'), 500


if __name__ == '__main__':
    print("WARNING: Running with the built-in development server. Use gunicorn for production.", file=sys.stderr)
    app.run(host='0.0.0.0', debug=False)
