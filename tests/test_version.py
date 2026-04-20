# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Smoke test: the version string is present and is valid semver.

Runs with either pytest (`pytest tests/test_version.py`) or standalone
(`python3 tests/test_version.py`) so contributors can verify the test
infrastructure without installing pytest first.
"""

import re
import sys
from pathlib import Path

# Allow standalone execution: python3 tests/test_version.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from version import __version__  # noqa: E402


def test_version_is_semver():
    assert isinstance(__version__, str) and __version__, \
        f"__version__ must be a non-empty string, got {__version__!r}"
    assert re.match(r"^\d+\.\d+\.\d+$", __version__), \
        f"__version__ {__version__!r} is not valid MAJOR.MINOR.PATCH semver"


def test_version_not_placeholder():
    assert __version__ != "0.0.0", "Placeholder version 0.0.0 must be replaced"


if __name__ == "__main__":
    test_version_is_semver()
    test_version_not_placeholder()
    print(f"OK: version {__version__} is valid semver")
