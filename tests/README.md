# ExpressLane — Tests

This directory contains smoke tests for ExpressLane. They are intentionally minimal — enough infrastructure that a PR contributor can extend them without inventing conventions, not a full test suite.

## What's here

| File | What it tests |
|------|---------------|
| `conftest.py` | pytest config; adds the repo root to `sys.path` so tests can `import app`, `import config`, etc. without installing the project as a package. |
| `test_version.py` | The `version.__version__` string exists and is valid `MAJOR.MINOR.PATCH` semver. |
| `test_migration_sizer.py` | `MigrationSizer` loads its mapping file, sizes a VMware asset, sizes an AWS asset against a direct mapping, and handles missing fields gracefully. |

Every test file is written so it can run **standalone without pytest installed**. This is deliberate — it lets contributors verify the test infrastructure on a fresh checkout without first setting up a dev environment.

## Running the tests

### Option 1 — Standalone (no dependencies)

From the repo root:

```bash
python3 tests/test_version.py
python3 tests/test_migration_sizer.py
```

Expected output:

```
OK: version 1.2.0 is valid semver
OK: migration_sizer smoke tests passed
```

### Option 2 — pytest (recommended for development)

Install pytest once:

```bash
pip install pytest
```

Then run all tests from the repo root:

```bash
python3 -m pytest tests/ -v
```

## What's NOT tested yet

These are gaps an external contribution could fill:

- **Flask routes.** `app.py` defines the web endpoints, but the tests here don't exercise them. A good next step is a `test_routes.py` using `app.test_client()` that verifies `/login` returns 200, `/setup` redirects correctly when the admin is unconfigured, and authenticated routes reject unauthenticated requests with a 302 to `/login`.
- **OCI SDK interactions.** `oci_clients.py`, `ocm_migration.py`, and `inventory_dashboard.py` all call the OCI SDK. Unit-testing these requires mocking `oci.cloud_migrations`, `oci.cloud_bridge`, and `oci.core` — see the `unittest.mock` patterns for examples.
- **Schema migrations.** `app.py:_migrate_schema()` runs `ALTER TABLE` statements on startup. A good test would create a fresh SQLite DB, load the pre-migration schema, run `_migrate_schema()`, and assert the new columns exist.
- **Setup wizard flow.** `templates/setup.html` + the POST handlers in `app.py` drive the three-step wizard. An end-to-end test using `app.test_client()` could walk through it, confirming `config.json` is written correctly.
- **Rate limiting.** The `@rate_limit` decorator on `/login` and `/setup` is an in-memory per-IP sliding window. A test that hits `/login` 11 times and verifies the 11th returns 429 would pin the behavior.

## How to add a test

Pick a module, create `test_<module>.py` next to the existing files, and follow the pattern of the existing smoke tests:

```python
# tests/test_my_module.py
# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_module import thing_i_want_to_test  # noqa: E402


def test_thing_does_what_i_expect():
    assert thing_i_want_to_test(input) == expected_output


if __name__ == "__main__":
    test_thing_does_what_i_expect()
    print("OK: my_module smoke tests passed")
```

The `if __name__ == "__main__":` block at the bottom is what lets your test run standalone, which is the convention in this directory.

---

*ExpressLane — Test Suite*
*Copyright (c) 2026 Oracle and/or its affiliates. Released under UPL-1.0.*
