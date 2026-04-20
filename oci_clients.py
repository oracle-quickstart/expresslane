# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane

Centralized OCI Authentication Module

Provides a single entry point for all OCI client creation.
Auth strategy:
  1. Instance Principals (when running on an OCI Compute instance)
  2. Config file fallback (~/.oci/config for local development)
"""

import logging
import oci
from config import config

logger = logging.getLogger(__name__)

_signer = None
_oci_config = None
_auth_mode = None


def _init_auth():
    """Lazy singleton — resolve auth mode once and cache."""
    global _signer, _oci_config, _auth_mode

    if _auth_mode is not None:
        return

    # Try Instance Principals first (preferred for OCI Compute instances).
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        # The signer constructor succeeds on any OCI instance (IMDS is always
        # reachable), even without a Dynamic Group / Policy.  Validate with a
        # real API call so we don't report a false positive.
        _test_cfg = {'region': config.get('OCM_REGION', 'us-ashburn-1')}
        _id_client = oci.identity.IdentityClient(
            _test_cfg, signer=signer,
            retry_strategy=oci.retry.NoneRetryStrategy(),
        )
        _id_client.get_tenancy(signer.tenancy_id)
        _signer = signer
        _oci_config = {}
        _auth_mode = 'instance_principal'
        logger.info("OCI auth: using Instance Principals")
        return
    except Exception as e:
        logger.debug("Instance Principals not usable: %s", e)
        pass

    # Fall back to config file (~/.oci/config) for local development.
    try:
        _oci_config = oci.config.from_file()
        oci.config.validate_config(_oci_config)
        _signer = None
        _auth_mode = 'config_file'
        logger.info("OCI auth: using config file (~/.oci/config)")
        return
    except Exception:
        pass

    _auth_mode = 'unavailable'
    logger.warning("OCI auth: no valid credentials found")


def get_oci_client(client_class, region=None, **kwargs):
    """
    Create an OCI service client using the resolved auth mode.

    Args:
        client_class: OCI client class (e.g. oci.cloud_migrations.MigrationClient)
        region: Optional region override. Defaults to OCM_REGION from config.
        **kwargs: Extra kwargs forwarded to the client constructor
                  (e.g. retry_strategy=oci.retry.NoneRetryStrategy()).

    Returns:
        An initialized OCI service client.
    """
    _init_auth()

    resolved_region = region or config.get('OCM_REGION', 'us-ashburn-1')

    if _auth_mode == 'instance_principal':
        cfg = {'region': resolved_region}
        return client_class(cfg, signer=_signer, **kwargs)
    elif _auth_mode == 'config_file':
        cfg = dict(_oci_config)
        cfg['region'] = resolved_region
        return client_class(cfg, **kwargs)
    else:
        raise RuntimeError(
            "OCI credentials not available. Configure Instance Principals "
            "or place a valid config at ~/.oci/config"
        )


def get_oci_config(region=None):
    """
    Return a resolved OCI config dict.

    For Instance Principals this is a minimal dict with just region.
    For config file this is the full parsed dict with region overridden.
    Useful for call sites that need raw config values (e.g. tenancy OCID).
    """
    _init_auth()

    resolved_region = region or config.get('OCM_REGION', 'us-ashburn-1')

    if _auth_mode == 'instance_principal':
        return {'region': resolved_region, 'tenancy': _signer.tenancy_id}
    elif _auth_mode == 'config_file':
        cfg = dict(_oci_config)
        cfg['region'] = resolved_region
        return cfg
    else:
        raise RuntimeError(
            "OCI credentials not available. Configure Instance Principals "
            "or place a valid config at ~/.oci/config"
        )


def get_signer():
    """Return the cached signer (None when using config file auth)."""
    _init_auth()
    return _signer


def get_auth_mode():
    """Return the active auth mode string."""
    _init_auth()
    return _auth_mode


def is_oci_configured():
    """Return True if any OCI auth method is available."""
    _init_auth()
    return _auth_mode in ('instance_principal', 'config_file')


def reset_auth():
    """Clear cached auth state (for testing / re-init)."""
    global _signer, _oci_config, _auth_mode
    _signer = None
    _oci_config = None
    _auth_mode = None
