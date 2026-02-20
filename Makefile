# ── thehallmonitor Makefile ───────────────────────────────────────────────────
#
# Targets overview:
#   init          First-time local setup (venv + deps + .env)
#   run           Run bot locally (foreground)
#   update-db     Fetch latest forbidden list and repopulate DB
#   reset-db      Wipe and repopulate DB from scratch
#   test          Run unit tests
#   clean         Remove venv and Python cache
#   reset         Remove venv, DB, and .env (full wipe)
#
#   docker-build  Build Docker image
#   docker-up     Start container in background
#   docker-down   Stop container
#   docker-logs   Follow container logs
#   docker-update-db   Run updater inside the running container
#   docker-reset-db    Force-repopulate DB inside the running container
#   docker-shell  Open a shell inside the running container
#   docker-clean  Remove container, image, and local DB
#
#   install       Install as systemd service (Ubuntu/Debian only)
#   uninstall     Remove systemd service

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Paths ─────────────────────────────────────────────────────────────────────

VENV        := venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
DATA_DIR    := data
DB_FILES    := $(DATA_DIR)/*.db $(DATA_DIR)/*.db-shm $(DATA_DIR)/*.db-wal

# Docker
COMPOSE     := docker compose
SERVICE     := thehallmonitor
IMAGE       := thehallmonitor

# systemd (Linux install)
INSTALL_DIR := /opt/thehallmonitor
SERVICE_FILE := /etc/systemd/system/thehallmonitor.service
CURRENT_USER := $(shell whoami)
CURRENT_DIR  := $(shell pwd)

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "thehallmonitor — available targets"
	@echo ""
	@echo "  Local development:"
	@echo "    make init           First-time setup: venv, deps, .env"
	@echo "    make run            Run the bot locally (foreground, Ctrl+C to stop)"
	@echo "    make update-db      Download latest list and refresh DB"
	@echo "    make reset-db       Wipe DB and repopulate from scratch"
	@echo "    make test           Run unit tests"
	@echo "    make clean          Remove venv and Python cache"
	@echo "    make reset          Full wipe: venv + DB + .env"
	@echo ""
	@echo "  Docker (recommended for production):"
	@echo "    make docker-build   Build image"
	@echo "    make docker-up      Start container in background"
	@echo "    make docker-down    Stop container"
	@echo "    make docker-logs    Follow container logs"
	@echo "    make docker-update-db  Refresh DB inside running container"
	@echo "    make docker-reset-db   Force-repopulate DB inside running container"
	@echo "    make docker-shell   Open shell inside running container"
	@echo "    make docker-clean   Remove container + image + local DB"
	@echo "    make docker-upgrade Show new base image digest (then update Dockerfile)"
	@echo ""
	@echo "  Linux system service (Ubuntu/Debian):"
	@echo "    make install        Install as systemd service"
	@echo "    make uninstall      Remove systemd service"
	@echo ""

# ── Local: first-time setup ───────────────────────────────────────────────────

.PHONY: init
init: $(VENV)/bin/activate $(DATA_DIR)/.gitkeep .env
	@echo ""
	@echo "✓ Ready. Edit .env, then run: make update-db && make run"

$(VENV)/bin/activate: requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)/bin/activate

$(DATA_DIR)/.gitkeep:
	mkdir -p $(DATA_DIR)
	touch $(DATA_DIR)/.gitkeep

.env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "⚠  Created .env from .env.example — fill in BOT_TOKEN, INDEX_PAGE, BASE_URL, DOC_LINK_RE"; \
	fi

# ── Local: run ────────────────────────────────────────────────────────────────

.PHONY: run
run: $(VENV)/bin/activate .env
	$(PYTHON) bot.py

# ── Local: database ───────────────────────────────────────────────────────────

.PHONY: update-db
update-db: $(VENV)/bin/activate .env
	$(PYTHON) updater.py

.PHONY: reset-db
reset-db: $(VENV)/bin/activate .env
	@echo "Wiping existing DB..."
	rm -f $(DB_FILES)
	$(PYTHON) updater.py --force
	@echo "✓ DB repopulated."

# ── Local: test ───────────────────────────────────────────────────────────────

.PHONY: test
test: $(VENV)/bin/activate
	$(PYTEST) tests/ -v

# ── Local: cleanup ────────────────────────────────────────────────────────────

.PHONY: clean
clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ venv and cache removed."

.PHONY: reset
reset: clean
	rm -f $(DB_FILES) .env
	@echo "✓ Full reset done: venv, DB, and .env removed."
	@echo "  Run 'make init' to start fresh."

# ── Docker ────────────────────────────────────────────────────────────────────

.PHONY: docker-build
docker-build:
	$(COMPOSE) build

.PHONY: docker-up
docker-up: .env $(DATA_DIR)/.gitkeep
	$(COMPOSE) up -d
	@echo "✓ Container started. Logs: make docker-logs"

.PHONY: docker-down
docker-down:
	$(COMPOSE) down

.PHONY: docker-logs
docker-logs:
	$(COMPOSE) logs -f

.PHONY: docker-update-db
docker-update-db:
	$(COMPOSE) exec $(SERVICE) python updater.py
	@echo "✓ DB updated inside container."

.PHONY: docker-reset-db
docker-reset-db:
	@echo "Wiping DB inside container..."
	$(COMPOSE) exec $(SERVICE) sh -c 'rm -f /app/data/*.db /app/data/*.db-shm /app/data/*.db-wal'
	$(COMPOSE) exec $(SERVICE) python updater.py --force
	@echo "✓ DB repopulated inside container."

.PHONY: docker-shell
docker-shell:
	$(COMPOSE) exec $(SERVICE) bash

.PHONY: docker-upgrade
docker-upgrade:
	@echo "Pulling latest python:3.11-slim digest..."
	docker pull python:3.11-slim
	@echo ""
	@echo "New digest:"
	@docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim
	@echo ""
	@echo "Update the FROM line in Dockerfile with the digest above, then run: make docker-build"

.PHONY: docker-clean
docker-clean: docker-down
	$(COMPOSE) down --rmi local --volumes
	rm -f $(DB_FILES)
	@echo "✓ Container, image, and local DB removed."

# ── Linux system service (Ubuntu/Debian) ──────────────────────────────────────

.PHONY: install
install:
	@if [ "$$(uname)" != "Linux" ]; then \
		echo "❌ 'make install' is for Linux (systemd) only."; \
		echo "   On macOS, use Docker: make docker-up"; \
		exit 1; \
	fi
	@if [ ! -f .env ]; then \
		echo "❌ .env not found. Run 'make init' first and fill in .env."; \
		exit 1; \
	fi
	@echo "Installing to $(INSTALL_DIR)..."
	sudo mkdir -p $(INSTALL_DIR)/data
	sudo cp -r bot.py updater.py parser.py matcher.py database.py \
	           requirements.txt .env $(INSTALL_DIR)/
	sudo python3 -m venv $(INSTALL_DIR)/venv
	sudo $(INSTALL_DIR)/venv/bin/pip install --upgrade pip
	sudo $(INSTALL_DIR)/venv/bin/pip install -r $(INSTALL_DIR)/requirements.txt
	sudo chown -R $(CURRENT_USER):$(CURRENT_USER) $(INSTALL_DIR)
	@echo "Writing systemd unit..."
	@printf '[Unit]\nDescription=thehallmonitor Telegram bot\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=$(CURRENT_USER)\nWorkingDirectory=$(INSTALL_DIR)\nEnvironmentFile=$(INSTALL_DIR)/.env\nExecStart=$(INSTALL_DIR)/venv/bin/python bot.py\nRestart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=multi-user.target\n' \
		| sudo tee $(SERVICE_FILE) > /dev/null
	@echo "Writing daily updater service..."
	@printf '[Unit]\nDescription=thehallmonitor daily DB update\nAfter=network-online.target\n\n[Service]\nType=oneshot\nUser=$(CURRENT_USER)\nWorkingDirectory=$(INSTALL_DIR)\nEnvironmentFile=$(INSTALL_DIR)/.env\nExecStart=$(INSTALL_DIR)/venv/bin/python updater.py\n' \
		| sudo tee /etc/systemd/system/thehallmonitor-updater.service > /dev/null
	@echo "Writing daily updater timer..."
	@printf '[Unit]\nDescription=thehallmonitor daily DB update timer\n\n[Timer]\nOnCalendar=*-*-* 04:05:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n' \
		| sudo tee /etc/systemd/system/thehallmonitor-updater.timer > /dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now thehallmonitor.service
	sudo systemctl enable --now thehallmonitor-updater.timer
	@echo ""
	@echo "✓ Service installed and started."
	@echo "  Status:  sudo systemctl status thehallmonitor"
	@echo "  Logs:    sudo journalctl -u thehallmonitor -f"

.PHONY: uninstall
uninstall:
	@if [ "$$(uname)" != "Linux" ]; then \
		echo "❌ 'make uninstall' is for Linux only."; exit 1; \
	fi
	-sudo systemctl disable --now thehallmonitor.service
	-sudo systemctl disable --now thehallmonitor-updater.timer
	-sudo systemctl disable --now thehallmonitor-updater.service
	-sudo rm -f $(SERVICE_FILE) \
	            /etc/systemd/system/thehallmonitor-updater.service \
	            /etc/systemd/system/thehallmonitor-updater.timer
	sudo systemctl daemon-reload
	@echo "Service files removed. Install dir $(INSTALL_DIR) kept — remove manually if desired:"
	@echo "  sudo rm -rf $(INSTALL_DIR)"
