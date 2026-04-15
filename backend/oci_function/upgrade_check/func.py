# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
ExpressLane - Upgrade Check backend (OCI Function)

Receives GET requests from ExpressLane installs, logs a single anonymous
event line to stdout (which OCI Logging forwards to the Function's log
group), and returns JSON describing the latest ExpressLane release.

Input (query string):
    ?version=1.2.0&install_id=a1b2c3d4

Output (200):
    {
        "latest_version": "1.2.0",
        "is_newer": false,
        "release_notes_url": "https://github.com/oracle-quickstart/expresslane/releases/tag/v1.2.0",
        "download_url": "https://github.com/oracle-quickstart/expresslane/releases/download/v1.2.0/expresslane-1.2.0.tar.gz",
        "checked_at": "2026-04-15T12:34:56+00:00"
    }

Output on validation failure (400):
    Same JSON shape with `error` set and the other fields nulled.

Deployment notes: see ./README.md
"""

import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import oci
from fdk import response

logger = logging.getLogger("upgrade_check")
logger.setLevel(logging.INFO)

# ─── Config (all from Function env vars set at deploy time) ────────────────

META_NAMESPACE = os.environ.get("META_NAMESPACE", "")        # auto-resolved if empty
META_BUCKET = os.environ.get("META_BUCKET", "expresslane-meta")
META_OBJECT = os.environ.get("META_OBJECT", "latest.json")
META_TTL_SECONDS = int(os.environ.get("META_TTL_SECONDS", "300"))

# GeoLite2-Country.mmdb should be copied into the function directory before
# `fn deploy` — Fn packages every file next to func.py into /function/.
GEOIP_DB_PATH = os.environ.get("GEOIP_DB_PATH", "/function/GeoLite2-Country.mmdb")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-z0-9.]+)?$")
STRICT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
INSTALL_ID_RE = re.compile(r"^[a-f0-9]{8}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# ─── Module-level caches (populated on cold-start, reused warm) ────────────

_latest_cache = {"data": None, "saved_at": 0.0}
_os_client = None
_geoip_reader = None  # None = not tried, False = tried and failed, Reader = usable


def _get_os_client():
    """Lazily build an Object Storage client using the Function's resource principal."""
    global _os_client
    if _os_client is None:
        signer = oci.auth.signers.get_resource_principals_signer()
        _os_client = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    return _os_client


def _get_geoip_reader():
    """Lazily open the GeoLite2 DB. Returns None if unavailable."""
    global _geoip_reader
    if _geoip_reader is None:
        try:
            import geoip2.database
            _geoip_reader = geoip2.database.Reader(GEOIP_DB_PATH)
        except Exception as e:
            logger.warning("geoip init failed: %s", e)
            _geoip_reader = False
    return _geoip_reader or None


def _load_latest():
    """Read latest.json from Object Storage, cached for META_TTL_SECONDS.

    Returns a dict with at least `latest_version`. On any failure returns a
    safe fallback so the client never sees a 500.
    """
    now = time.time()
    if (_latest_cache["data"] is not None
            and (now - _latest_cache["saved_at"]) < META_TTL_SECONDS):
        return _latest_cache["data"]

    try:
        client = _get_os_client()
        namespace = META_NAMESPACE or client.get_namespace().data
        obj = client.get_object(namespace, META_BUCKET, META_OBJECT)
        data = json.loads(obj.data.content.decode("utf-8"))
        _latest_cache["data"] = data
        _latest_cache["saved_at"] = now
        return data
    except Exception as e:
        logger.error("failed to load latest.json: %s", e)
        return {
            "latest_version": "0.0.0",
            "release_notes_url": None,
            "download_url": None,
        }


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


def _resolve_country(headers):
    """Best-effort country lookup. Returns a 2-letter ISO code or 'unknown'.

    Strategy:
        1. If the API Gateway (or an upstream proxy) injected a country
           header, trust it.
        2. Otherwise resolve X-Forwarded-For through the GeoLite2 DB.
        3. On any failure, return 'unknown' — the field is always present.

    The raw IP is read, resolved, and immediately discarded. It is never
    logged, cached, or returned to the client.
    """
    # Fn Python prefixes inbound HTTP headers with "Fn-Http-H-".
    candidates = [
        "Fn-Http-H-Cf-Ipcountry",
        "Fn-Http-H-X-Country-Code",
        "Cf-Ipcountry",
        "X-Country-Code",
    ]
    for key in candidates:
        val = headers.get(key) or headers.get(key.lower())
        if val and COUNTRY_RE.match(val.upper()):
            return val.upper()

    xff = (headers.get("Fn-Http-H-X-Forwarded-For")
           or headers.get("X-Forwarded-For")
           or "")
    if xff:
        ip = xff.split(",")[0].strip()
        reader = _get_geoip_reader()
        if reader and ip:
            try:
                rec = reader.country(ip)
                if rec.country.iso_code:
                    return rec.country.iso_code.upper()
            except Exception as e:
                logger.debug("geoip lookup failed: %s", e)

    return "unknown"


def _response_body(latest, is_newer, error=None):
    return {
        "latest_version": latest.get("latest_version") if not error else None,
        "is_newer": is_newer,
        "release_notes_url": latest.get("release_notes_url") if not error else None,
        "download_url": latest.get("download_url") if not error else None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **({"error": error} if error else {}),
    }


def _error(ctx, code, message):
    body = json.dumps(_response_body({}, False, error=message))
    return response.Response(
        ctx,
        response_data=body,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        status_code=code,
    )


def handler(ctx, data: io.BytesIO = None):
    try:
        return _handler(ctx)
    except Exception as e:
        logger.error("unhandled error: %s", e)
        return _error(ctx, 500, "internal error")


def _handler(ctx):
    # ── Parse query string ────────────────────────────────────────────────
    try:
        url = ctx.RequestURL()
        qs = parse_qs(urlsplit(url).query)
    except Exception:
        qs = {}

    version = (qs.get("version", [""])[0] or "").strip()
    install_id = (qs.get("install_id", [""])[0] or "").strip().lower()

    # ── Validate ──────────────────────────────────────────────────────────
    if not VERSION_RE.match(version):
        return _error(ctx, 400, "invalid version")
    if not INSTALL_ID_RE.match(install_id):
        return _error(ctx, 400, "invalid install_id")

    # ── Resolve country (best-effort, never blocks) ───────────────────────
    try:
        headers = dict(ctx.Headers() or {})
    except Exception:
        headers = {}
    country = _resolve_country(headers)

    # ── Emit the single anonymous event line ──────────────────────────────
    logger.info(json.dumps({
        "event": "upgrade_check",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": version,
        "install_id_prefix": install_id,
        "country": country,
    }))

    # ── Build the response from latest.json ───────────────────────────────
    latest = _load_latest()
    latest_version = latest.get("latest_version") or "0.0.0"
    is_newer = _compute_is_newer(version, latest_version)

    body = _response_body(latest, is_newer)
    return response.Response(
        ctx,
        response_data=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "public, max-age=3600",
        },
        status_code=200,
    )
