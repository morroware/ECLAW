#!/bin/bash
# ============================================================
# ECLAW — Reinstall / Update Script
# ============================================================
#
# Full reinstall (fresh clone from GitHub) or lightweight update
# (git pull + restart).
#
# Usage:
#   ./reinstall.sh              Interactive mode
#   ./reinstall.sh dev          Reinstall development environment
#   ./reinstall.sh pi           Reinstall Pi 5 production deployment
#   ./reinstall.sh demo         Reinstall Pi 5 PoC demo setup
#   ./reinstall.sh update       Quick update (git pull + restart)
#   ./reinstall.sh update dev   Quick update for dev environment
#   ./reinstall.sh update pi    Quick update for Pi 5 deployment
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

REPO_URL="https://github.com/morroware/ECLAW.git"

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

# If run from inside the ECLAW repo, use the parent directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/install.sh" ] && [ -d "$SCRIPT_DIR/app" ]; then
    INSTALL_PARENT="$(dirname "$SCRIPT_DIR")"
    INSTALL_DIR="$SCRIPT_DIR"
    RUNNING_FROM_REPO=true
else
    INSTALL_PARENT="$(pwd)"
    INSTALL_DIR="$INSTALL_PARENT/ECLAW"
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
        ENV_BACKUP="$(mktemp /tmp/eclaw-env-backup.XXXXXX)"
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

backup_pi_env() {
    # Save the deployed .env from /opt/claw so Pi config isn't lost
    PI_ENV_BACKUP=""
    if [ -f /opt/claw/.env ]; then
        PI_ENV_BACKUP="$(mktemp /tmp/eclaw-pi-env-backup.XXXXXX)"
        sudo cp /opt/claw/.env "$PI_ENV_BACKUP"
        ok "Backed up /opt/claw/.env to $PI_ENV_BACKUP"
    fi
}

restore_pi_env() {
    # Restore the deployed .env to /opt/claw so setup_pi.sh preserves it
    if [ -n "${PI_ENV_BACKUP:-}" ] && [ -f "${PI_ENV_BACKUP:-}" ]; then
        sudo mkdir -p /opt/claw
        sudo cp "$PI_ENV_BACKUP" /opt/claw/.env
        sudo chown claw:claw /opt/claw/.env 2>/dev/null || true
        rm -f "$PI_ENV_BACKUP"
        ok "Restored /opt/claw/.env from backup"
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
    info "Stopping ECLAW services..."
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
    echo -e "${BOLD}  ECLAW — Fresh Reinstall (Dev)${NC}"
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
    echo -e "${BOLD}  ECLAW — Fresh Reinstall (Pi 5)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    echo -e "  This will:"
    echo "    1. Back up your /opt/claw/.env configuration"
    echo "    2. Stop all ECLAW services"
    echo "    3. Remove the current deployment"
    echo "    4. Clone a fresh copy from GitHub"
    echo "    5. Restore your .env and run the Pi 5 installer"
    echo ""
    echo -e "  ${RED}Database and logs will be reset.${NC}"
    echo -e "  ${GREEN}Your .env configuration will be preserved.${NC}"
    echo ""
    if ! confirm "Continue with fresh reinstall?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    check_deps
    backup_pi_env
    clean_pi
    clean_dev
    fresh_clone
    restore_pi_env

    info "Running installer..."
    cd "$INSTALL_DIR"
    chmod +x install.sh
    exec ./install.sh pi
}

reinstall_demo() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Fresh Reinstall (Demo)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    echo -e "  This will:"
    echo "    1. Back up your /opt/claw/.env configuration"
    echo "    2. Stop all ECLAW services"
    echo "    3. Remove the current deployment"
    echo "    4. Clone a fresh copy from GitHub"
    echo "    5. Restore your .env and run the Pi 5 demo installer"
    echo ""
    echo -e "  ${RED}Database and logs will be reset.${NC}"
    echo -e "  ${GREEN}Your .env configuration will be preserved.${NC}"
    echo ""
    if ! confirm "Continue with fresh reinstall?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    check_deps
    backup_pi_env
    clean_pi
    clean_dev
    fresh_clone
    restore_pi_env

    info "Running installer..."
    cd "$INSTALL_DIR"
    chmod +x install.sh
    exec ./install.sh demo
}

# ---- Quick Update Mode -----------------------------------------------------
#
# Lightweight update: git pull + rebuild deps + restart.
# No fresh clone, preserves .env and database.

update_dev() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Quick Update (Dev)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if ! $RUNNING_FROM_REPO; then
        fail "Must run from inside the ECLAW repository."
    fi

    cd "$INSTALL_DIR"

    info "Pulling latest code..."
    git pull || fail "git pull failed. Resolve conflicts and try again."
    ok "Code updated"

    if [ -d venv ]; then
        info "Updating dependencies..."
        ./venv/bin/pip install --upgrade pip -q
        if [ -f requirements-dev.txt ]; then
            ./venv/bin/pip install -r requirements-dev.txt -q
        else
            ./venv/bin/pip install -r requirements.txt -q
        fi
        ok "Dependencies updated"
    else
        warn "No venv found — running full dev install."
        exec ./install.sh dev
    fi

    echo ""
    echo -e "${GREEN}  Update complete!${NC}"
    echo ""
    echo "  Restart the server to apply changes:"
    echo "    make run"
    echo "    # or: MOCK_GPIO=true uvicorn app.main:app --reload"
    echo ""
}

update_pi() {
    local svc_mode="${1:-pi}"

    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Quick Update (Pi 5)${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    if [ "$(id -u)" -eq 0 ]; then
        fail "Do not run as root. Run as a normal user with sudo access."
    fi

    if ! $RUNNING_FROM_REPO; then
        fail "Must run from inside the ECLAW repository."
    fi

    cd "$INSTALL_DIR"

    echo -e "  This will:"
    echo "    1. Pull latest code from GitHub"
    echo "    2. Stop claw services"
    echo "    3. Update files in /opt/claw (preserving .env and data)"
    echo "    4. Update Python dependencies"
    echo "    5. Restart services"
    echo ""
    echo -e "  ${GREEN}Your .env and database will be preserved.${NC}"
    echo ""
    if ! confirm "Continue with quick update?"; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    info "Pulling latest code..."
    git pull || fail "git pull failed. Resolve conflicts and try again."
    ok "Code updated"

    info "Stopping services..."
    for svc in claw-watchdog claw-server; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop "$svc"
            ok "Stopped $svc"
        fi
    done

    if [ ! -d /opt/claw ]; then
        warn "/opt/claw not found — falling back to full reinstall."
        exec ./install.sh "$svc_mode"
    fi

    info "Updating application files..."
    for dir in app migrations watchdog; do
        if [ -d "$INSTALL_DIR/$dir" ]; then
            sudo rm -rf "/opt/claw/$dir"
            sudo cp -r "$INSTALL_DIR/$dir" /opt/claw/
        fi
    done
    if [ -d "$INSTALL_DIR/web" ]; then
        sudo rm -rf /opt/claw/web
        sudo cp -r "$INSTALL_DIR/web" /opt/claw/
    fi
    sudo cp "$INSTALL_DIR/requirements.txt" /opt/claw/
    sudo chown -R claw:claw /opt/claw
    ok "Application files updated in /opt/claw"

    info "Updating dependencies..."
    sudo -u claw /opt/claw/venv/bin/pip install --upgrade pip -q
    sudo -u claw /opt/claw/venv/bin/pip install -r /opt/claw/requirements.txt -q
    ok "Dependencies updated"

    info "Restarting services..."
    sudo systemctl start claw-server
    sleep 2
    sudo systemctl start claw-watchdog

    echo ""
    for svc in mediamtx claw-server claw-watchdog nginx; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            ok "$svc is running"
        else
            warn "$svc is NOT running"
        fi
    done

    PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<pi-ip>")
    echo ""
    echo -e "${GREEN}  Update complete!${NC}"
    echo ""
    echo "  Access the game:  http://$PI_IP"
    echo "  Admin panel:      http://$PI_IP/admin/panel"
    echo ""
}

# ---- Interactive Menu ------------------------------------------------------

interactive_menu() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Reinstall / Update${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo "  What would you like to do?"
    echo ""
    echo "  1) ${GREEN}update${NC} — Quick update (recommended)"
    echo "            git pull + update deps + restart services."
    echo "            Preserves your .env and database."
    echo ""
    echo "  2) ${YELLOW}dev${NC}    — Full reinstall (development)"
    echo "            Fresh clone, rebuild venv."
    echo "            .env is backed up and restored."
    echo ""
    echo "  3) ${YELLOW}pi${NC}     — Full reinstall (Pi 5 production)"
    echo "            Fresh clone, full clean reinstall."
    echo "            .env is backed up and restored."
    echo ""
    echo "  4) ${YELLOW}demo${NC}   — Full reinstall (Pi 5 demo)"
    echo "            Same as pi, with short demo timers."
    echo ""
    echo -n "  Choose [1/2/3/4]: "
    read -r choice

    case "$choice" in
        1|update) update_auto ;;
        2|dev)    reinstall_dev ;;
        3|pi)     reinstall_pi ;;
        4|demo)   reinstall_demo ;;
        *)
            echo "  Invalid choice. Usage: ./reinstall.sh [update|dev|pi|demo]"
            exit 1
            ;;
    esac
}

# ---- Auto-detect update mode -----------------------------------------------

update_auto() {
    # Detect whether this is a dev or Pi deployment and update accordingly.
    if [ -d /opt/claw ] && [ -f /etc/systemd/system/claw-server.service ]; then
        update_pi "pi"
    else
        update_dev
    fi
}

# ---- Main ------------------------------------------------------------------

case "${1:-}" in
    dev)        reinstall_dev ;;
    pi)         reinstall_pi ;;
    demo)       reinstall_demo ;;
    update)
        # Allow: ./reinstall.sh update [dev|pi|demo]
        case "${2:-}" in
            dev)   update_dev ;;
            pi)    update_pi "pi" ;;
            demo)  update_pi "demo" ;;
            "")    update_auto ;;
            *)     fail "Unknown update target: $2. Use: update [dev|pi|demo]" ;;
        esac
        ;;
    --help|-h)
        echo "Usage: ./reinstall.sh [update|dev|pi|demo]"
        echo ""
        echo "  Quick update (recommended for routine updates):"
        echo "    update       Auto-detect environment and update"
        echo "    update dev   Update development environment"
        echo "    update pi    Update Pi 5 production deployment"
        echo "    update demo  Update Pi 5 demo deployment"
        echo ""
        echo "  Full reinstall (destructive — fresh clone from GitHub):"
        echo "    dev          Reinstall development environment"
        echo "    pi           Reinstall Pi 5 production deployment"
        echo "    demo         Reinstall Pi 5 PoC demo setup"
        echo ""
        echo "Repo: $REPO_URL"
        echo "Run without arguments for interactive mode."
        ;;
    "")         interactive_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: ./reinstall.sh [update|dev|pi|demo]"
        exit 1
        ;;
esac
