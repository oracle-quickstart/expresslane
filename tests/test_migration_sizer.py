# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Smoke tests for the migration_sizer module.

These tests exercise the pure-Python pricing and sizing logic without needing
any OCI credentials, network access, or the Flask app context. They use the
bundled aws_oci_mapping.json so they reflect the shapes ExpressLane will
actually recommend at runtime.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migration_sizer import MigrationSizer, SizingResult  # noqa: E402


def _sizer():
    return MigrationSizer()


def test_sizer_loads_mapping_file():
    sizer = _sizer()
    assert sizer.aws_mappings, "aws_mappings dict should not be empty"
    assert sizer.default_shape.startswith("VM.Standard."), \
        f"default_shape looks wrong: {sizer.default_shape!r}"


def test_size_vmware_asset_returns_valid_result():
    sizer = _sizer()
    result = sizer.size_asset({
        "hostname": "vmware-test-01",
        "source": "VMware",
        "vcpus": 4,
        "ram_gb": 16,
    })
    assert isinstance(result, SizingResult)
    assert result.oci_ocpu >= 1, "oci_ocpu must be at least 1"
    assert result.oci_ram_gb >= 1, "oci_ram_gb must be at least 1"
    assert result.sizing_method == "generic_calculation"
    assert result.confidence in {"high", "medium", "low"}
    assert result.current_monthly_cost > 0
    assert result.oci_monthly_cost > 0


def test_size_aws_asset_returns_valid_result():
    sizer = _sizer()
    # Pick any AWS instance type that is in the mapping file; we iterate
    # to stay resilient to mapping updates.
    if not sizer.aws_mappings:
        return
    instance_type = next(iter(sizer.aws_mappings.keys()))
    result = sizer.size_asset({
        "hostname": "aws-test-01",
        "source": "AWS",
        "instance_type": instance_type,
        "vcpus": 2,
        "ram_gb": 8,
    })
    assert isinstance(result, SizingResult)
    assert result.sizing_method == "direct_mapping"
    assert result.confidence == "high"
    assert result.oci_shape  # non-empty


def test_size_asset_handles_missing_fields_gracefully():
    sizer = _sizer()
    result = sizer.size_asset({"source": "unknown"})
    assert isinstance(result, SizingResult)
    # Zero vCPU / RAM falls through to generic calc with min_ocpu floor
    assert result.oci_ocpu >= sizer.min_ocpu


if __name__ == "__main__":
    test_sizer_loads_mapping_file()
    test_size_vmware_asset_returns_valid_result()
    test_size_aws_asset_returns_valid_result()
    test_size_asset_handles_missing_fields_gracefully()
    print("OK: migration_sizer smoke tests passed")
