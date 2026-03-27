# ── Hevy2Intervals Makefile ─────────────────────────────────
.DEFAULT_GOAL := help
SHELL := /bin/bash

# ── Config ───────────────────────────────────────────────────
APP_NAME     := hevy-sync
DEPLOY_HOST  ?= your-server-ip
DEPLOY_USER  ?= coach
DEPLOY_PATH  ?= /opt/hevy-sync

# ── Local ────────────────────────────────────────────────────

.PHONY: sync
sync: ## Sync recent workouts (local)
	python3 hevy_intervals_sync.py sync

.PHONY: backfill
backfill: ## Backfill all historical workouts (local)
	python3 hevy_intervals_sync.py backfill

.PHONY: status
status: ## Show sync statistics
	python3 hevy_intervals_sync.py status

# ── Docker ───────────────────────────────────────────────────

.PHONY: build
build: ## Build Docker image
	docker build -t $(APP_NAME):latest .

.PHONY: up
up: ## Start services (docker compose)
	docker compose up -d

.PHONY: down
down: ## Stop services
	docker compose down

.PHONY: logs
logs: ## Tail container logs
	docker compose logs -f --tail=100

# ── Deploy ───────────────────────────────────────────────────

.PHONY: deploy
deploy: ## Deploy to VPS. Override: DEPLOY_HOST=x.x.x.x
	@./scripts/deploy.sh

.PHONY: setup-vps
setup-vps: ## First-time VPS setup. Usage: make setup-vps DOMAIN=hevy.yourdomain.com
ifndef DOMAIN
	$(error DOMAIN is required. Usage: make setup-vps DOMAIN=hevy.yourdomain.com)
endif
	ssh root@$(DEPLOY_HOST) 'bash -s' < scripts/setup-vps.sh $(DOMAIN)

# ── Help ─────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
