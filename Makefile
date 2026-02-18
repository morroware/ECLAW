# ECLAW — Common development and operations commands
# Run 'make help' to see available targets

.PHONY: help install run dev demo demo-pi test simulate lint clean status logs deploy-check audit-internet diagnose-stream

VENV     := venv/bin
PYTHON   := $(VENV)/python
PIP      := $(VENV)/pip
UVICORN  := $(VENV)/uvicorn
PYTEST   := $(VENV)/pytest
PORT     ?= 8000

help: ## Show this help message
	@echo "ECLAW — Remote Claw Machine"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ---- Setup -----------------------------------------------------------------

install: ## Set up development environment (venv + deps + .env)
	@./install.sh dev

install-prod: ## Set up Pi 5 production environment
	@./install.sh pi

# ---- Development -----------------------------------------------------------

run: ## Start dev server with mock GPIO (auto-reload)
	MOCK_GPIO=true $(UVICORN) app.main:app --reload --host 0.0.0.0 --port $(PORT)

run-prod: ## Start production server (no reload, localhost only)
	$(UVICORN) app.main:app --host 127.0.0.1 --port $(PORT) --workers 1

dev: ## Start dev server and open browser
	@echo "Starting ECLAW dev server at http://localhost:$(PORT)"
	@echo "Press Ctrl+C to stop"
	@command -v xdg-open >/dev/null 2>&1 && (sleep 2 && xdg-open http://localhost:$(PORT)) & true
	@command -v open >/dev/null 2>&1 && (sleep 2 && open http://localhost:$(PORT)) & true
	MOCK_GPIO=true $(UVICORN) app.main:app --reload --host 0.0.0.0 --port $(PORT)

demo: ## Start PoC demo mode (short timers, mock GPIO)
	@echo "Starting ECLAW in PoC DEMO mode at http://localhost:$(PORT)"
	@echo "  Short timers for fast demo cycles"
	@echo "  Press Ctrl+C to stop"
	ECLAW_ENV_FILE=.env.demo MOCK_GPIO=true $(UVICORN) app.main:app --reload --host 0.0.0.0 --port $(PORT)

demo-pi: ## Start PoC demo on Pi 5 (short timers, real GPIO)
	@echo "Starting ECLAW PoC DEMO on Pi 5 at http://0.0.0.0:$(PORT)"
	@echo "  Short timers, REAL GPIO"
	@echo "  Press Ctrl+C to stop"
	ECLAW_ENV_FILE=.env.demo MOCK_GPIO=false GPIOZERO_PIN_FACTORY=lgpio $(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT)

# ---- Testing ---------------------------------------------------------------

test: ## Run the test suite
	MOCK_GPIO=true $(PYTEST) tests/ -v

test-quick: ## Run tests without verbose output
	MOCK_GPIO=true $(PYTEST) tests/ -q

simulate: ## Run simulated player against local server
	MOCK_GPIO=true $(PYTHON) scripts/simulate_player.py --base-url http://localhost:$(PORT) --count 3

simulate-parallel: ## Run 5 simulated players in parallel
	MOCK_GPIO=true $(PYTHON) scripts/simulate_player.py --base-url http://localhost:$(PORT) --count 5 --parallel

gpio-test: ## Run GPIO hardware test (Pi 5 only)
	$(PYTHON) scripts/gpio_test.py

# ---- Operations ------------------------------------------------------------

status: ## Check health of running server
	@bash scripts/health_check.sh http://localhost:$(PORT) 2>/dev/null || \
		bash scripts/health_check.sh http://localhost:$(PORT)

logs: ## Tail game server logs (systemd)
	sudo journalctl -u claw-server -f --no-pager

logs-watchdog: ## Tail watchdog logs (systemd)
	sudo journalctl -u claw-watchdog -f --no-pager

logs-all: ## Tail all ECLAW service logs
	sudo journalctl -u claw-server -u claw-watchdog -u mediamtx -f --no-pager

deploy-check: ## Verify production deployment health
	@bash scripts/health_check.sh http://localhost:$(PORT)

diagnose-stream: ## Diagnose MediaMTX streaming issues
	@bash scripts/diagnose_mediamtx.sh

restart: ## Restart all ECLAW services (systemd)
	sudo systemctl restart claw-server claw-watchdog mediamtx
	@echo "Services restarted. Run 'make status' to verify."

stop: ## Stop all ECLAW services
	sudo systemctl stop claw-server claw-watchdog
	@echo "Game server and watchdog stopped."

# ---- Cleanup ---------------------------------------------------------------

clean: ## Remove build artifacts and database
	rm -rf __pycache__ **/__pycache__ .pytest_cache
	rm -f data/claw.db data/claw.db-wal data/claw.db-shm
	@echo "Cleaned build artifacts and database."

clean-all: clean ## Remove venv and all generated files
	rm -rf venv
	@echo "Cleaned everything including virtual environment."

db-reset: ## Reset the database (deletes all data, restarts if running via systemd)
	rm -f data/claw.db data/claw.db-wal data/claw.db-shm
	@if systemctl is-active --quiet claw-server 2>/dev/null; then \
		echo "Restarting claw-server to apply database reset..."; \
		sudo systemctl restart claw-server; \
		echo "Database reset and server restarted."; \
	else \
		echo "Database deleted. Restart the server for the reset to take effect."; \
		echo "  Dev mode:  Ctrl+C then 'make run'"; \
		echo "  systemd:   sudo systemctl restart claw-server"; \
	fi

.PHONY: audit-internet
audit-internet: ## Run offline internet-readiness checks
	./scripts/internet_readiness_audit.sh
