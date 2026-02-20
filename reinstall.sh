#!/bin/bash
# ============================================================
# Remote Claw — Reinstall Script (fresh clone from GitHub)
# ============================================================
#
# Uninstalls the current Remote Claw installation, pulls a fresh copy
# from GitHub, and runs the installer.
#
# Usage:
#   ./reinstall.sh          Interactive mode
#   ./reinstall.sh dev      Reinstall development environment
#   ./reinstall.sh pi       Reinstall Pi 5 production deployment
#   ./reinstall.sh demo     Reinstall Pi 5 PoC demo setup
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

REPO_URL="https://github.com/morroware/remote-claw.git"

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

# ---- Determine install directory -------------------------------------------

# If run from inside the Remote Claw repo, use the parent directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/install.sh" ] && [ -d "$SCRIPT_DIR/app" ]; then
    INSTALL_PARENT="$(dirname "$SCRIPT_DIR")"
    INSTALL_DIR="$SCRIPT_DIR"
    RUNNING_FROM_REPO=true
else
    INSTALL_PARENT="$(pwd)"
    INSTALL_DIR="$INSTALL_PARENT/remote-claw"
    RUNNING_FROM_REPO=false
fi

# ---- Helpers ---------------------------------------------------------------

check_deps() {
    if ! command -v git &>/dev/null; then
        fail "git is required but not found. Install it first."
    fi
}

backup_env() {
    # Save .env if it exists so user config isn't lost
    ENV_BACKUP=""
    if [ -f "$INSTALL_DIR/.env" ]; then
        ENV_BACKUP="$(mktemp /tmp/remote-claw-env-backup.XXXXXX)"
        cp "$INSTALL_DIR/.env" "$ENV_BACKUP"
        ok "Backed up .env to $ENV_BACKUP"
    fi
}

restore_env() {
    if [ -n "${ENV_BACKUP:-}" ] && [ -f "${ENV_BACKUP:-}" ]; then
        cp "$ENV_BACKUP" "$INSTALL_DIR/.env"
        rm -f "$ENV_BACKUP"
        ok "Restored .env from backup"
    fi
}

clean_dev() {
    info "Cleaning local development files..."
    rm -rf "$INSTALL_DIR/venv"
    rm -rf "$INSTALL_DIR/data"
    rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/.pytest_cache"
    find "$INSTALL_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    ok "Development files cleaned"
}

clean_pi() {
    info "Stopping Remote Claw services..."
    for svc in claw-watchdog claw-server mediamtx; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop "$svc"
            ok "Stopped $svc"
        fi
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl disable "$svc" 2>/dev/null
        fi
    done

    # Clear any failure state / start-limit counters so the fresh install
    # can start services immediately (critical after crash-loops).
    sudo systemctl reset-failed claw-watchdog claw-server mediamtx 2>/dev/null || true

    # Remove service files
    for unit in claw-server.service claw-watchdog.service mediamtx.service; do
        sudo rm -f "/etc/systemd/system/$unit"
    done
    sudo systemctl daemon-reload

    # Remove nginx config
    sudo rm -f /etc/nginx/sites-enabled/claw
    sudo rm -f /etc/nginx/sites-available/claw
    if command -v nginx &>/dev/null && sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx 2>/dev/null || true
    fi

    # Remove application directory
    if [ -d /opt/claw ]; then
        sudo rm -rf /opt/claw
        ok "Removed /opt/claw"
    fi

    # Remove MediaMTX config
    sudo rm -f /etc/mediamtx.yml

    ok "Pi 5 deployment cleaned"
}

fresh_clone() {
    info "Cloning fresh copy from GitHub..."

    if $RUNNING_FROM_REPO; then
        # We're inside the repo — need to cd out, remove, and re-clone
        cd "$INSTALL_PARENT"
        local dir_name
        dir_name="$(basename "$INSTALL_DIR")"

        # Safety check: don't delete if there are uncommitted changes
        if [ -d "$INSTALL_DIR/.git" ]; then
            cd "$INSTALL_DIR"
            if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
                warn "You have uncommitted changes in the repository."
                if ! confirm "Discard all local changes and re-clone?"; then
                    echo "  Aborted."
                    exit 0
                fi
            fi
            cd "$INSTALL_PARENT"
        fi

        rm -rf "$INSTALL_DIR"
        git clone "$REPO_URL" "$dir_name"
        INSTALL_DIR="$INSTALL_PARENT/$dir_name"
    else
        if [ -d "$INSTALL_DIR" ]; then
            warn "Directory $INSTALL_DIR already exists."
            if ! confirm "Remove it and clone fresh?"; then
                echo "  Aborted."
                exit 0
            fi
            rm -rf "$INSTALL_DIR"
        fi
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi

    ok "Fresh clone complete at $INSTALL_DIR"
}

# ---- Reinstall Modes -------------------------------------------------------

reinstall_dev() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Fresh Reinstall (Dev)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    echo -e "  This will:"
    echo "    1. Back up your .env file"
    echo "    2. Remove the current installation"
    echo "    3. Clone a fresh copy from GitHub"
    echo "    4. Restore your .env file"
    echo "    5. Run the dev installer"
    echo ""
    if ! confirm "Continue with fresh reinstall?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    check_deps
    backup_env
    clean_dev
    fresh_clone
    restore_env

    info "Running installer..."
    cd "$INSTALL_DIR"
    chmod +x install.sh
    exec ./install.sh dev
}

reinstall_pi() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Fresh Reinstall (Pi 5)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    echo -e "  This will:"
    echo "    1. Stop all Remote Claw services"
    echo "    2. Remove the current deployment"
    echo "    3. Clone a fresh copy from GitHub"
    echo "    4. Run the Pi 5 production installer"
    echo ""
    echo -e "  ${RED}All data (database, logs) will be lost.${NC}"
    echo ""
    if ! confirm "Continue with fresh reinstall?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    check_deps
    clean_pi
    clean_dev
    fresh_clone

    info "Running installer..."
    cd "$INSTALL_DIR"
    chmod +x install.sh
    exec ./install.sh pi
}

reinstall_demo() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Fresh Reinstall (Demo)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    echo -e "  This will:"
    echo "    1. Stop all Remote Claw services"
    echo "    2. Remove the current deployment"
    echo "    3. Clone a fresh copy from GitHub"
    echo "    4. Run the Pi 5 demo installer"
    echo ""
    echo -e "  ${RED}All data (database, logs) will be lost.${NC}"
    echo ""
    if ! confirm "Continue with fresh reinstall?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    check_deps
    clean_pi
    clean_dev
    fresh_clone

    info "Running installer..."
    cd "$INSTALL_DIR"
    chmod +x install.sh
    exec ./install.sh demo
}

# ---- Interactive Menu ------------------------------------------------------

interactive_menu() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  Remote Claw — Fresh Reinstall from GitHub${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo "  This will remove your current Remote Claw installation,"
    echo "  clone a fresh copy from GitHub, and reinstall."
    echo ""
    echo "  What type of installation?"
    echo ""
    echo "  1) ${GREEN}dev${NC}   — Development environment"
    echo "            Reinstall venv + deps on this machine."
    echo "            Your .env will be backed up and restored."
    echo ""
    echo "  2) ${YELLOW}pi${NC}    — Pi 5 production deployment"
    echo "            Full clean reinstall of all services."
    echo ""
    echo "  3) ${YELLOW}demo${NC}  — Pi 5 PoC demo setup"
    echo "            Same as pi, with short demo timers."
    echo ""
    echo -n "  Choose [1/2/3]: "
    read -r choice

    case "$choice" in
        1|dev)  reinstall_dev ;;
        2|pi)   reinstall_pi ;;
        3|demo) reinstall_demo ;;
        *)
            echo "  Invalid choice. Usage: ./reinstall.sh [dev|pi|demo]"
            exit 1
            ;;
    esac
}

# ---- Main ------------------------------------------------------------------

case "${1:-}" in
    dev)        reinstall_dev ;;
    pi)         reinstall_pi ;;
    demo)       reinstall_demo ;;
    --help|-h)
        echo "Usage: ./reinstall.sh [dev|pi|demo]"
        echo ""
        echo "  dev   Reinstall development environment"
        echo "  pi    Reinstall Pi 5 production deployment"
        echo "  demo  Reinstall Pi 5 PoC demo setup"
        echo ""
        echo "Clones a fresh copy from: $REPO_URL"
        echo "Run without arguments for interactive mode."
        ;;
    "")         interactive_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: ./reinstall.sh [dev|pi|demo]"
        exit 1
        ;;
esac
