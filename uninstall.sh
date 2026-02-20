#!/bin/bash
# ============================================================
# Remote Claw — Uninstall Script
# ============================================================
#
# Usage:
#   ./uninstall.sh          Interactive mode (asks what to remove)
#   ./uninstall.sh dev      Remove local development environment
#   ./uninstall.sh pi       Remove Pi 5 production deployment
#   ./uninstall.sh all      Remove everything (dev + pi)
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

confirm() {
    local msg="$1"
    echo -n -e "  ${YELLOW}$msg${NC} [y/N] "
    read -r answer
    [[ "$answer" =~ ^[Yy] ]]
}

# ---- Dev Uninstall ---------------------------------------------------------

uninstall_dev() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Remove Development Environment${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    local removed=false

    # Virtual environment
    if [ -d "$SCRIPT_DIR/venv" ]; then
        info "Found virtual environment at ./venv"
        if confirm "Remove virtual environment?"; then
            rm -rf "$SCRIPT_DIR/venv"
            ok "Virtual environment removed"
            removed=true
        fi
    else
        ok "No virtual environment found"
    fi

    # .env file
    if [ -f "$SCRIPT_DIR/.env" ]; then
        info "Found .env configuration file"
        if confirm "Remove .env file?"; then
            rm -f "$SCRIPT_DIR/.env"
            ok ".env removed"
            removed=true
        fi
    else
        ok "No .env file found"
    fi

    # Data directory
    if [ -d "$SCRIPT_DIR/data" ]; then
        info "Found data directory (may contain database)"
        if confirm "Remove data directory (includes database)?"; then
            rm -rf "$SCRIPT_DIR/data"
            ok "Data directory removed"
            removed=true
        fi
    else
        ok "No data directory found"
    fi

    # Python cache
    if find "$SCRIPT_DIR" -type d -name "__pycache__" 2>/dev/null | grep -q .; then
        info "Found Python cache files"
        find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        rm -rf "$SCRIPT_DIR/.pytest_cache"
        ok "Cache files removed"
        removed=true
    fi

    echo ""
    if $removed; then
        echo -e "${GREEN}  Development environment cleaned up.${NC}"
    else
        echo -e "${BLUE}  Nothing to remove.${NC}"
    fi
    echo ""
}

# ---- Pi 5 Uninstall --------------------------------------------------------

uninstall_pi() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Remove Pi 5 Deployment${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    if ! command -v sudo &>/dev/null; then
        fail "sudo is required for Pi 5 uninstall."
    fi

    echo -e "  ${RED}This will remove the Remote Claw production deployment.${NC}"
    echo ""
    if ! confirm "Are you sure you want to continue?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    # [1/5] Stop and disable services
    echo -e "${BOLD}[1/5] Stopping and disabling services...${NC}"
    for svc in claw-watchdog claw-server mediamtx; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop "$svc"
            ok "Stopped $svc"
        fi
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl disable "$svc" 2>/dev/null
            ok "Disabled $svc"
        fi
    done
    # Clear failure state / start-limit counters for a clean slate
    sudo systemctl reset-failed claw-watchdog claw-server mediamtx 2>/dev/null || true

    # [2/5] Remove systemd service files
    echo ""
    echo -e "${BOLD}[2/5] Removing systemd service files...${NC}"
    for unit in claw-server.service claw-watchdog.service mediamtx.service; do
        if [ -f "/etc/systemd/system/$unit" ]; then
            sudo rm -f "/etc/systemd/system/$unit"
            ok "Removed /etc/systemd/system/$unit"
        fi
    done
    sudo systemctl daemon-reload
    ok "Systemd daemon reloaded"

    # [3/5] Remove nginx config
    echo ""
    echo -e "${BOLD}[3/5] Removing nginx configuration...${NC}"
    if [ -f /etc/nginx/sites-available/claw ]; then
        sudo rm -f /etc/nginx/sites-enabled/claw
        sudo rm -f /etc/nginx/sites-available/claw
        ok "nginx Remote Claw config removed"
        if sudo nginx -t 2>/dev/null; then
            sudo systemctl reload nginx 2>/dev/null || true
            ok "nginx reloaded"
        fi
    else
        ok "No nginx Remote Claw config found"
    fi

    # [4/5] Remove application directory
    echo ""
    echo -e "${BOLD}[4/5] Removing application files...${NC}"
    if [ -d /opt/claw ]; then
        if confirm "Remove /opt/claw (application + database)?"; then
            sudo rm -rf /opt/claw
            ok "Removed /opt/claw"
        fi
    else
        ok "/opt/claw not found"
    fi

    # Remove MediaMTX config
    if [ -f /etc/mediamtx.yml ]; then
        sudo rm -f /etc/mediamtx.yml
        ok "Removed /etc/mediamtx.yml"
    fi

    # Remove MediaMTX binary
    if [ -f /usr/local/bin/mediamtx ]; then
        if confirm "Remove MediaMTX binary (/usr/local/bin/mediamtx)?"; then
            sudo rm -f /usr/local/bin/mediamtx
            ok "Removed MediaMTX binary"
        fi
    fi

    # [5/5] Remove system users (optional)
    echo ""
    echo -e "${BOLD}[5/5] System users...${NC}"
    for user in claw mediamtx; do
        if id "$user" &>/dev/null; then
            if confirm "Remove system user '$user'?"; then
                sudo userdel -r "$user" 2>/dev/null || sudo userdel "$user" 2>/dev/null || true
                ok "Removed user: $user"
            fi
        fi
    done

    echo ""
    echo -e "${GREEN}  Pi 5 deployment removed.${NC}"
    echo ""
    echo "  Note: System packages (python3, nginx, ffmpeg, etc.) were NOT removed."
    echo "  To remove them manually: sudo apt remove nginx ffmpeg"
    echo ""
}

# ---- Interactive Menu ------------------------------------------------------

interactive_menu() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Uninstall${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo "  What would you like to remove?"
    echo ""
    echo "  1) ${GREEN}dev${NC}   — Remove local development environment"
    echo "            Removes venv, .env, data/, and cache files."
    echo ""
    echo "  2) ${YELLOW}pi${NC}    — Remove Pi 5 production deployment"
    echo "            Stops services, removes /opt/claw, nginx config,"
    echo "            systemd units, MediaMTX, and system users."
    echo ""
    echo "  3) ${RED}all${NC}   — Remove everything (dev + pi)"
    echo ""
    echo -n "  Choose [1/2/3]: "
    read -r choice

    case "$choice" in
        1|dev)  uninstall_dev ;;
        2|pi)   uninstall_pi ;;
        3|all)  uninstall_dev; uninstall_pi ;;
        *)
            echo "  Invalid choice. Usage: ./uninstall.sh [dev|pi|all]"
            exit 1
            ;;
    esac
}

# ---- Main ------------------------------------------------------------------

case "${1:-}" in
    dev)        uninstall_dev ;;
    pi)         uninstall_pi ;;
    all)        uninstall_dev; uninstall_pi ;;
    --help|-h)
        echo "Usage: ./uninstall.sh [dev|pi|all]"
        echo ""
        echo "  dev   Remove local development environment"
        echo "  pi    Remove Pi 5 production deployment"
        echo "  all   Remove everything (dev + pi)"
        echo ""
        echo "Run without arguments for interactive mode."
        ;;
    "")         interactive_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: ./uninstall.sh [dev|pi|all]"
        exit 1
        ;;
esac
