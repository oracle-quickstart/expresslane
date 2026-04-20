#!/usr/bin/env python3
# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane

Asset specs extraction for Cloud Bridge assets.

This module provides a clean implementation for extracting VM compute
specifications (CPU, memory, instance type) from OCI Cloud Bridge assets.

Handles both VMware and AWS assets, with special handling for AWS EC2
instances where OCI Cloud Bridge may not populate CPU/RAM directly.
"""

from typing import Dict, Optional, Any
from oci.cloud_bridge import InventoryClient


# AWS EC2 Instance Type Specifications Lookup Map
# Format: 'instance_type': {'cpu': vCPU count, 'ram': memory in GB}
# Reference: https://aws.amazon.com/ec2/instance-types/
AWS_INSTANCE_SPECS = {
    # T2 family (burstable, previous gen)
    't2.nano': {'cpu': 1, 'ram': 0.5},
    't2.micro': {'cpu': 1, 'ram': 1},
    't2.small': {'cpu': 1, 'ram': 2},
    't2.medium': {'cpu': 2, 'ram': 4},
    't2.large': {'cpu': 2, 'ram': 8},
    't2.xlarge': {'cpu': 4, 'ram': 16},
    't2.2xlarge': {'cpu': 8, 'ram': 32},

    # T3 family (burstable)
    't3.nano': {'cpu': 2, 'ram': 0.5},
    't3.micro': {'cpu': 2, 'ram': 1},
    't3.small': {'cpu': 2, 'ram': 2},
    't3.medium': {'cpu': 2, 'ram': 4},
    't3.large': {'cpu': 2, 'ram': 8},
    't3.xlarge': {'cpu': 4, 'ram': 16},
    't3.2xlarge': {'cpu': 8, 'ram': 32},

    # T3a family (burstable, AMD)
    't3a.nano': {'cpu': 2, 'ram': 0.5},
    't3a.micro': {'cpu': 2, 'ram': 1},
    't3a.small': {'cpu': 2, 'ram': 2},
    't3a.medium': {'cpu': 2, 'ram': 4},
    't3a.large': {'cpu': 2, 'ram': 8},
    't3a.xlarge': {'cpu': 4, 'ram': 16},
    't3a.2xlarge': {'cpu': 8, 'ram': 32},

    # T4g family (burstable, ARM/Graviton2)
    't4g.nano': {'cpu': 2, 'ram': 0.5},
    't4g.micro': {'cpu': 2, 'ram': 1},
    't4g.small': {'cpu': 2, 'ram': 2},
    't4g.medium': {'cpu': 2, 'ram': 4},
    't4g.large': {'cpu': 2, 'ram': 8},
    't4g.xlarge': {'cpu': 4, 'ram': 16},
    't4g.2xlarge': {'cpu': 8, 'ram': 32},

    # M4 family (general purpose, previous gen)
    'm4.large': {'cpu': 2, 'ram': 8},
    'm4.xlarge': {'cpu': 4, 'ram': 16},
    'm4.2xlarge': {'cpu': 8, 'ram': 32},
    'm4.4xlarge': {'cpu': 16, 'ram': 64},
    'm4.10xlarge': {'cpu': 40, 'ram': 160},
    'm4.16xlarge': {'cpu': 64, 'ram': 256},

    # M5 family (general purpose)
    'm5.large': {'cpu': 2, 'ram': 8},
    'm5.xlarge': {'cpu': 4, 'ram': 16},
    'm5.2xlarge': {'cpu': 8, 'ram': 32},
    'm5.4xlarge': {'cpu': 16, 'ram': 64},
    'm5.8xlarge': {'cpu': 32, 'ram': 128},
    'm5.12xlarge': {'cpu': 48, 'ram': 192},
    'm5.16xlarge': {'cpu': 64, 'ram': 256},
    'm5.24xlarge': {'cpu': 96, 'ram': 384},
    'm5.metal': {'cpu': 96, 'ram': 384},

    # M5a family (general purpose, AMD)
    'm5a.large': {'cpu': 2, 'ram': 8},
    'm5a.xlarge': {'cpu': 4, 'ram': 16},
    'm5a.2xlarge': {'cpu': 8, 'ram': 32},
    'm5a.4xlarge': {'cpu': 16, 'ram': 64},
    'm5a.8xlarge': {'cpu': 32, 'ram': 128},
    'm5a.12xlarge': {'cpu': 48, 'ram': 192},
    'm5a.16xlarge': {'cpu': 64, 'ram': 256},
    'm5a.24xlarge': {'cpu': 96, 'ram': 384},

    # M5n family (general purpose, network optimized)
    'm5n.large': {'cpu': 2, 'ram': 8},
    'm5n.xlarge': {'cpu': 4, 'ram': 16},
    'm5n.2xlarge': {'cpu': 8, 'ram': 32},
    'm5n.4xlarge': {'cpu': 16, 'ram': 64},
    'm5n.8xlarge': {'cpu': 32, 'ram': 128},
    'm5n.12xlarge': {'cpu': 48, 'ram': 192},
    'm5n.16xlarge': {'cpu': 64, 'ram': 256},
    'm5n.24xlarge': {'cpu': 96, 'ram': 384},

    # M6i family (general purpose, Intel)
    'm6i.large': {'cpu': 2, 'ram': 8},
    'm6i.xlarge': {'cpu': 4, 'ram': 16},
    'm6i.2xlarge': {'cpu': 8, 'ram': 32},
    'm6i.4xlarge': {'cpu': 16, 'ram': 64},
    'm6i.8xlarge': {'cpu': 32, 'ram': 128},
    'm6i.12xlarge': {'cpu': 48, 'ram': 192},
    'm6i.16xlarge': {'cpu': 64, 'ram': 256},
    'm6i.24xlarge': {'cpu': 96, 'ram': 384},
    'm6i.32xlarge': {'cpu': 128, 'ram': 512},
    'm6i.metal': {'cpu': 128, 'ram': 512},

    # M6a family (general purpose, AMD)
    'm6a.large': {'cpu': 2, 'ram': 8},
    'm6a.xlarge': {'cpu': 4, 'ram': 16},
    'm6a.2xlarge': {'cpu': 8, 'ram': 32},
    'm6a.4xlarge': {'cpu': 16, 'ram': 64},
    'm6a.8xlarge': {'cpu': 32, 'ram': 128},
    'm6a.12xlarge': {'cpu': 48, 'ram': 192},
    'm6a.16xlarge': {'cpu': 64, 'ram': 256},
    'm6a.24xlarge': {'cpu': 96, 'ram': 384},
    'm6a.32xlarge': {'cpu': 128, 'ram': 512},
    'm6a.48xlarge': {'cpu': 192, 'ram': 768},

    # M7i family (general purpose, Intel latest)
    'm7i.large': {'cpu': 2, 'ram': 8},
    'm7i.xlarge': {'cpu': 4, 'ram': 16},
    'm7i.2xlarge': {'cpu': 8, 'ram': 32},
    'm7i.4xlarge': {'cpu': 16, 'ram': 64},
    'm7i.8xlarge': {'cpu': 32, 'ram': 128},
    'm7i.12xlarge': {'cpu': 48, 'ram': 192},
    'm7i.16xlarge': {'cpu': 64, 'ram': 256},
    'm7i.24xlarge': {'cpu': 96, 'ram': 384},
    'm7i.48xlarge': {'cpu': 192, 'ram': 768},

    # C4 family (compute optimized, previous gen)
    'c4.large': {'cpu': 2, 'ram': 3.75},
    'c4.xlarge': {'cpu': 4, 'ram': 7.5},
    'c4.2xlarge': {'cpu': 8, 'ram': 15},
    'c4.4xlarge': {'cpu': 16, 'ram': 30},
    'c4.8xlarge': {'cpu': 36, 'ram': 60},

    # C5 family (compute optimized)
    'c5.large': {'cpu': 2, 'ram': 4},
    'c5.xlarge': {'cpu': 4, 'ram': 8},
    'c5.2xlarge': {'cpu': 8, 'ram': 16},
    'c5.4xlarge': {'cpu': 16, 'ram': 32},
    'c5.9xlarge': {'cpu': 36, 'ram': 72},
    'c5.12xlarge': {'cpu': 48, 'ram': 96},
    'c5.18xlarge': {'cpu': 72, 'ram': 144},
    'c5.24xlarge': {'cpu': 96, 'ram': 192},
    'c5.metal': {'cpu': 96, 'ram': 192},

    # C5a family (compute optimized, AMD)
    'c5a.large': {'cpu': 2, 'ram': 4},
    'c5a.xlarge': {'cpu': 4, 'ram': 8},
    'c5a.2xlarge': {'cpu': 8, 'ram': 16},
    'c5a.4xlarge': {'cpu': 16, 'ram': 32},
    'c5a.8xlarge': {'cpu': 32, 'ram': 64},
    'c5a.12xlarge': {'cpu': 48, 'ram': 96},
    'c5a.16xlarge': {'cpu': 64, 'ram': 128},
    'c5a.24xlarge': {'cpu': 96, 'ram': 192},

    # C5n family (compute optimized, network)
    'c5n.large': {'cpu': 2, 'ram': 5.25},
    'c5n.xlarge': {'cpu': 4, 'ram': 10.5},
    'c5n.2xlarge': {'cpu': 8, 'ram': 21},
    'c5n.4xlarge': {'cpu': 16, 'ram': 42},
    'c5n.9xlarge': {'cpu': 36, 'ram': 96},
    'c5n.18xlarge': {'cpu': 72, 'ram': 192},

    # C6i family (compute optimized, Intel)
    'c6i.large': {'cpu': 2, 'ram': 4},
    'c6i.xlarge': {'cpu': 4, 'ram': 8},
    'c6i.2xlarge': {'cpu': 8, 'ram': 16},
    'c6i.4xlarge': {'cpu': 16, 'ram': 32},
    'c6i.8xlarge': {'cpu': 32, 'ram': 64},
    'c6i.12xlarge': {'cpu': 48, 'ram': 96},
    'c6i.16xlarge': {'cpu': 64, 'ram': 128},
    'c6i.24xlarge': {'cpu': 96, 'ram': 192},
    'c6i.32xlarge': {'cpu': 128, 'ram': 256},
    'c6i.metal': {'cpu': 128, 'ram': 256},

    # C6a family (compute optimized, AMD)
    'c6a.large': {'cpu': 2, 'ram': 4},
    'c6a.xlarge': {'cpu': 4, 'ram': 8},
    'c6a.2xlarge': {'cpu': 8, 'ram': 16},
    'c6a.4xlarge': {'cpu': 16, 'ram': 32},
    'c6a.8xlarge': {'cpu': 32, 'ram': 64},
    'c6a.12xlarge': {'cpu': 48, 'ram': 96},
    'c6a.16xlarge': {'cpu': 64, 'ram': 128},
    'c6a.24xlarge': {'cpu': 96, 'ram': 192},
    'c6a.32xlarge': {'cpu': 128, 'ram': 256},
    'c6a.48xlarge': {'cpu': 192, 'ram': 384},

    # C7i family (compute optimized, Intel latest)
    'c7i.large': {'cpu': 2, 'ram': 4},
    'c7i.xlarge': {'cpu': 4, 'ram': 8},
    'c7i.2xlarge': {'cpu': 8, 'ram': 16},
    'c7i.4xlarge': {'cpu': 16, 'ram': 32},
    'c7i.8xlarge': {'cpu': 32, 'ram': 64},
    'c7i.12xlarge': {'cpu': 48, 'ram': 96},
    'c7i.16xlarge': {'cpu': 64, 'ram': 128},
    'c7i.24xlarge': {'cpu': 96, 'ram': 192},
    'c7i.48xlarge': {'cpu': 192, 'ram': 384},

    # R4 family (memory optimized, previous gen)
    'r4.large': {'cpu': 2, 'ram': 15.25},
    'r4.xlarge': {'cpu': 4, 'ram': 30.5},
    'r4.2xlarge': {'cpu': 8, 'ram': 61},
    'r4.4xlarge': {'cpu': 16, 'ram': 122},
    'r4.8xlarge': {'cpu': 32, 'ram': 244},
    'r4.16xlarge': {'cpu': 64, 'ram': 488},

    # R5 family (memory optimized)
    'r5.large': {'cpu': 2, 'ram': 16},
    'r5.xlarge': {'cpu': 4, 'ram': 32},
    'r5.2xlarge': {'cpu': 8, 'ram': 64},
    'r5.4xlarge': {'cpu': 16, 'ram': 128},
    'r5.8xlarge': {'cpu': 32, 'ram': 256},
    'r5.12xlarge': {'cpu': 48, 'ram': 384},
    'r5.16xlarge': {'cpu': 64, 'ram': 512},
    'r5.24xlarge': {'cpu': 96, 'ram': 768},
    'r5.metal': {'cpu': 96, 'ram': 768},

    # R5a family (memory optimized, AMD)
    'r5a.large': {'cpu': 2, 'ram': 16},
    'r5a.xlarge': {'cpu': 4, 'ram': 32},
    'r5a.2xlarge': {'cpu': 8, 'ram': 64},
    'r5a.4xlarge': {'cpu': 16, 'ram': 128},
    'r5a.8xlarge': {'cpu': 32, 'ram': 256},
    'r5a.12xlarge': {'cpu': 48, 'ram': 384},
    'r5a.16xlarge': {'cpu': 64, 'ram': 512},
    'r5a.24xlarge': {'cpu': 96, 'ram': 768},

    # R5n family (memory optimized, network)
    'r5n.large': {'cpu': 2, 'ram': 16},
    'r5n.xlarge': {'cpu': 4, 'ram': 32},
    'r5n.2xlarge': {'cpu': 8, 'ram': 64},
    'r5n.4xlarge': {'cpu': 16, 'ram': 128},
    'r5n.8xlarge': {'cpu': 32, 'ram': 256},
    'r5n.12xlarge': {'cpu': 48, 'ram': 384},
    'r5n.16xlarge': {'cpu': 64, 'ram': 512},
    'r5n.24xlarge': {'cpu': 96, 'ram': 768},

    # R6i family (memory optimized, Intel)
    'r6i.large': {'cpu': 2, 'ram': 16},
    'r6i.xlarge': {'cpu': 4, 'ram': 32},
    'r6i.2xlarge': {'cpu': 8, 'ram': 64},
    'r6i.4xlarge': {'cpu': 16, 'ram': 128},
    'r6i.8xlarge': {'cpu': 32, 'ram': 256},
    'r6i.12xlarge': {'cpu': 48, 'ram': 384},
    'r6i.16xlarge': {'cpu': 64, 'ram': 512},
    'r6i.24xlarge': {'cpu': 96, 'ram': 768},
    'r6i.32xlarge': {'cpu': 128, 'ram': 1024},
    'r6i.metal': {'cpu': 128, 'ram': 1024},

    # R6a family (memory optimized, AMD)
    'r6a.large': {'cpu': 2, 'ram': 16},
    'r6a.xlarge': {'cpu': 4, 'ram': 32},
    'r6a.2xlarge': {'cpu': 8, 'ram': 64},
    'r6a.4xlarge': {'cpu': 16, 'ram': 128},
    'r6a.8xlarge': {'cpu': 32, 'ram': 256},
    'r6a.12xlarge': {'cpu': 48, 'ram': 384},
    'r6a.16xlarge': {'cpu': 64, 'ram': 512},
    'r6a.24xlarge': {'cpu': 96, 'ram': 768},
    'r6a.32xlarge': {'cpu': 128, 'ram': 1024},
    'r6a.48xlarge': {'cpu': 192, 'ram': 1536},

    # I3 family (storage optimized)
    'i3.large': {'cpu': 2, 'ram': 15.25},
    'i3.xlarge': {'cpu': 4, 'ram': 30.5},
    'i3.2xlarge': {'cpu': 8, 'ram': 61},
    'i3.4xlarge': {'cpu': 16, 'ram': 122},
    'i3.8xlarge': {'cpu': 32, 'ram': 244},
    'i3.16xlarge': {'cpu': 64, 'ram': 488},
    'i3.metal': {'cpu': 72, 'ram': 512},

    # I3en family (storage optimized, NVMe)
    'i3en.large': {'cpu': 2, 'ram': 16},
    'i3en.xlarge': {'cpu': 4, 'ram': 32},
    'i3en.2xlarge': {'cpu': 8, 'ram': 64},
    'i3en.3xlarge': {'cpu': 12, 'ram': 96},
    'i3en.6xlarge': {'cpu': 24, 'ram': 192},
    'i3en.12xlarge': {'cpu': 48, 'ram': 384},
    'i3en.24xlarge': {'cpu': 96, 'ram': 768},
    'i3en.metal': {'cpu': 96, 'ram': 768},

    # D2 family (dense storage)
    'd2.xlarge': {'cpu': 4, 'ram': 30.5},
    'd2.2xlarge': {'cpu': 8, 'ram': 61},
    'd2.4xlarge': {'cpu': 16, 'ram': 122},
    'd2.8xlarge': {'cpu': 36, 'ram': 244},

    # D3 family (dense storage)
    'd3.xlarge': {'cpu': 4, 'ram': 32},
    'd3.2xlarge': {'cpu': 8, 'ram': 64},
    'd3.4xlarge': {'cpu': 16, 'ram': 128},
    'd3.8xlarge': {'cpu': 32, 'ram': 256},

    # X1 family (memory optimized, extra large)
    'x1.16xlarge': {'cpu': 64, 'ram': 976},
    'x1.32xlarge': {'cpu': 128, 'ram': 1952},

    # X1e family (memory optimized, extra large)
    'x1e.xlarge': {'cpu': 4, 'ram': 122},
    'x1e.2xlarge': {'cpu': 8, 'ram': 244},
    'x1e.4xlarge': {'cpu': 16, 'ram': 488},
    'x1e.8xlarge': {'cpu': 32, 'ram': 976},
    'x1e.16xlarge': {'cpu': 64, 'ram': 1952},
    'x1e.32xlarge': {'cpu': 128, 'ram': 3904},

    # X2idn family (memory optimized, Intel)
    'x2idn.16xlarge': {'cpu': 64, 'ram': 1024},
    'x2idn.24xlarge': {'cpu': 96, 'ram': 1536},
    'x2idn.32xlarge': {'cpu': 128, 'ram': 2048},
    'x2idn.metal': {'cpu': 128, 'ram': 2048},

    # P3 family (GPU, accelerated computing)
    'p3.2xlarge': {'cpu': 8, 'ram': 61},
    'p3.8xlarge': {'cpu': 32, 'ram': 244},
    'p3.16xlarge': {'cpu': 64, 'ram': 488},

    # P4d family (GPU, accelerated computing)
    'p4d.24xlarge': {'cpu': 96, 'ram': 1152},

    # G4dn family (GPU, graphics)
    'g4dn.xlarge': {'cpu': 4, 'ram': 16},
    'g4dn.2xlarge': {'cpu': 8, 'ram': 32},
    'g4dn.4xlarge': {'cpu': 16, 'ram': 64},
    'g4dn.8xlarge': {'cpu': 32, 'ram': 128},
    'g4dn.12xlarge': {'cpu': 48, 'ram': 192},
    'g4dn.16xlarge': {'cpu': 64, 'ram': 256},
    'g4dn.metal': {'cpu': 96, 'ram': 384},

    # G5 family (GPU, graphics latest)
    'g5.xlarge': {'cpu': 4, 'ram': 16},
    'g5.2xlarge': {'cpu': 8, 'ram': 32},
    'g5.4xlarge': {'cpu': 16, 'ram': 64},
    'g5.8xlarge': {'cpu': 32, 'ram': 128},
    'g5.12xlarge': {'cpu': 48, 'ram': 192},
    'g5.16xlarge': {'cpu': 64, 'ram': 256},
    'g5.24xlarge': {'cpu': 96, 'ram': 384},
    'g5.48xlarge': {'cpu': 192, 'ram': 768},

    # Inf1 family (inference)
    'inf1.xlarge': {'cpu': 4, 'ram': 8},
    'inf1.2xlarge': {'cpu': 8, 'ram': 16},
    'inf1.6xlarge': {'cpu': 24, 'ram': 48},
    'inf1.24xlarge': {'cpu': 96, 'ram': 192},

    # A1 family (ARM/Graviton)
    'a1.medium': {'cpu': 1, 'ram': 2},
    'a1.large': {'cpu': 2, 'ram': 4},
    'a1.xlarge': {'cpu': 4, 'ram': 8},
    'a1.2xlarge': {'cpu': 8, 'ram': 16},
    'a1.4xlarge': {'cpu': 16, 'ram': 32},
    'a1.metal': {'cpu': 16, 'ram': 32},

    # M6g family (general purpose, ARM/Graviton2)
    'm6g.medium': {'cpu': 1, 'ram': 4},
    'm6g.large': {'cpu': 2, 'ram': 8},
    'm6g.xlarge': {'cpu': 4, 'ram': 16},
    'm6g.2xlarge': {'cpu': 8, 'ram': 32},
    'm6g.4xlarge': {'cpu': 16, 'ram': 64},
    'm6g.8xlarge': {'cpu': 32, 'ram': 128},
    'm6g.12xlarge': {'cpu': 48, 'ram': 192},
    'm6g.16xlarge': {'cpu': 64, 'ram': 256},
    'm6g.metal': {'cpu': 64, 'ram': 256},

    # C6g family (compute optimized, ARM/Graviton2)
    'c6g.medium': {'cpu': 1, 'ram': 2},
    'c6g.large': {'cpu': 2, 'ram': 4},
    'c6g.xlarge': {'cpu': 4, 'ram': 8},
    'c6g.2xlarge': {'cpu': 8, 'ram': 16},
    'c6g.4xlarge': {'cpu': 16, 'ram': 32},
    'c6g.8xlarge': {'cpu': 32, 'ram': 64},
    'c6g.12xlarge': {'cpu': 48, 'ram': 96},
    'c6g.16xlarge': {'cpu': 64, 'ram': 128},
    'c6g.metal': {'cpu': 64, 'ram': 128},

    # R6g family (memory optimized, ARM/Graviton2)
    'r6g.medium': {'cpu': 1, 'ram': 8},
    'r6g.large': {'cpu': 2, 'ram': 16},
    'r6g.xlarge': {'cpu': 4, 'ram': 32},
    'r6g.2xlarge': {'cpu': 8, 'ram': 64},
    'r6g.4xlarge': {'cpu': 16, 'ram': 128},
    'r6g.8xlarge': {'cpu': 32, 'ram': 256},
    'r6g.12xlarge': {'cpu': 48, 'ram': 384},
    'r6g.16xlarge': {'cpu': 64, 'ram': 512},
    'r6g.metal': {'cpu': 64, 'ram': 512},

    # M7g family (general purpose, ARM/Graviton3)
    'm7g.medium': {'cpu': 1, 'ram': 4},
    'm7g.large': {'cpu': 2, 'ram': 8},
    'm7g.xlarge': {'cpu': 4, 'ram': 16},
    'm7g.2xlarge': {'cpu': 8, 'ram': 32},
    'm7g.4xlarge': {'cpu': 16, 'ram': 64},
    'm7g.8xlarge': {'cpu': 32, 'ram': 128},
    'm7g.12xlarge': {'cpu': 48, 'ram': 192},
    'm7g.16xlarge': {'cpu': 64, 'ram': 256},
    'm7g.metal': {'cpu': 64, 'ram': 256},

    # C7g family (compute optimized, ARM/Graviton3)
    'c7g.medium': {'cpu': 1, 'ram': 2},
    'c7g.large': {'cpu': 2, 'ram': 4},
    'c7g.xlarge': {'cpu': 4, 'ram': 8},
    'c7g.2xlarge': {'cpu': 8, 'ram': 16},
    'c7g.4xlarge': {'cpu': 16, 'ram': 32},
    'c7g.8xlarge': {'cpu': 32, 'ram': 64},
    'c7g.12xlarge': {'cpu': 48, 'ram': 96},
    'c7g.16xlarge': {'cpu': 64, 'ram': 128},
    'c7g.metal': {'cpu': 64, 'ram': 128},

    # R7g family (memory optimized, ARM/Graviton3)
    'r7g.medium': {'cpu': 1, 'ram': 8},
    'r7g.large': {'cpu': 2, 'ram': 16},
    'r7g.xlarge': {'cpu': 4, 'ram': 32},
    'r7g.2xlarge': {'cpu': 8, 'ram': 64},
    'r7g.4xlarge': {'cpu': 16, 'ram': 128},
    'r7g.8xlarge': {'cpu': 32, 'ram': 256},
    'r7g.12xlarge': {'cpu': 48, 'ram': 384},
    'r7g.16xlarge': {'cpu': 64, 'ram': 512},
    'r7g.metal': {'cpu': 64, 'ram': 512},
}


def get_aws_specs_from_instance_type(instance_type: str) -> Optional[Dict[str, Any]]:
    """
    Look up CPU and RAM specs for an AWS instance type.

    Args:
        instance_type: AWS EC2 instance type (e.g., 't2.micro', 'm5.large')

    Returns:
        Dictionary with 'cpu' and 'ram' keys, or None if not found
    """
    if not instance_type:
        return None

    # Normalize to lowercase for lookup
    instance_type_lower = instance_type.lower().strip()

    return AWS_INSTANCE_SPECS.get(instance_type_lower)


def _get_instance_type(asset, compute) -> Optional[str]:
    """
    Extract AWS instance type from asset/compute objects.

    Checks in order:
    1. compute.hardware_type
    2. asset.aws_ec2.instance_type (SDK 2.160.0+)
    3. freeform_tags
    4. Parse from display name (last resort)

    Returns:
        Instance type string or None
    """
    instance_type = None

    # 1. Check compute.hardware_type
    if compute:
        hardware_type = getattr(compute, 'hardware_type', None)
        if hardware_type and isinstance(hardware_type, str) and hardware_type.strip():
            instance_type = hardware_type.strip()

    # 2. Check aws_ec2.instance_type (SDK 2.160.0+)
    if not instance_type and hasattr(asset, 'aws_ec2') and asset.aws_ec2:
        aws_instance_type = getattr(asset.aws_ec2, 'instance_type', None)
        if aws_instance_type and isinstance(aws_instance_type, str) and aws_instance_type.strip():
            instance_type = aws_instance_type.strip()

    # 3. Check freeform_tags
    if not instance_type and hasattr(asset, 'freeform_tags') and asset.freeform_tags:
        tags = asset.freeform_tags
        tag_keys = ['aws_instance_type', 'instance_type', 'instanceType']
        for key in tag_keys:
            if key in tags and tags[key]:
                instance_type = str(tags[key]).strip()
                break

    # 4. Parse from display name (last resort)
    if not instance_type and hasattr(asset, 'display_name'):
        instance_type = _parse_instance_type_from_name(asset.display_name)

    return instance_type


def _parse_instance_type_from_name(name: str) -> Optional[str]:
    """
    Attempt to parse AWS instance type from VM display name.

    Args:
        name: VM display name

    Returns:
        Instance type string if found, None otherwise
    """
    if not name:
        return None

    name_lower = name.lower()

    # Check against known instance type prefixes
    for instance_type in AWS_INSTANCE_SPECS.keys():
        if instance_type in name_lower:
            return instance_type

    return None


def get_vm_compute_specs(inventory_client: InventoryClient, asset_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract VM compute specifications from a Cloud Bridge asset.

    Args:
        inventory_client: OCI Cloud Bridge InventoryClient instance
        asset_id: Asset OCID

    Returns:
        Dictionary with VM specs or None if asset is not a VM:
        {
            'name': str,
            'source_type': str,  # 'AWS', 'VMware', or 'Unknown'
            'cpu_count': int or None,
            'memory_gb': float or None,
            'instance_type': str or None,  # AWS instance type if available
            'cpu_model': str or None,
            'operating_system': str or None,
            'primary_ip': str or None,
            'power_state': str or None,
            'specs_from_lookup': bool  # True if specs came from AWS lookup table
        }

    Raises:
        Exception: If asset fetch fails
    """
    # Fetch full asset details
    asset = inventory_client.get_asset(asset_id).data

    # Initialize specs dictionary
    specs = {
        'name': asset.display_name,
        'source_type': 'Unknown',
        'cpu_count': None,
        'memory_gb': None,
        'instance_type': None,
        'cpu_model': None,
        'operating_system': None,
        'primary_ip': None,
        'power_state': None,
        'specs_from_lookup': False
    }

    # Skip non-VM assets (volumes, etc.)
    external_key = getattr(asset, 'external_asset_key', '')
    if external_key.startswith('vol-'):
        return None  # This is a volume, not a VM

    # Determine source type
    is_aws = False

    # Check external key for AWS indicators
    if external_key.startswith('i-') or external_key.startswith('ami-'):
        is_aws = True

    # Check asset_source_ids for AWS indicators
    if hasattr(asset, 'asset_source_ids') and asset.asset_source_ids:
        for source_id in asset.asset_source_ids:
            if source_id and 'aws' in source_id.lower():
                is_aws = True
                break

    # Check source_key for AWS region patterns
    source_key = getattr(asset, 'source_key', '').lower()
    if any(region in source_key for region in ['us-east', 'us-west', 'eu-', 'ap-', 'sa-', 'ca-', 'me-', 'af-']):
        is_aws = True

    # Check asset_type for AWS
    asset_type = getattr(asset, 'asset_type', '')
    if asset_type == 'AWS_EC2':
        is_aws = True

    if is_aws:
        specs['source_type'] = 'AWS'
    elif asset_type == 'VMWARE_VM':
        specs['source_type'] = 'VMware'
    elif asset_type == 'VM':
        specs['source_type'] = 'Generic VM'
    else:
        specs['source_type'] = str(asset_type) if asset_type else 'Unknown'

    # Get compute object
    compute = getattr(asset, 'compute', None)

    # Extract compute specifications
    if compute:
        # CPU count
        specs['cpu_count'] = getattr(compute, 'cores_count', None)

        # Memory - convert from MB to GB if needed
        memory_mb = getattr(compute, 'memory_in_mbs', None)
        if memory_mb:
            specs['memory_gb'] = round(memory_mb / 1024, 2)

        # Additional properties
        specs['cpu_model'] = getattr(compute, 'cpu_model', None)
        specs['operating_system'] = getattr(compute, 'operating_system', None)
        specs['primary_ip'] = getattr(compute, 'primary_ip', None)
        specs['power_state'] = getattr(compute, 'power_state', None)

    # For AWS assets, get instance type and fill in missing specs from lookup
    if specs['source_type'] == 'AWS':
        specs['instance_type'] = _get_instance_type(asset, compute)

        # If CPU/RAM are missing, use instance type lookup
        if specs['instance_type'] and (specs['cpu_count'] is None or specs['memory_gb'] is None):
            aws_specs = get_aws_specs_from_instance_type(specs['instance_type'])

            if aws_specs:
                if specs['cpu_count'] is None:
                    specs['cpu_count'] = aws_specs['cpu']
                    specs['specs_from_lookup'] = True

                if specs['memory_gb'] is None:
                    specs['memory_gb'] = aws_specs['ram']
                    specs['specs_from_lookup'] = True

    return specs


def get_batch_vm_specs(inventory_client: InventoryClient, asset_ids: list) -> Dict[str, Dict[str, Any]]:
    """
    Extract compute specs for multiple assets.

    Args:
        inventory_client: OCI Cloud Bridge InventoryClient instance
        asset_ids: List of asset OCIDs

    Returns:
        Dictionary mapping asset_id to specs dict (or error dict)
    """
    results = {}

    for asset_id in asset_ids:
        try:
            specs = get_vm_compute_specs(inventory_client, asset_id)
            if specs:
                results[asset_id] = specs
            else:
                results[asset_id] = {
                    'error': 'Not a VM asset (possibly a volume or other resource)'
                }
        except Exception as e:
            results[asset_id] = {
                'error': str(e)
            }

    return results


# Example usage
if __name__ == '__main__':
    """Example of how to use this module."""
    from config import config
    from oci_clients import get_oci_client

    # Initialize inventory client
    inventory_client = get_oci_client(InventoryClient)
    compartment_id = config.get('OCM_TARGET_COMPARTMENT_OCID')

    # List assets
    print("Fetching Cloud Bridge assets...")
    response = inventory_client.list_assets(
        compartment_id=compartment_id,
        lifecycle_state="ACTIVE",
        limit=5
    )

    asset_ids = [asset.id for asset in response.data.items]

    # Get specs for all assets
    print(f"\nExtracting specs for {len(asset_ids)} assets...\n")
    specs_map = get_batch_vm_specs(inventory_client, asset_ids)

    # Print results
    import json
    for asset_id, specs in specs_map.items():
        print(f"Asset: {asset_id}")
        print(json.dumps(specs, indent=2))
        print()
