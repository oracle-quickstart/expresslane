#!/usr/bin/env bash
# ExpressLane Podman installer for Oracle Linux 9
#
# Usage:
#   sudo bash deploy/podman-deploy.sh
#   sudo bash deploy/podman-deploy.sh --fqdn HOSTNAME \
#                                     --tls-cert /path/to/fullchain.pem \
#                                     --tls-key  /path/to/privkey.pem
#
# Runs from the extracted release tree (i.e., from inside ./expresslane/).
# Installs podman + podman-compose if missing, seeds config.json and .env,
# makes sure bind-mount directories have correct ownership, and brings up
# the stack. Idempotent — safe to re-run to upgrade in place.

set -euo pipefail

# ── must run as root ────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run with sudo — rootful podman needs to bind ports 80/443."
    echo "  sudo bash deploy/podman-deploy.sh"
    exit 1
fi

# ── parse args ──────────────────────────────────────────────────
SOURCE_DIR=""
FQDN=""
TLS_CERT=""
TLS_KEY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)   SOURCE_DIR="$2"; shift 2 ;;
        --fqdn)     FQDN="$2";       shift 2 ;;
        --tls-cert) TLS_CERT="$2";   shift 2 ;;
        --tls-key)  TLS_KEY="$2";    shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Default source: the script's parent directory (expresslane/)
if [[ -z "$SOURCE_DIR" ]]; then
    SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi
if [[ ! -f "$SOURCE_DIR/docker-compose.yml" ]]; then
    echo "ERROR: docker-compose.yml not found in $SOURCE_DIR"
    exit 1
fi

# All-or-none TLS trio
ENABLE_TLS=false
if [[ -n "$FQDN" || -n "$TLS_CERT" || -n "$TLS_KEY" ]]; then
    if [[ -z "$FQDN" || -z "$TLS_CERT" || -z "$TLS_KEY" ]]; then
        echo "ERROR: --fqdn, --tls-cert, and --tls-key must all be provided together."
        exit 1
    fi
    [[ -f "$TLS_CERT" ]] || { echo "ERROR: TLS cert not found: $TLS_CERT"; exit 1; }
    [[ -f "$TLS_KEY"  ]] || { echo "ERROR: TLS key not found: $TLS_KEY";   exit 1; }
    ENABLE_TLS=true
fi

# Resolve the non-root user who should own the runtime files.
HOST_USER="${SUDO_USER:-opc}"
HOST_UID=$(id -u "$HOST_USER")
HOST_GID=$(id -g "$HOST_USER")
HOST_HOME=$(getent passwd "$HOST_USER" | cut -d: -f6)

NEW_VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' "$SOURCE_DIR/version.py" 2>/dev/null || echo "unknown")

cd "$SOURCE_DIR"

echo "================================================"
echo "  ExpressLane v${NEW_VERSION} — Podman Installer"
echo "  Source:   $SOURCE_DIR"
echo "  Run as:   $HOST_USER (${HOST_UID}:${HOST_GID})"
[[ "$ENABLE_TLS" == "true" ]] && echo "  TLS:      $FQDN"
echo "================================================"

# ── [1/4] Install podman + podman-compose ──────────────────────
echo ""
echo "[1/4] Installing podman + podman-compose (if missing)..."

if ! command -v podman &>/dev/null; then
    dnf install -y podman podman-docker
fi

if ! command -v podman-compose &>/dev/null; then
    dnf install -y python3-pip
    pip3 install --quiet podman-compose
    ln -sf /usr/local/bin/podman-compose /usr/bin/podman-compose
fi

echo "  podman:         $(podman --version)"
echo "  podman-compose: $(podman-compose --version 2>&1 | head -1)"

# ── [2/4] Seed config + runtime layout ──────────────────────────
echo ""
echo "[2/4] Seeding config.json, .env, and runtime directories..."

if [[ ! -f config.json ]]; then
    cp config.json.example config.json
    chown "$HOST_USER:$HOST_USER" config.json
    chmod 0600 config.json
    echo "  created config.json from template"
else
    echo "  config.json already exists — left alone"
fi

cat > .env <<EOF
UID=$HOST_UID
GID=$HOST_GID
EOF
chown "$HOST_USER:$HOST_USER" .env

# Runtime dirs: ship empty in the release, but make sure ownership is
# correct even if someone ran a previous install as root.
for d in instance cache certs; do
    mkdir -p "$d"
    chown -R "$HOST_USER:$HOST_USER" "$d"
done

# OCI config dir for Instance Principals fallback
mkdir -p "$HOST_HOME/.oci"
chown "$HOST_USER:$HOST_USER" "$HOST_HOME/.oci"

# TLS wiring
if [[ "$ENABLE_TLS" == "true" ]]; then
    cp "$TLS_CERT" certs/fullchain.pem
    cp "$TLS_KEY"  certs/privkey.pem
    chmod 0644 certs/fullchain.pem
    chmod 0600 certs/privkey.pem
    chown -R "$HOST_USER:$HOST_USER" certs
    # Render the SSL nginx conf with FQDN substituted
    sed "s/EXPRESSLANE_FQDN/$FQDN/g" deploy/nginx-docker-ssl.conf \
        > deploy/nginx-docker-ssl-live.conf
    chown "$HOST_USER:$HOST_USER" deploy/nginx-docker-ssl-live.conf
    # Append TLS-mode env vars (don't duplicate if re-run)
    grep -q '^SECURE_COOKIES=' .env || echo 'SECURE_COOKIES=true' >> .env
    grep -q '^NGINX_CONF='     .env || echo 'NGINX_CONF=./deploy/nginx-docker-ssl-live.conf' >> .env
    echo "  TLS configured for $FQDN"
fi

# ── [3/4] Build + start the stack ───────────────────────────────
echo ""
echo "[3/4] Building and starting the stack..."

podman-compose build
podman-compose up -d

# ── [4/4] Wait for healthcheck ──────────────────────────────────
echo ""
echo "[4/4] Waiting for app container to become healthy..."

for i in $(seq 1 60); do
    STATUS=$(podman inspect expresslane-app --format '{{.State.Health.Status}}' 2>/dev/null || echo 'unknown')
    if [[ "$STATUS" == "healthy" ]]; then break; fi
    sleep 2
done

HEALTH=$(podman inspect expresslane-app --format '{{.State.Health.Status}}' 2>/dev/null || echo 'unknown')

echo ""
echo "================================================"
echo "  ExpressLane v${NEW_VERSION} — Installation Complete!"
echo "================================================"
echo ""
if [[ "$HEALTH" == "healthy" ]]; then
    echo "  ExpressLane app: RUNNING (healthy)"
else
    echo "  ExpressLane app: $HEALTH  — check: sudo podman logs expresslane-app"
fi

if podman inspect expresslane-nginx --format '{{.State.Status}}' 2>/dev/null | grep -q running; then
    echo "  nginx:           RUNNING"
else
    echo "  nginx:           NOT RUNNING — check: sudo podman logs expresslane-nginx"
fi
echo ""

INTERNAL_IP=$(hostname -I | awk '{print $1}')
EXTERNAL_IP=$(curl -s --max-time 5 http://169.254.169.254/opc/v1/vnics/ 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0].get('publicIp',''))" 2>/dev/null || echo "")
if [[ -z "$EXTERNAL_IP" ]]; then
    EXTERNAL_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || echo "")
fi

if [[ "$ENABLE_TLS" == "true" ]]; then
    echo "  Internal:  https://$INTERNAL_IP/"
    [[ -n "$EXTERNAL_IP" ]] && echo "  External:  https://$EXTERNAL_IP/"
else
    echo "  Internal:  http://$INTERNAL_IP/"
    [[ -n "$EXTERNAL_IP" ]] && echo "  External:  http://$EXTERNAL_IP/"
fi

echo ""
echo "  Ensure ports 80/443 are open in your OCI Security List / NSG."
echo ""
echo "  Logs:      sudo podman logs -f expresslane-app"
echo "  Restart:   sudo podman-compose restart app"
echo "  Uninstall: sudo podman-compose down"
echo "================================================"
