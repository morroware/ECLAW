#!/bin/bash
# ============================================================
# ECLAW — One-command setup for development or Pi 5 deployment
# ============================================================
#
# Usage:
#   ./install.sh          Interactive mode (asks what you want)
#   ./install.sh dev      Set up local development environment
#   ./install.sh pi       Full Pi 5 production deployment
#   ./install.sh test     Install deps and run test suite
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No color

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Helpers ---------------------------------------------------------------

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON=python3
    elif command -v python &>/dev/null; then
        PYTHON=python
    else
        fail "Python 3 is required but not found. Install it first."
    fi

    PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
        fail "Python 3.11+ required (found $PY_VERSION)"
    fi
    ok "Python $PY_VERSION found at $(command -v $PYTHON)"
}

create_venv() {
    if [ ! -d "venv" ]; then
        info "Creating virtual environment..."
        $PYTHON -m venv venv
        ok "Virtual environment created at ./venv"
    else
        ok "Virtual environment already exists"
    fi
}

install_deps() {
    local req_file="${1:-requirements.txt}"
    info "Installing dependencies from $req_file..."
    ./venv/bin/pip install --upgrade pip -q
    ./venv/bin/pip install -r "$req_file" -q
    ok "Dependencies installed"
}

setup_env() {
    if [ ! -f ".env" ]; then
        info "Creating .env from .env.example..."
        cp .env.example .env
        ok ".env created — edit it to customize settings"
    else
        ok ".env already exists"
    fi
}

setup_data_dir() {
    mkdir -p data
    ok "Data directory ready"
}

run_tests() {
    info "Running test suite..."
    if ./venv/bin/python -m pytest tests/ -v; then
        ok "All tests passed"
    else
        warn "Some tests failed — see output above"
        return 1
    fi
}

verify_import() {
    info "Verifying app imports..."
    if MOCK_GPIO=true ./venv/bin/python -c "from app.config import settings; print(f'  Config OK (mock_gpio={settings.mock_gpio})')"; then
        ok "Application imports cleanly"
    else
        fail "Import check failed — see error above"
    fi
}

# ---- Dev Setup -------------------------------------------------------------

setup_dev() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Development Setup${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    check_python
    create_venv
    install_deps "requirements-dev.txt"
    setup_env
    setup_data_dir
    verify_import
    echo ""
    run_tests || true
    echo ""

    echo -e "${BOLD}========================================${NC}"
    echo -e "${GREEN}  Development environment ready!${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo "  Quick start:"
    echo "    source venv/bin/activate"
    echo "    make run             # Start dev server"
    echo "    make test            # Run test suite"
    echo "    make simulate        # Run simulated player"
    echo ""
    echo "  Or without make:"
    echo "    MOCK_GPIO=true uvicorn app.main:app --reload"
    echo ""
    echo "  Then open: http://localhost:8000"
    echo ""
}

# ---- Pi 5 Setup ------------------------------------------------------------

setup_pi() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Pi 5 Production Setup${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""

    # Check we're on a Pi
    if [ ! -f /proc/device-tree/model ]; then
        warn "This doesn't appear to be a Raspberry Pi"
        echo -n "  Continue anyway? [y/N] "
        read -r answer
        if [[ ! "$answer" =~ ^[Yy] ]]; then
            echo "Aborted."
            exit 0
        fi
    else
        MODEL=$(tr -d '\0' < /proc/device-tree/model)
        info "Detected: $MODEL"
    fi

    # Delegate to the full Pi setup script
    info "Running full Pi 5 deployment script..."
    exec bash "$SCRIPT_DIR/scripts/setup_pi.sh"
}

# ---- Test Only -------------------------------------------------------------

setup_test() {
    echo ""
    echo -e "${BOLD}  ECLAW — Install & Test${NC}"
    echo ""

    check_python
    create_venv
    install_deps "requirements-dev.txt"
    setup_env
    setup_data_dir
    verify_import
    echo ""
    run_tests
}

# ---- Interactive Menu ------------------------------------------------------

interactive_menu() {
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  ECLAW — Remote Claw Machine Setup${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo "  What would you like to do?"
    echo ""
    echo "  1) ${GREEN}dev${NC}   — Set up local development environment"
    echo "            Creates venv, installs deps, runs tests."
    echo "            Works on any machine (uses mock GPIO)."
    echo ""
    echo "  2) ${YELLOW}pi${NC}    — Full Raspberry Pi 5 deployment"
    echo "            Installs system packages, MediaMTX, nginx,"
    echo "            systemd services. Requires Pi 5 + sudo."
    echo ""
    echo "  3) ${BLUE}test${NC}  — Install dependencies and run tests"
    echo "            Quick validation that everything works."
    echo ""
    echo -n "  Choose [1/2/3]: "
    read -r choice

    case "$choice" in
        1|dev)  setup_dev ;;
        2|pi)   setup_pi ;;
        3|test) setup_test ;;
        *)
            echo "  Invalid choice. Usage: ./install.sh [dev|pi|test]"
            exit 1
            ;;
    esac
}

# ---- Main ------------------------------------------------------------------

case "${1:-}" in
    dev)        setup_dev ;;
    pi)         setup_pi ;;
    test)       setup_test ;;
    --help|-h)
        echo "Usage: ./install.sh [dev|pi|test]"
        echo ""
        echo "  dev   Set up local development environment"
        echo "  pi    Full Pi 5 production deployment"
        echo "  test  Install deps and run test suite"
        echo ""
        echo "Run without arguments for interactive mode."
        ;;
    "")         interactive_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: ./install.sh [dev|pi|test]"
        exit 1
        ;;
esac
