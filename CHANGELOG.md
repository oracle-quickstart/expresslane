# Changelog

All notable changes to ExpressLane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-04-14

**First public release to [`oracle-quickstart/expresslane`](https://github.com/oracle-quickstart/expresslane).**

### Added

- **VM migration pipeline** from VMware vSphere and AWS EC2 to Oracle Cloud Infrastructure via Oracle Cloud Migrations (OCM).
- **Cloud Bridge inventory audit** with complexity scoring, zombie detection, and CSV / PDF export for pre-migration analysis.
- **Guided 3-step setup wizard** (Admin account → Environment → Cloud Bridge) with Instance Principals auto-detection — no OCI API keys or `~/.oci/config` needed on the host.
- **Batch migrations** — select multiple VMs in the builder and submit them as a single batch, tracked on the dashboard with a shared batch identifier.
- **Warm Migration with test-before-cutover flow** — schedules data replication continuously, lets you deploy a live test VM to the destination subnet for validation, then preserves the hydrated boot volume so the real cutover reuses it (no re-replication).
- **Three Migration Schedule modes:** Migrate Immediately, Run Once & Pause, and Warm Migration (Daily Sync or Weekly Sync).
- **Six-step OCM pipeline monitoring** (Create Project → Create Plan → Add Asset → Replicate Asset → Generate RMS Stack → Deploy Stack) with live SDK log streaming and per-step retry.
- **Two supported install paths:**
  - **VM Manual Install** via `deploy/deploy.sh` on Oracle Linux 9, with first-class HTTPS via `--fqdn`, `--tls-cert`, and `--tls-key` arguments.
  - **Podman Install** via `docker-compose.yml` (compatible with `sudo podman-compose`), featuring a healthcheck-gated app container, nginx reverse proxy, SELinux `:z` bind-mount labels, and UID/GID passthrough via `.env`.
- **Complete OSS documentation:** README with OCM Prerequisites appendix (AWS Cloud Bridge asset source walkthrough, VMware pointer), UPDATING.md (upgrade + rollback + uninstall for both paths), CONTRIBUTING.md with Oracle Contributor Agreement policy, SECURITY.md disclosure process, THIRD_PARTY_LICENSES.txt covering Flask / SQLAlchemy / gunicorn / oci / reportlab / Werkzeug / Jinja2 / and their transitive dependencies.
- **Oracle Linux 9 specific fixes** in the container stack: fully-qualified image names (`docker.io/library/python:3.9-slim`, `docker.io/library/nginx:1.25-alpine`) to sidestep podman's `short-name-mode = "enforcing"` default; absolute `/home/opc/.oci` bind-mount path so `sudo podman-compose` doesn't break on `$HOME=/root` expansion.

### Security

- **In-memory per-IP rate limiting** on `/login` and `/setup` POST endpoints (10 requests per IP per 60 seconds, 429 response over the limit).
- **CSRF protection** via Flask-WTF `CSRFProtect` on all state-changing routes.
- **Hardened session cookies:** `HttpOnly`, `SameSite=Lax`, 8-hour session lifetime, `Secure` flag enabled automatically when `SECURE_COOKIES=true` is set (done by `deploy.sh` when run with the `--fqdn/--tls-cert/--tls-key` trio).
- **Response security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-XSS-Protection`, `Content-Security-Policy` locked to self + cdnjs + Google Fonts, `Strict-Transport-Security` when behind HTTPS.
- **PBKDF2 password hashing** for the local admin account via werkzeug's `generate_password_hash`.
- **Global `before_request` auth gate** — every route except `/login`, `/logout`, `/setup`, and `/static` requires an authenticated session.

### Database

- Schema migrations run idempotently on every startup via `app.py:_migrate_schema()`. v1.2.0 adds the following columns to the existing `ocm_migration` table if they do not already exist:
  - **Test migration state:** `test_status`, `test_start_time`, `test_end_time`, `test_logs`, `test_cleanup_job_id`, plus legacy columns kept for back-compat (`test_rms_stack_ocid`, `test_rms_job_id`, `test_destroy_job_id`, `test_instance_ocid`, `test_cleanup_required`, `test_started_at`, `test_deployed_at`, `test_completed_at`, `test_migration_count`).
  - **Warm sync state:** `sync_status`.
- Row-level status normalization: rows still carrying legacy V1.6-era `Test-Deploy-Failed`, `Test-Cleanup-Failed`, `Test-Deploying`, `Test-Deployed`, or `Test-Cleanup` main statuses are rewritten to `Failed` or `Running` on startup so they stop showing up as stuck rows in the new UI.

### Known limitations

- `podman-compose` is not in the default Oracle Linux 9 AppStream repos; the Podman install path installs it via `pip3`.
- Dockerfile-level `HEALTHCHECK` directive is silently ignored when podman builds in OCI image format (the compose-level `healthcheck:` is honored and drives `depends_on: service_healthy` correctly).
- VMware source environment setup is out of scope for the README's bundled walkthrough. The AWS asset source is covered end-to-end in the OCM Prerequisites appendix; VMware readers are pointed at <https://docs.oracle.com/en-us/iaas/Content/cloud-migration/home.htm>.

## Pre-public releases

### [1.1.0] and [1.0.0]

Distributed privately to a small number of early customers for feedback. These releases were never published to `oracle-quickstart/expresslane`. Users running v1.0.0 or v1.1.0 should follow the [UPDATING.md — VM Manual Install upgrade](./UPDATING.md#upgrading--option-1-vm-manual-install) or [Podman Install upgrade](./UPDATING.md#upgrading--option-2-podman-install) flow to move to v1.2.0. Schema migrations on first startup after the upgrade are automatic and idempotent.

---

*ExpressLane — Changelog*
*Copyright (c) 2026 Oracle and/or its affiliates. Released under UPL-1.0.*
