#!/usr/bin/env python3
# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
ExpressLane Migration Sizer
===========================
Presales sizing engine that enriches inventory assets with OCI shape
recommendations, cost estimates, and potential savings calculations.

Usage:
    from migration_sizer import MigrationSizer

    sizer = MigrationSizer()
    enriched_assets = sizer.enrich_inventory(raw_assets)
    summary = sizer.calculate_summary(enriched_assets)
"""

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Any, Optional

# Hours per month (730 = 365 days * 24 hours / 12 months)
HOURS_PER_MONTH = 730


@dataclass
class SizingResult:
    """Result of sizing a single asset for OCI migration."""
    # Recommended OCI configuration
    oci_shape: str
    oci_ocpu: int
    oci_ram_gb: float

    # Cost estimates (monthly)
    current_monthly_cost: float
    oci_monthly_cost: float
    monthly_savings: float
    savings_percentage: float

    # Metadata
    sizing_method: str  # "direct_mapping", "generic_calculation", "fallback"
    perf_note: str
    confidence: str  # "high", "medium", "low"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MigrationSummary:
    """Summary statistics for the entire migration sizing."""
    total_assets: int
    total_vcpus: int
    total_ram_gb: float

    # OCI totals
    total_oci_ocpus: int
    total_oci_ram_gb: float

    # Cost analysis
    total_current_monthly: float
    total_oci_monthly: float
    total_monthly_savings: float
    total_annual_savings: float
    average_savings_percentage: float

    # Breakdown by source
    aws_assets: int = 0
    vmware_assets: int = 0
    other_assets: int = 0

    # Confidence breakdown
    high_confidence_count: int = 0
    medium_confidence_count: int = 0
    low_confidence_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MigrationSizer:
    """
    Presales sizing engine for OCI migrations.

    Takes inventory assets and enriches them with:
    - Recommended OCI compute shape
    - Estimated current and OCI costs
    - Potential monthly/annual savings
    """

    def __init__(self, mapping_file: Optional[str] = None):
        """
        Initialize the sizer with mapping data.

        Args:
            mapping_file: Path to aws_oci_mapping.json. If None, uses default location.
        """
        if mapping_file is None:
            mapping_file = Path(__file__).parent / "aws_oci_mapping.json"

        self.mapping_file = Path(mapping_file)
        self._load_mappings()

    def _load_mappings(self):
        """Load the AWS to OCI mapping rules from JSON."""
        if not self.mapping_file.exists():
            raise FileNotFoundError(f"Mapping file not found: {self.mapping_file}")

        with open(self.mapping_file, 'r') as f:
            data = json.load(f)

        self.meta = data.get('meta', {})
        self.aws_mappings = data.get('aws_mappings', {})
        self.generic_logic = data.get('generic_logic', {})

        # Extract pricing model for generic calculations
        pricing = self.generic_logic.get('pricing_model', {})
        self.oci_ocpu_hourly = pricing.get('base_ocpu_hourly', 0.012)
        self.oci_ram_gb_hourly = pricing.get('base_ram_gb_hourly', 0.0015)
        self.vmware_vcpu_hourly = pricing.get('vmware_est_hourly_per_vcpu', 0.05)
        self.vmware_ram_gb_hourly = pricing.get('vmware_est_hourly_per_gb_ram', 0.006)

        self.default_shape = self.meta.get('default_oci_shape', 'VM.Standard.E6.Flex')
        self.ocpu_ratio = self.generic_logic.get('ocpu_ratio', 0.5)
        self.min_ocpu = self.generic_logic.get('min_ocpu', 1)

    def size_asset(self, asset: Dict[str, Any]) -> SizingResult:
        """
        Calculate OCI sizing for a single asset.

        Args:
            asset: Dictionary with keys: hostname, source, instance_type, vcpus, ram_gb

        Returns:
            SizingResult with OCI recommendation and cost analysis
        """
        source = (asset.get('source') or asset.get('source_type') or '').upper()
        instance_type = asset.get('instance_type') or ''
        vcpus = asset.get('vcpus') or asset.get('vcpu_count') or 0
        ram_gb = asset.get('ram_gb') or asset.get('memory_gb') or 0

        # Try direct AWS mapping first
        if source == 'AWS' and instance_type in self.aws_mappings:
            return self._size_from_aws_mapping(instance_type, vcpus, ram_gb)

        # Fall back to generic calculation for VMware or unmapped AWS types
        return self._size_generic(source, vcpus, ram_gb, instance_type)

    def _size_from_aws_mapping(self, instance_type: str, vcpus: int, ram_gb: float) -> SizingResult:
        """Size using direct AWS instance type mapping."""
        mapping = self.aws_mappings[instance_type]

        # Calculate monthly costs
        aws_hourly = mapping['aws_hourly_est']
        oci_hourly = mapping['oci_hourly_est']

        current_monthly = aws_hourly * HOURS_PER_MONTH
        oci_monthly = oci_hourly * HOURS_PER_MONTH
        monthly_savings = current_monthly - oci_monthly
        savings_pct = (monthly_savings / current_monthly * 100) if current_monthly > 0 else 0

        return SizingResult(
            oci_shape=mapping['oci_rec_shape'],
            oci_ocpu=mapping['oci_ocpu'],
            oci_ram_gb=mapping['oci_ram_gb'],
            current_monthly_cost=round(current_monthly, 2),
            oci_monthly_cost=round(oci_monthly, 2),
            monthly_savings=round(monthly_savings, 2),
            savings_percentage=round(savings_pct, 1),
            sizing_method="direct_mapping",
            perf_note=mapping.get('perf_note', ''),
            confidence="high"
        )

    def _size_generic(self, source: str, vcpus: int, ram_gb: float,
                      instance_type: str = '') -> SizingResult:
        """
        Size using generic vCPU/RAM calculation.
        Used for VMware assets or unmapped AWS instance types.
        """
        # Calculate OCI OCPUs (1 OCPU = 2 vCPUs for Intel/AMD)
        oci_ocpu = max(self.min_ocpu, math.ceil(vcpus * self.ocpu_ratio))
        oci_ram_gb = max(1, ram_gb)

        # Estimate current costs based on source
        if source == 'VMWARE':
            # VMware licensing + infrastructure estimate
            current_hourly = (vcpus * self.vmware_vcpu_hourly) + (ram_gb * self.vmware_ram_gb_hourly)
            confidence = "medium"
            perf_note = "VMware estimate based on typical on-prem + licensing costs."
        elif source == 'AWS':
            # Unknown AWS type - estimate based on specs
            # Use m5-like pricing as baseline
            current_hourly = 0.048 * vcpus + 0.006 * ram_gb
            confidence = "low"
            perf_note = f"AWS instance type '{instance_type}' not in mapping. Using spec-based estimate."
        else:
            # Generic/unknown source
            current_hourly = (vcpus * 0.04) + (ram_gb * 0.005)
            confidence = "low"
            perf_note = "Unknown source - using conservative estimate."

        # Calculate OCI costs
        oci_hourly = (oci_ocpu * self.oci_ocpu_hourly) + (oci_ram_gb * self.oci_ram_gb_hourly)

        current_monthly = current_hourly * HOURS_PER_MONTH
        oci_monthly = oci_hourly * HOURS_PER_MONTH
        monthly_savings = current_monthly - oci_monthly
        savings_pct = (monthly_savings / current_monthly * 100) if current_monthly > 0 else 0

        return SizingResult(
            oci_shape=self.default_shape,
            oci_ocpu=oci_ocpu,
            oci_ram_gb=oci_ram_gb,
            current_monthly_cost=round(current_monthly, 2),
            oci_monthly_cost=round(oci_monthly, 2),
            monthly_savings=round(monthly_savings, 2),
            savings_percentage=round(savings_pct, 1),
            sizing_method="generic_calculation",
            perf_note=perf_note,
            confidence=confidence
        )

    def enrich_inventory(self, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Enrich a list of inventory assets with OCI sizing data.

        Args:
            assets: List of asset dictionaries

        Returns:
            List of assets with added 'oci_sizing' key containing SizingResult
        """
        enriched = []

        for asset in assets:
            sizing = self.size_asset(asset)

            # Create enriched copy
            enriched_asset = asset.copy()
            enriched_asset['oci_sizing'] = sizing.to_dict()

            # Add convenience fields at top level
            enriched_asset['oci_shape'] = sizing.oci_shape
            enriched_asset['oci_ocpu'] = sizing.oci_ocpu
            enriched_asset['oci_monthly_cost'] = sizing.oci_monthly_cost
            enriched_asset['monthly_savings'] = sizing.monthly_savings
            enriched_asset['savings_percentage'] = sizing.savings_percentage

            enriched.append(enriched_asset)

        return enriched

    def calculate_summary(self, enriched_assets: List[Dict[str, Any]]) -> MigrationSummary:
        """
        Calculate summary statistics for enriched inventory.

        Args:
            enriched_assets: List of assets that have been enriched with oci_sizing

        Returns:
            MigrationSummary with totals and breakdowns
        """
        total_vcpus = 0
        total_ram_gb = 0
        total_oci_ocpus = 0
        total_oci_ram_gb = 0
        total_current = 0
        total_oci = 0

        aws_count = 0
        vmware_count = 0
        other_count = 0

        high_conf = 0
        med_conf = 0
        low_conf = 0

        for asset in enriched_assets:
            vcpus = asset.get('vcpus') or asset.get('vcpu_count') or 0
            ram_gb = asset.get('ram_gb') or asset.get('memory_gb') or 0

            total_vcpus += vcpus
            total_ram_gb += ram_gb

            sizing = asset.get('oci_sizing', {})
            total_oci_ocpus += sizing.get('oci_ocpu', 0)
            total_oci_ram_gb += sizing.get('oci_ram_gb', 0)
            total_current += sizing.get('current_monthly_cost', 0)
            total_oci += sizing.get('oci_monthly_cost', 0)

            # Count by source
            source = (asset.get('source') or asset.get('source_type') or '').upper()
            if source == 'AWS':
                aws_count += 1
            elif source == 'VMWARE':
                vmware_count += 1
            else:
                other_count += 1

            # Count by confidence
            confidence = sizing.get('confidence', 'low')
            if confidence == 'high':
                high_conf += 1
            elif confidence == 'medium':
                med_conf += 1
            else:
                low_conf += 1

        total_savings = total_current - total_oci
        avg_savings_pct = (total_savings / total_current * 100) if total_current > 0 else 0

        return MigrationSummary(
            total_assets=len(enriched_assets),
            total_vcpus=total_vcpus,
            total_ram_gb=round(total_ram_gb, 2),
            total_oci_ocpus=total_oci_ocpus,
            total_oci_ram_gb=round(total_oci_ram_gb, 2),
            total_current_monthly=round(total_current, 2),
            total_oci_monthly=round(total_oci, 2),
            total_monthly_savings=round(total_savings, 2),
            total_annual_savings=round(total_savings * 12, 2),
            average_savings_percentage=round(avg_savings_pct, 1),
            aws_assets=aws_count,
            vmware_assets=vmware_count,
            other_assets=other_count,
            high_confidence_count=high_conf,
            medium_confidence_count=med_conf,
            low_confidence_count=low_conf
        )

    def get_top_savings_opportunities(self, enriched_assets: List[Dict[str, Any]],
                                       top_n: int = 10) -> List[Dict[str, Any]]:
        """
        Get the top N assets by monthly savings potential.

        Args:
            enriched_assets: List of enriched assets
            top_n: Number of top opportunities to return

        Returns:
            List of top N assets sorted by savings (highest first)
        """
        sorted_assets = sorted(
            enriched_assets,
            key=lambda x: x.get('oci_sizing', {}).get('monthly_savings', 0),
            reverse=True
        )
        return sorted_assets[:top_n]

    def get_quick_wins(self, enriched_assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Get assets that are "quick wins" - high confidence, good savings.

        Returns assets with:
        - High confidence sizing
        - Savings >= 50%
        """
        quick_wins = [
            asset for asset in enriched_assets
            if asset.get('oci_sizing', {}).get('confidence') == 'high'
            and asset.get('oci_sizing', {}).get('savings_percentage', 0) >= 50
        ]

        return sorted(
            quick_wins,
            key=lambda x: x.get('oci_sizing', {}).get('monthly_savings', 0),
            reverse=True
        )


# CLI for testing
if __name__ == '__main__':
    print("ExpressLane Migration Sizer - Test")
    print("=" * 50)

    # Test data
    test_assets = [
        {
            "hostname": "web-server-01",
            "source": "AWS",
            "instance_type": "t3.medium",
            "vcpus": 2,
            "ram_gb": 4
        },
        {
            "hostname": "db-server-01",
            "source": "AWS",
            "instance_type": "m5.xlarge",
            "vcpus": 4,
            "ram_gb": 16
        },
        {
            "hostname": "app-server-01",
            "source": "AWS",
            "instance_type": "r5.large",
            "vcpus": 2,
            "ram_gb": 16
        },
        {
            "hostname": "vmware-vm-01",
            "source": "VMware",
            "instance_type": "General (Medium)",
            "vcpus": 4,
            "ram_gb": 8
        },
        {
            "hostname": "vmware-vm-02",
            "source": "VMware",
            "instance_type": "Memory Opt (Large)",
            "vcpus": 8,
            "ram_gb": 32
        }
    ]

    # Initialize sizer
    sizer = MigrationSizer()

    # Enrich assets
    enriched = sizer.enrich_inventory(test_assets)

    print("\nEnriched Assets:")
    print("-" * 50)
    for asset in enriched:
        sizing = asset['oci_sizing']
        print(f"\n{asset['hostname']} ({asset['source']} - {asset['instance_type']})")
        print(f"  Current Specs: {asset['vcpus']} vCPU, {asset['ram_gb']} GB RAM")
        print(f"  OCI Recommendation: {sizing['oci_shape']}")
        print(f"    -> {sizing['oci_ocpu']} OCPU, {sizing['oci_ram_gb']} GB RAM")
        print(f"  Cost Analysis:")
        print(f"    Current: ${sizing['current_monthly_cost']}/mo")
        print(f"    OCI:     ${sizing['oci_monthly_cost']}/mo")
        print(f"    Savings: ${sizing['monthly_savings']}/mo ({sizing['savings_percentage']}%)")
        print(f"  Confidence: {sizing['confidence']}")
        print(f"  Note: {sizing['perf_note']}")

    # Calculate summary
    summary = sizer.calculate_summary(enriched)

    print("\n" + "=" * 50)
    print("MIGRATION SUMMARY")
    print("=" * 50)
    print(f"Total Assets: {summary.total_assets}")
    print(f"  - AWS: {summary.aws_assets}")
    print(f"  - VMware: {summary.vmware_assets}")
    print(f"\nCompute Totals:")
    print(f"  Current: {summary.total_vcpus} vCPUs, {summary.total_ram_gb} GB RAM")
    print(f"  OCI:     {summary.total_oci_ocpus} OCPUs, {summary.total_oci_ram_gb} GB RAM")
    print(f"\nCost Analysis:")
    print(f"  Current Monthly:  ${summary.total_current_monthly:,.2f}")
    print(f"  OCI Monthly:      ${summary.total_oci_monthly:,.2f}")
    print(f"  Monthly Savings:  ${summary.total_monthly_savings:,.2f} ({summary.average_savings_percentage}%)")
    print(f"  Annual Savings:   ${summary.total_annual_savings:,.2f}")
    print(f"\nConfidence Breakdown:")
    print(f"  High:   {summary.high_confidence_count}")
    print(f"  Medium: {summary.medium_confidence_count}")
    print(f"  Low:    {summary.low_confidence_count}")

    # Quick wins
    quick_wins = sizer.get_quick_wins(enriched)
    print(f"\nQuick Wins (High Confidence, 50%+ Savings): {len(quick_wins)}")
    for qw in quick_wins[:5]:
        print(f"  - {qw['hostname']}: ${qw['monthly_savings']}/mo savings")
