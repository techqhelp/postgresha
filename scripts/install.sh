#!/usr/bin/env bash
# =============================================================================
# install.sh — pgha PostgreSQL HA Heartbeat Manager — Server Installer
#
# Run as root on EACH node (pg-primary and pg-standby):
#
#   sudo bash install.sh
#
# What this script does:
#   1. Installs Python dependencies via pip3
#   2. Installs the pgha Python package
#   3. Installs binaries, config, and systemd unit
#   4. Creates runtime directories (/var/log/pgha, /var/run/pgha)
#   5. Disables the built-in EDB service (pgha manages EDB start/stop)
#   6. Reloads systemd
#
# After running this script:
#   Step 2 — Edit  /etc/pgha/pgha.conf   (set node_name, peer_ip, project_id …)
#   Step 3 — Start systemctl enable pgha && systemctl start pgha
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${YELLOW}=== $* ===${NC}"; }

# --------------------------------------------------------------------------
# Must run as root
# --------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || error "Run this script as root:  sudo bash install.sh"

# --------------------------------------------------------------------------
# Locate the project root (directory containing this script)
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
info "Project directory: ${PROJECT_DIR}"

# --------------------------------------------------------------------------
# Step 1 — Install Python dependencies
# --------------------------------------------------------------------------
section "Installing Python dependencies"

pip3 install \
    "psutil>=5.9.0" \
    "psycopg2-binary==2.9.5" \
    "google-auth>=2.17.0" \
    "requests>=2.28.0"

# Python 3.6 needs the dataclasses backport
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "${PYTHON_MINOR}" -lt 7 ]]; then
    info "Python 3.6 detected — installing dataclasses backport"
    pip3 install "dataclasses"
fi

info "Python dependencies installed"

# --------------------------------------------------------------------------
# Step 2 — Install the pgha Python package
# --------------------------------------------------------------------------
section "Installing pgha Python package"

cd "${PROJECT_DIR}"
python3 setup.py install --quiet
info "pgha package installed"

# --------------------------------------------------------------------------
# Step 3 — Install binaries
# --------------------------------------------------------------------------
section "Installing binaries to /usr/bin"

install -m 755 "${PROJECT_DIR}/bin/pgha-daemon" /usr/bin/pgha-daemon
install -m 755 "${PROJECT_DIR}/bin/pgha-ctl"    /usr/bin/pgha-ctl
info "Installed: /usr/bin/pgha-daemon"
info "Installed: /usr/bin/pgha-ctl"

# --------------------------------------------------------------------------
# Step 4 — Install configuration file (only if not already present)
# --------------------------------------------------------------------------
section "Installing configuration"

mkdir -p /etc/pgha
if [[ -f /etc/pgha/pgha.conf ]]; then
    warn "/etc/pgha/pgha.conf already exists — not overwriting"
    warn "Review and update it manually for this node"
else
    install -m 640 "${PROJECT_DIR}/conf/pgha.conf" /etc/pgha/pgha.conf
    info "Installed: /etc/pgha/pgha.conf"
    warn "IMPORTANT: Edit /etc/pgha/pgha.conf before starting the service"
fi

# --------------------------------------------------------------------------
# Step 5 — Install systemd unit
# --------------------------------------------------------------------------
section "Installing systemd unit"

install -m 644 "${PROJECT_DIR}/systemd/pgha.service" \
    /etc/systemd/system/pgha.service
info "Installed: /etc/systemd/system/pgha.service"

# --------------------------------------------------------------------------
# Step 6 — Create runtime directories
# --------------------------------------------------------------------------
section "Creating runtime directories"

mkdir -p /var/log/pgha
chmod 755 /var/log/pgha
info "Created: /var/log/pgha"

mkdir -p /var/run/pgha
chmod 750 /var/run/pgha
info "Created: /var/run/pgha"

# --------------------------------------------------------------------------
# Step 7 — Disable the built-in EDB service (pgha owns EDB lifecycle)
# --------------------------------------------------------------------------
section "Disabling built-in EDB EPAS 15 service"

if systemctl list-unit-files | grep -q "edb-as-15"; then
    systemctl disable edb-as-15 2>/dev/null || true
    systemctl stop    edb-as-15 2>/dev/null || true
    info "edb-as-15 service disabled — pgha will manage EDB start/stop"
else
    warn "edb-as-15 unit not found — skipping (EDB may not be installed yet)"
fi

# --------------------------------------------------------------------------
# Step 8 — Reload systemd
# --------------------------------------------------------------------------
section "Reloading systemd"

systemctl daemon-reload
info "systemd reloaded"

# --------------------------------------------------------------------------
# Done — print next steps
# --------------------------------------------------------------------------
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  pgha installation complete${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "  NEXT STEP 2 — Edit configuration for THIS node:"
echo ""
echo "    sudo vi /etc/pgha/pgha.conf"
echo ""
echo "  Minimum changes required:"
echo "    [cluster]"
echo "    node_name  = <this-vm-name>    # e.g. pg-primary OR pg-standby"
echo "    peer_ip    = <other-vm-ip>     # internal IP of the other node"
echo ""
echo "    [gcp]"
echo "    project_id = <your-gcp-project>"
echo "    region     = <your-region>     # e.g. us-central1"
echo ""
echo "  NEXT STEP 3 — Enable and start pgha:"
echo ""
echo "    sudo systemctl enable pgha"
echo "    sudo systemctl start pgha"
echo ""
echo "  Start pg-standby BEFORE pg-primary."
echo "  Check status:  pgha-ctl status"
echo "  Live log:      sudo journalctl -u pgha -f"
echo ""
