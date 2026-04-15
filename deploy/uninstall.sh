#!/usr/bin/env bash
# ExpressLane uninstaller for Oracle Linux 8/9
# Usage: sudo bash uninstall.sh [--yes]
set -euo pipefail

INSTALL_DIR="/opt/expresslane"
SERVICE_NAME="expresslane"
NGINX_CONF="/etc/nginx/conf.d/expresslane.conf"
TLS_DIR="/etc/pki/tls/expresslane"

# ── Parse arguments ──────────────────────────────────────────────
AUTO_YES=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes) AUTO_YES=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Must be root ─────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)"
    exit 1
fi

# ── Confirmation ─────────────────────────────────────────────────
echo "This will remove:"
echo "  - systemd service: ${SERVICE_NAME}"
echo "  - nginx config:    ${NGINX_CONF}"
echo "  - TLS certificates: ${TLS_DIR}/ (if present)"
echo "  - application dir: ${INSTALL_DIR}/"
echo ""
echo "This will NOT remove:"
echo "  - opc user account"
echo "  - ~/.oci/ configuration"
echo "  - system packages (python3, nginx, etc.)"
echo "  - firewall rules"
echo ""

if [[ "$AUTO_YES" != "true" ]]; then
    read -rp "Continue? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ── Stop and disable service ─────────────────────────────────────
echo "Stopping and disabling ${SERVICE_NAME} service..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

# ── Remove systemd unit ──────────────────────────────────────────
if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    echo "Removing systemd unit..."
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
fi

# ── Remove nginx config ──────────────────────────────────────────
if [[ -f "$NGINX_CONF" ]]; then
    echo "Removing nginx config..."
    rm -f "$NGINX_CONF"
fi

# ── Remove TLS certificates ──────────────────────────────────────
if [[ -d "$TLS_DIR" ]]; then
    echo "Removing TLS certificates..."
    rm -rf "$TLS_DIR"
fi

# ── Restart nginx if running ─────────────────────────────────────
if systemctl is-active --quiet nginx; then
    echo "Restarting nginx..."
    systemctl restart nginx
fi

# ── Remove application directory ─────────────────────────────────
if [[ -d "$INSTALL_DIR" ]]; then
    echo "Removing ${INSTALL_DIR}/..."
    rm -rf "$INSTALL_DIR"
fi

# ── Summary ──────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  ExpressLane has been uninstalled."
echo "================================================"
echo ""
echo "  Removed: service, nginx config, application files"
echo "  Kept:    opc user, ~/.oci/ config, system packages, firewall rules"
echo ""
