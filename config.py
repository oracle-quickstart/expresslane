# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""
Project: ExpressLane for Oracle Cloud Migrations
Tagline: The fast path inside Oracle
Lead Architect: Tim McFadden
GitHub: https://github.com/oracle-quickstart/expresslane
"""

import os
import json
import secrets
from pathlib import Path

class Config:
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.config_path = Path(config_file)
        self._config = {}
        self._file_mtime = 0
        self.load_config()

    def load_config(self):
        """Load configuration from file or create with defaults"""
        if self.config_path.exists():
            try:
                self._file_mtime = self.config_path.stat().st_mtime
                with open(self.config_path, 'r') as f:
                    self._config = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config: {e}")
                self._config = {}

        # Set defaults for any missing keys
        self._set_defaults()

    def _check_reload(self):
        """Reload config from disk if the file has been modified by another process"""
        try:
            if self.config_path.exists():
                mtime = self.config_path.stat().st_mtime
                if mtime > self._file_mtime:
                    self.load_config()
        except (IOError, OSError):
            pass

    def _set_defaults(self):
        """Set default configuration values for OCM"""
        defaults = {
            # Oracle Cloud Migrations (OCM) Configuration
            'OCM_REGION': os.getenv('OCM_REGION', 'us-ashburn-1'),
            'OCM_TARGET_COMPARTMENT_OCID': os.getenv('OCM_TARGET_COMPARTMENT_OCID', ''),
            'OCM_ASSET_SOURCE_OCID': os.getenv('OCM_ASSET_SOURCE_OCID', ''),
            'OCM_REPLICATION_BUCKET_OCID': os.getenv('OCM_REPLICATION_BUCKET_OCID', ''),
            'OCM_POLL_INTERVAL_SECONDS': os.getenv('OCM_POLL_INTERVAL_SECONDS', '30'),

            # Auth
            'ADMIN_USERNAME': '',
            'ADMIN_PASSWORD_HASH': '',
            'SECRET_KEY': '',
        }

        for key, value in defaults.items():
            if key not in self._config:
                self._config[key] = value

    def get(self, key, default=None):
        """Get configuration value (auto-reloads if file changed on disk)"""
        self._check_reload()
        return self._config.get(key, default)

    def set(self, key, value):
        """Set configuration value"""
        self._config[key] = value

    def update(self, updates):
        """Update multiple configuration values"""
        self._config.update(updates)

    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self._config, f, indent=2)
            os.chmod(self.config_path, 0o600)
            return True
        except IOError as e:
            print(f"Error saving config: {e}")
            return False

    def is_configured(self):
        """Check if minimum required config is set"""
        self._check_reload()
        required = ['OCM_REGION', 'OCM_TARGET_COMPARTMENT_OCID', 'OCM_ASSET_SOURCE_OCID']
        return all(self._config.get(k) for k in required)

    def is_admin_configured(self):
        """Check if admin credentials have been set"""
        self._check_reload()
        return bool(self._config.get('ADMIN_USERNAME')) and bool(self._config.get('ADMIN_PASSWORD_HASH'))

    def get_all(self):
        """Get all configuration values"""
        self._check_reload()
        return self._config.copy()

    def get_display_config(self):
        """Get config for display, stripping sensitive fields"""
        self._check_reload()
        display = self._config.copy()
        display.pop('ADMIN_PASSWORD_HASH', None)
        display.pop('SECRET_KEY', None)
        return display

def is_oci_cli_configured():
    """Check if OCI auth is available (Instance Principals or ~/.oci/config)."""
    from oci_clients import is_oci_configured
    return is_oci_configured()

# Global configuration instance
config = Config()
