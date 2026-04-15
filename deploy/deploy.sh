#!/usr/bin/env bash
# ExpressLane bare-metal installer for Oracle Linux 8/9
# Usage: sudo bash deploy.sh [--source /path/to/vm_migrator_oci]
#                             [--fqdn HOSTNAME]
#                             [--tls-cert /path/to/fullchain.pem]
#                             [--tls-key  /path/to/privkey.pem]
set -euo pipefail

INSTALL_DIR="/opt/expresslane"
SERVICE_NAME="expresslane"
NGINX_CONF_NAME="expresslane"

# ── Parse arguments ──────────────────────────────────────────────
SOURCE_DIR=""
FQDN=""
TLS_CERT=""
TLS_KEY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)   SOURCE_DIR="$2"; shift 2 ;;
        --fqdn)     FQDN="$2";      shift 2 ;;
        --tls-cert) TLS_CERT="$2";   shift 2 ;;
        --tls-key)  TLS_KEY="$2";    shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Validate FQDN if provided (prevent sed metacharacter injection)
if [[ -n "$FQDN" ]]; then
    if [[ ${#FQDN} -gt 253 ]]; then
        echo "ERROR: FQDN exceeds 253 characters: $FQDN"
        exit 1
    fi
    if [[ ! "$FQDN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]]; then
        echo "ERROR: Invalid FQDN: $FQDN"
        echo "FQDN must contain only letters, digits, hyphens, and dots."
        exit 1
    fi
fi

if [[ -z "$SOURCE_DIR" ]]; then
    # Default: script is in deploy/ inside the source tree
    SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi

if [[ ! -f "$SOURCE_DIR/app.py" ]]; then
    echo "ERROR: Cannot find app.py in $SOURCE_DIR"
    echo "Usage: sudo bash deploy.sh --source /path/to/vm_migrator_oci"
    exit 1
fi

# ── Validate TLS arguments ──────────────────────────────────────
ENABLE_TLS=false
if [[ -n "$FQDN" || -n "$TLS_CERT" || -n "$TLS_KEY" ]]; then
    if [[ -z "$FQDN" || -z "$TLS_CERT" || -z "$TLS_KEY" ]]; then
        echo "ERROR: --fqdn, --tls-cert, and --tls-key must all be provided together."
        exit 1
    fi
    if [[ ! -f "$TLS_CERT" ]]; then
        echo "ERROR: TLS certificate not found: $TLS_CERT"
        exit 1
    fi
    if [[ ! -f "$TLS_KEY" ]]; then
        echo "ERROR: TLS private key not found: $TLS_KEY"
        exit 1
    fi
    ENABLE_TLS=true
fi

NEW_VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' "$SOURCE_DIR/version.py" 2>/dev/null || echo "unknown")
OLD_VERSION=""
if [[ -f "$INSTALL_DIR/version.py" ]]; then
    OLD_VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' "$INSTALL_DIR/version.py" || echo "")
fi

echo "================================================"
echo "  ExpressLane Installer"
echo "  Source: $SOURCE_DIR"
echo "  Target: $INSTALL_DIR"
if [[ -n "$OLD_VERSION" ]]; then
    echo "  Upgrade:  v${OLD_VERSION} -> v${NEW_VERSION}"
else
    echo "  Version:  v${NEW_VERSION}"
fi
if [[ "$ENABLE_TLS" == "true" ]]; then
    echo "  TLS:    ENABLED ($FQDN)"
fi
echo "================================================"

# ── Must be root ─────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)"
    exit 1
fi

# ── Detect Oracle Linux version ──────────────────────────────────
if [[ ! -f /etc/oracle-release ]] && [[ ! -f /etc/redhat-release ]]; then
    echo "WARNING: This script is designed for Oracle Linux 8/9."
    echo "Proceeding anyway..."
fi

OS_VERSION=$(rpm -E %{rhel} 2>/dev/null || echo "9")
echo "Detected EL version: $OS_VERSION"

# ── Install system packages ──────────────────────────────────────
echo ""
echo "[1/7] Installing system packages..."

if [[ "$OS_VERSION" == "8" ]]; then
    dnf install -y python39 python39-pip python39-devel nginx
    PYTHON_BIN="python3.9"
else
    dnf install -y python3 python3-pip python3-devel nginx
    PYTHON_BIN="python3"
fi

# Verify Python version is 3.9+
PY_VERSION=$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python version: $PY_VERSION"

# ── Copy application files ───────────────────────────────────────
echo ""
echo "[2/7] Installing application to $INSTALL_DIR..."

mkdir -p "$INSTALL_DIR"

# rsync app files, preserving existing config/data on upgrades
rsync -a --delete \
    --exclude='config.json' \
    --exclude='instance/' \
    --exclude='cache/' \
    --exclude='venv/' \
    --exclude='.claude/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.git/' \
    --exclude='deploy/' \
    --exclude='.dockerignore' \
    --exclude='docker-compose.yml' \
    "$SOURCE_DIR/" "$INSTALL_DIR/"

# Copy deploy files (needed for nginx conf and service file)
mkdir -p "$INSTALL_DIR/deploy"
cp "$SOURCE_DIR/deploy/expresslane.service" "$INSTALL_DIR/deploy/"
cp "$SOURCE_DIR/deploy/nginx-expresslane.conf" "$INSTALL_DIR/deploy/"
cp "$SOURCE_DIR/deploy/nginx-expresslane-ssl.conf" "$INSTALL_DIR/deploy/"

# Ensure writable directories exist
mkdir -p "$INSTALL_DIR/instance"
mkdir -p "$INSTALL_DIR/cache"

# Set ownership and ensure nginx can traverse to static/
chown -R opc:opc "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"

# ── Create virtual environment ───────────────────────────────────
echo ""
echo "[3/7] Setting up Python virtual environment..."

# Recreate venv if missing or broken
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    echo "Creating fresh virtual environment..."
    rm -rf "$INSTALL_DIR/venv"
    sudo -u opc $PYTHON_BIN -m venv "$INSTALL_DIR/venv"
fi

sudo -u opc "$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
sudo -u opc "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
echo "Python dependencies installed."

# ── Install systemd service ──────────────────────────────────────
echo ""
echo "[4/7] Installing systemd service..."

cp "$INSTALL_DIR/deploy/expresslane.service" /etc/systemd/system/${SERVICE_NAME}.service

# Inject SECURE_COOKIES=true when TLS is enabled
if [[ "$ENABLE_TLS" == "true" ]]; then
    sed -i '/^\[Service\]/a Environment="SECURE_COOKIES=true"' /etc/systemd/system/${SERVICE_NAME}.service
    echo "Injected SECURE_COOKIES=true into service unit."
fi

systemctl daemon-reload

# ── Install nginx config ─────────────────────────────────────────
echo ""
echo "[5/7] Configuring nginx..."

# Remove default nginx server block if it exists
rm -f /etc/nginx/conf.d/default.conf

if [[ "$ENABLE_TLS" == "true" ]]; then
    # Install TLS certificates
    mkdir -p /etc/pki/tls/expresslane
    cp "$TLS_CERT" /etc/pki/tls/expresslane/fullchain.pem
    cp "$TLS_KEY"  /etc/pki/tls/expresslane/privkey.pem
    chmod 0644 /etc/pki/tls/expresslane/fullchain.pem
    chmod 0600 /etc/pki/tls/expresslane/privkey.pem
    echo "TLS certificates installed to /etc/pki/tls/expresslane/"

    # Install SSL nginx config with FQDN substituted
    sed "s/EXPRESSLANE_FQDN/${FQDN}/g" \
        "$INSTALL_DIR/deploy/nginx-expresslane-ssl.conf" \
        > /etc/nginx/conf.d/${NGINX_CONF_NAME}.conf
    echo "nginx: SSL config installed for ${FQDN}"
else
    cp "$INSTALL_DIR/deploy/nginx-expresslane.conf" /etc/nginx/conf.d/${NGINX_CONF_NAME}.conf
fi

# Test nginx config
nginx -t

# ── SELinux & Firewall ───────────────────────────────────────────
echo ""
echo "[6/7] Configuring SELinux and firewall..."

# Allow nginx to connect to gunicorn
if command -v setsebool &>/dev/null; then
    setsebool -P httpd_can_network_connect 1 2>/dev/null || true
    echo "SELinux: httpd_can_network_connect = on"
fi

# Label static files so nginx can serve them directly
if command -v semanage &>/dev/null; then
    semanage fcontext -a -t httpd_sys_content_t "$INSTALL_DIR/static(/.*)?" 2>/dev/null || true
    restorecon -Rv "$INSTALL_DIR/static" 2>/dev/null || true
    echo "SELinux: static files labeled httpd_sys_content_t"
fi

# Open HTTP/HTTPS ports
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=http 2>/dev/null || true
    firewall-cmd --permanent --add-service=https 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    echo "Firewall: ports 80/443 opened"
else
    echo "Firewall: firewalld not running, skipping"
fi

# ── Start services ───────────────────────────────────────────────
echo ""
echo "[7/7] Starting services..."

systemctl enable --now ${SERVICE_NAME}
systemctl enable nginx
# Reload (not just --now) so a running nginx actually picks up conf.d/expresslane.conf.
# --now is a no-op when nginx is already running, which is how users hit the OL
# default test page after uninstall+reinstall.
systemctl reload nginx || systemctl restart nginx

# Wait a moment for gunicorn to start
sleep 2

# ── Status check ─────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  ExpressLane v${NEW_VERSION} — Installation Complete!"
echo "================================================"
echo ""

if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "  ExpressLane service: RUNNING"
else
    echo "  ExpressLane service: FAILED (check: journalctl -u ${SERVICE_NAME})"
fi

if systemctl is-active --quiet nginx; then
    echo "  nginx:               RUNNING"
else
    echo "  nginx:               FAILED (check: journalctl -u nginx)"
fi

# Get IP addresses
INTERNAL_IP=$(hostname -I | awk '{print $1}')
# Try IMDS first, then fall back to external lookup
EXTERNAL_IP=$(curl -s --max-time 5 http://169.254.169.254/opc/v1/vnics/ 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0].get('publicIp',''))" 2>/dev/null || echo "")
if [[ -z "$EXTERNAL_IP" ]]; then
    EXTERNAL_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || echo "")
fi

echo ""
if [[ "$ENABLE_TLS" == "true" ]]; then
    echo "  URL:      https://${FQDN}/"
    echo "  Internal: https://${INTERNAL_IP}/ (cert mismatch expected)"
    if [[ -n "$EXTERNAL_IP" ]]; then
        echo "  External: https://${EXTERNAL_IP}/ (cert mismatch expected)"
    fi
    echo ""
    echo "  IMPORTANT: Ensure DNS for ${FQDN} points to"
    echo "  ${EXTERNAL_IP:-<your public IP>}"
    echo ""
    echo "  IMPORTANT: Ensure ports 80 and 443 are open in your"
    echo "  OCI Security List / Network Security Group."
else
    echo "  Internal: http://${INTERNAL_IP}/"
    if [[ -n "$EXTERNAL_IP" ]]; then
        echo "  External: http://${EXTERNAL_IP}/"
    else
        echo "  External: (no public IP detected)"
    fi
    echo ""
    echo "  IMPORTANT: Ensure port 80 is open in your OCI"
    echo "  Security List / Network Security Group."
fi
echo ""
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Restart: sudo systemctl restart ${SERVICE_NAME}"
echo "================================================"
