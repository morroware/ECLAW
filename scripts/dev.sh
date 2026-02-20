#!/bin/bash
# Quick-start script for local development.
# Activates venv, sets mock GPIO, and starts the dev server.
#
# Usage:
#   ./scripts/dev.sh              # Start on default port 8000
#   ./scripts/dev.sh 3000         # Start on custom port
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

PORT="${1:-8000}"

# Check venv exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Run './install.sh dev' first."
    exit 1
fi

# Ensure .env exists
if [ ! -f ".env" ]; then
    echo "No .env file found. Creating from .env.example..."
    cp .env.example .env
fi

echo "========================================"
echo "  Remote Claw Dev Server"
echo "========================================"
echo ""
echo "  URL:       http://localhost:$PORT"
echo "  Mock GPIO: enabled"
echo "  Auto-reload: enabled"
echo ""
echo "  Press Ctrl+C to stop"
echo "========================================"
echo ""

export MOCK_GPIO=true
exec ./venv/bin/uvicorn app.main:app \
    --reload \
    --host 0.0.0.0 \
    --port "$PORT"
