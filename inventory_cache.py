#!/usr/bin/env python3
# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
ExpressLane Inventory Cache Module
==================================
Implements "Stale-While-Revalidate" caching pattern for instant page loads.

Features:
- Persistent file-based cache (survives app restarts)
- Background refresh using threading
- Instant response with cached data
- Automatic cache updates in background
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

# Cache configuration
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_FILE = CACHE_DIR / "inventory_cache.json"
CACHE_LOCK = threading.Lock()

# Background refresh state
_refresh_in_progress = False
_last_refresh_started = None
_last_refresh_completed = None
_refresh_error = None


def ensure_cache_dir():
    """Ensure the cache directory exists."""
    CACHE_DIR.mkdir(exist_ok=True)


def get_cached_inventory() -> Optional[Dict[str, Any]]:
    """
    Get cached inventory data from file.

    Returns:
        Cached data dict with 'data', 'cached_at', and 'cache_age_seconds' keys,
        or None if no cache exists.
    """
    ensure_cache_dir()

    if not CACHE_FILE.exists():
        return None

    try:
        with CACHE_LOCK:
            with open(CACHE_FILE, 'r') as f:
                cache_data = json.load(f)

        # Calculate cache age
        cached_at = cache_data.get('cached_at')
        if cached_at:
            cached_time = datetime.fromisoformat(cached_at)
            age_seconds = (datetime.now() - cached_time).total_seconds()
            cache_data['cache_age_seconds'] = int(age_seconds)
            cache_data['cache_age_human'] = format_age(age_seconds)

        return cache_data

    except (json.JSONDecodeError, IOError) as e:
        print(f"[Cache] Error reading cache file: {e}")
        return None


def save_to_cache(data: Dict[str, Any]) -> bool:
    """
    Save inventory data to cache file.

    Args:
        data: The inventory dashboard data to cache

    Returns:
        True if saved successfully, False otherwise
    """
    ensure_cache_dir()

    cache_data = {
        'data': data,
        'cached_at': datetime.now().isoformat(),
        'version': '3.1'
    }

    try:
        with CACHE_LOCK:
            with open(CACHE_FILE, 'w') as f:
                json.dump(cache_data, f, indent=2, default=str)

        print(f"[Cache] Inventory saved to cache at {cache_data['cached_at']}")
        return True

    except IOError as e:
        print(f"[Cache] Error writing cache file: {e}")
        return False


def format_age(seconds: float) -> str:
    """Format age in seconds to human-readable string."""
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"


def get_cache_status() -> Dict[str, Any]:
    """
    Get current cache status for frontend polling.

    Returns:
        Dict with refresh status, timestamps, and any errors
    """
    global _refresh_in_progress, _last_refresh_started, _last_refresh_completed, _refresh_error

    cached = get_cached_inventory()

    return {
        'has_cache': cached is not None,
        'cached_at': cached.get('cached_at') if cached else None,
        'cache_age_seconds': cached.get('cache_age_seconds') if cached else None,
        'cache_age_human': cached.get('cache_age_human') if cached else None,
        'refresh_in_progress': _refresh_in_progress,
        'last_refresh_started': _last_refresh_started,
        'last_refresh_completed': _last_refresh_completed,
        'refresh_error': _refresh_error,
        'new_data_available': (
            _last_refresh_completed is not None and
            cached is not None and
            _last_refresh_completed > cached.get('cached_at', '')
        )
    }


def trigger_background_refresh(force: bool = False) -> Dict[str, Any]:
    """
    Trigger a background refresh of inventory data.

    Args:
        force: If True, start refresh even if one is in progress

    Returns:
        Status dict indicating if refresh was started
    """
    global _refresh_in_progress, _last_refresh_started, _refresh_error

    if _refresh_in_progress and not force:
        return {
            'status': 'already_running',
            'message': 'Background refresh already in progress',
            'started_at': _last_refresh_started
        }

    # Start background thread
    thread = threading.Thread(target=_background_refresh_worker, daemon=True)
    thread.start()

    return {
        'status': 'started',
        'message': 'Background refresh started',
        'started_at': _last_refresh_started
    }


def _background_refresh_worker():
    """Worker function that runs in background thread to fetch fresh data."""
    global _refresh_in_progress, _last_refresh_started, _last_refresh_completed, _refresh_error

    _refresh_in_progress = True
    _last_refresh_started = datetime.now().isoformat()
    _refresh_error = None

    print(f"[Cache] Background refresh started at {_last_refresh_started}")
    start_time = time.time()

    try:
        # Import here to avoid circular imports
        from inventory_dashboard import get_inventory_dashboard_data

        # Fetch fresh data from OCI
        fresh_data = get_inventory_dashboard_data()

        # Save to cache
        save_to_cache(fresh_data)

        elapsed = time.time() - start_time
        _last_refresh_completed = datetime.now().isoformat()

        print(f"[Cache] Background refresh completed in {elapsed:.2f}s")
        print(f"[Cache] Fetched {len(fresh_data.get('assets', []))} assets")

    except Exception as e:
        _refresh_error = str(e)
        print(f"[Cache] Background refresh failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        _refresh_in_progress = False


def get_inventory_with_cache() -> Tuple[Dict[str, Any], bool]:
    """
    Main entry point: Get inventory data from cache.

    Background refresh is NOT automatically triggered.
    Refresh only happens when:
    - User clicks "Refresh" button
    - Cache is older than 1 day (triggered by frontend)

    Returns:
        Tuple of (data, from_cache) where:
        - data: The inventory dashboard data
        - from_cache: True if data came from cache, False if freshly fetched
    """
    # Try to get cached data first
    cached = get_cached_inventory()

    if cached and cached.get('data'):
        # Return cached data immediately (no background refresh)
        data = cached['data']

        # Add cache metadata to response
        data['_cache'] = {
            'from_cache': True,
            'cached_at': cached.get('cached_at'),
            'cache_age_seconds': cached.get('cache_age_seconds'),
            'cache_age_human': cached.get('cache_age_human')
        }

        return data, True

    else:
        # No cache - must fetch synchronously
        print("[Cache] No cache found, fetching fresh data...")

        from inventory_dashboard import get_inventory_dashboard_data

        fresh_data = get_inventory_dashboard_data()

        # Save to cache for next time
        save_to_cache(fresh_data)

        fresh_data['_cache'] = {
            'from_cache': False,
            'cached_at': datetime.now().isoformat(),
            'cache_age_seconds': 0,
            'cache_age_human': 'just now'
        }

        return fresh_data, False


def clear_cache() -> bool:
    """Clear the cache file."""
    ensure_cache_dir()

    try:
        if CACHE_FILE.exists():
            with CACHE_LOCK:
                CACHE_FILE.unlink()
            print("[Cache] Cache cleared")
        return True
    except IOError as e:
        print(f"[Cache] Error clearing cache: {e}")
        return False


def warm_cache():
    """
    Check cache status on application startup.
    Does NOT automatically refresh - refresh only on user action or if > 1 day old.
    """
    cached = get_cached_inventory()

    if cached:
        age = cached.get('cache_age_seconds', 0)
        print(f"[Cache] Found existing cache from {cached.get('cache_age_human', 'unknown time')}")

        # Only auto-refresh if cache is older than 1 day
        if age > 86400:
            print("[Cache] Cache is older than 1 day, triggering background refresh...")
            trigger_background_refresh()
    else:
        print("[Cache] No cache found - will fetch on first page load")


# CLI testing
if __name__ == '__main__':
    print("Testing Inventory Cache Module")
    print("=" * 50)

    # Check current status
    status = get_cache_status()
    print(f"\nCache Status:")
    print(f"  Has Cache: {status['has_cache']}")
    print(f"  Cached At: {status['cached_at']}")
    print(f"  Age: {status['cache_age_human']}")
    print(f"  Refresh In Progress: {status['refresh_in_progress']}")

    # Test getting data with cache
    print("\nFetching inventory with cache...")
    data, from_cache = get_inventory_with_cache()

    print(f"\nResult:")
    print(f"  From Cache: {from_cache}")
    print(f"  Assets Count: {len(data.get('assets', []))}")
    print(f"  Cache Info: {data.get('_cache', {})}")
