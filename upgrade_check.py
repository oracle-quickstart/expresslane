# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
ExpressLane - Upgrade Check

A lightweight, opt-out, privacy-respecting check that asks the ExpressLane
release service whether a newer version is available. The result is rendered
as a non-blocking banner in the UI.

What is sent (and ONLY what is sent):
    - the installed ExpressLane version string (e.g. "1.2.0")
    - the first 8 characters of a locally-generated install UUID

Nothing else leaves the host. No hostname, no IP, no OCIDs, no tenancy info,
no user names, no resource details.

How to disable:
    export EXPRESSLANE_NO_UPGRADE_CHECK=true

The full implementation is stdlib Python (no `requests` dependency) and is
designed to be easy to audit. See README.md for the user-facing description.
"""

import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Constants (everything an auditor needs in one place) ──────────────────

# Production API Gateway URL — the OCI Function backend behind this endpoint
# lives in the ExpressLane compartment (us-ashburn-1) and is sourced from
# backend/oci_function/upgrade_check/. Overridable at runtime via
# EXPRESSLANE_UPGRADE_CHECK_URL (useful for staging / tests).
DEFAULT_UPGRADE_CHECK_URL = (
    "https://kpbcyxd4d23n4ww5eqqyuszebi.apigateway.us-ashburn-1.oci.customer-oci.com"
    "/expresslane/v1/check"
)

# Default state directory for the install_id and the result cache.
# Overridable via EXPRESSLANE_STATE_DIR for containers with a non-standard $HOME.
DEFAULT_STATE_DIR = Path.home() / ".expresslane"

# How long a cached upgrade-check result is considered fresh. Jitter is added
# to prevent a thundering-herd of simultaneous restarts from hitting the
# backend at the same second.
CACHE_BASE_SECONDS = 24 * 60 * 60           # 24 hours
CACHE_JITTER_SECONDS = 6 * 60 * 60          # +  random 0-6 hours

# Network budget for the upgrade check. We never retry.
HTTP_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 16 * 1024              # responses are tiny; cap anyway

# Only show the upgrade banner when both the installed AND the advertised
# latest version are a strict X.Y.Z release. Dev / pre-release / local builds
# are silently skipped - the developer knows what they are doing.
STRICT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Env vars recognised by this module.
OPT_OUT_ENV = "EXPRESSLANE_NO_UPGRADE_CHECK"
ENDPOINT_ENV = "EXPRESSLANE_UPGRADE_CHECK_URL"
STATE_DIR_ENV = "EXPRESSLANE_STATE_DIR"
_OPT_OUT_TRUE_VALUES = {"1", "true", "yes", "on"}

# ─── Module-level state (all access guarded by _LOCK) ──────────────────────

_LOCK = threading.Lock()
_STARTED = False
_STATUS = {
    "available": False,
    "latest": None,
    "notes_url": None,
    "download_url": None,
    "checked_at": None,
}


# ─── Public API ────────────────────────────────────────────────────────────

def is_disabled():
    """Return True if the user has opted out via env var."""
    val = os.environ.get(OPT_OUT_ENV, "").strip().lower()
    return val in _OPT_OUT_TRUE_VALUES


def get_status():
    """Return a copy of the current upgrade-check status dict.

    Schema is stable across all states so templates can key on
    `upgrade_status.available` without guarding. Safe for Jinja rendering.
    """
    with _LOCK:
        return dict(_STATUS)


def start_background_check(current_version):
    """Spawn a daemon thread that performs the upgrade check, once.

    Idempotent and thread-safe: the first caller wins; subsequent calls are
    no-ops. Safe to invoke from `before_request` under gunicorn preload where
    multiple worker requests may race.
    """
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    if is_disabled():
        logger.info("upgrade_check: disabled via %s", OPT_OUT_ENV)
        return

    t = threading.Thread(
        target=_run_check_safely,
        args=(current_version,),
        name="expresslane-upgrade-check",
        daemon=True,
    )
    t.start()


# ─── Internals ─────────────────────────────────────────────────────────────

def _state_dir():
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_STATE_DIR


def _endpoint_url():
    return os.environ.get(ENDPOINT_ENV, DEFAULT_UPGRADE_CHECK_URL)


def _ensure_state_dir():
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d
    except Exception as e:
        logger.debug("upgrade_check: cannot create state dir: %s", e)
        return None


def _get_install_id_prefix():
    """Return the first 8 chars of the stored install UUID, creating it if needed.

    Returns None if disabled, if the state dir is not writable, or if any IO
    fails. Never raises.
    """
    d = _ensure_state_dir()
    if d is None:
        return None
    path = d / "install_id"
    try:
        if path.exists():
            content = path.read_text().strip()
        else:
            content = uuid.uuid4().hex
            path.write_text(content + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return content[:8] if content else None
    except Exception as e:
        logger.debug("upgrade_check: cannot read/write install_id: %s", e)
        return None


def _cache_path():
    try:
        return _state_dir() / "upgrade_cache.json"
    except Exception:
        return None


def _load_cached_result():
    p = _cache_path()
    if p is None or not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        ttl = CACHE_BASE_SECONDS + float(data.get("_jitter", 0))
        if (time.time() - float(data.get("_saved_at", 0))) < ttl:
            return data.get("payload")
    except Exception as e:
        logger.debug("upgrade_check: cache read failed: %s", e)
    return None


def _save_cached_result(payload):
    p = _cache_path()
    if p is None:
        return
    try:
        _ensure_state_dir()
        body = {
            "_saved_at": time.time(),
            "_jitter": random.uniform(0, CACHE_JITTER_SECONDS),
            "payload": payload,
        }
        p.write_text(json.dumps(body))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception as e:
        logger.debug("upgrade_check: cache write failed: %s", e)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow any 3xx response.

    Guards the client against a compromised endpoint bouncing us to a
    third-party host. urllib's default behaviour is to follow redirects
    silently; we explicitly override that here.
    """
    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects disabled", headers, fp
        )
    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def _http_get_json(url):
    """GET `url` and return parsed JSON, or None on any error.

    3s timeout, https-only, no redirects, 16 KiB response cap, silent on
    every failure mode.
    """
    if not url.startswith("https://"):
        logger.debug("upgrade_check: refusing non-https URL")
        return None
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ExpressLane-UpgradeCheck/1"},
        )
        with opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(MAX_RESPONSE_BYTES)
            return json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.debug("upgrade_check: HTTP call failed: %s", e)
        return None


def _compute_is_newer(current, latest):
    """Strict X.Y.Z numeric comparison. Anything non-standard returns False."""
    if not (isinstance(current, str) and isinstance(latest, str)):
        return False
    if not (STRICT_VERSION_RE.match(current) and STRICT_VERSION_RE.match(latest)):
        return False
    try:
        ct = tuple(int(p) for p in current.split("."))
        lt = tuple(int(p) for p in latest.split("."))
        return lt > ct
    except Exception:
        return False


def _run_check_safely(current_version):
    try:
        _run_check(current_version)
    except Exception as e:
        logger.debug("upgrade_check: unexpected error: %s", e)


def _run_check(current_version):
    if is_disabled():
        return

    cached = _load_cached_result()
    if cached is not None:
        _apply_result(current_version, cached, from_cache=True)
        return

    prefix = _get_install_id_prefix()
    if not prefix:
        return

    base = _endpoint_url()
    query = urllib.parse.urlencode({
        "version": current_version,
        "install_id": prefix,
    })
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}{query}"

    payload = _http_get_json(url)
    if payload is None:
        return

    _save_cached_result(payload)
    _apply_result(current_version, payload, from_cache=False)


def _apply_result(current_version, payload, *, from_cache):
    latest = payload.get("latest_version") or ""
    notes_url = payload.get("release_notes_url")
    download_url = payload.get("download_url")

    is_newer = _compute_is_newer(current_version, latest)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _LOCK:
        _STATUS["available"] = is_newer
        _STATUS["latest"] = latest or None
        _STATUS["notes_url"] = notes_url
        _STATUS["download_url"] = download_url
        _STATUS["checked_at"] = checked_at

    logger.info(
        "upgrade_check: available=%s latest=%s source=%s",
        is_newer, latest, "cache" if from_cache else "network",
    )
