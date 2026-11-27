# Common commands for provenance-jobs. Run `make` or `make help` to list them.

# Load .env if present so targets can use the same values as Docker Compose.
-include .env
export

COMPOSE := docker compose
POSTGRES_USER ?= provenance
POSTGRES_DB ?= provenance

.DEFAULT_GOAL := help
.PHONY: help env up down clean ps logs psql redis-cli check test submit tamper resilience

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(firstword $(MAKEFILE_LIST)) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example (keeps an existing .env)
	@test -f .env && echo ".env already exists" || (cp .env.example .env && echo "created .env")

up: env ## Start the stack and wait until every service is healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) down

clean: ## Stop the stack and DELETE all data volumes
	$(COMPOSE) down -v

ps: ## Show service status and health
	$(COMPOSE) ps

logs: ## Follow logs of all services
	$(COMPOSE) logs -f --tail=100

psql: ## Open a psql shell in the database
	$(COMPOSE) exec postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

redis-cli: ## Open a redis-cli shell
	$(COMPOSE) exec redis redis-cli

check: ## Smoke test: Postgres answers SQL, Redis answers PING, AOF is on
	@echo "--- postgres ---"
	@$(COMPOSE) exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -tAc "select 'postgres ok: ' || version();"
	@echo "--- redis ---"
	@$(COMPOSE) exec -T redis redis-cli ping
	@$(COMPOSE) exec -T redis redis-cli config get appendonly | tr '\n' ' '; echo

test: ## Run the unit tests inside the api container
	$(COMPOSE) exec -T api pytest -q

submit: ## Submit 10 jobs and print final status + chain validity
	$(COMPOSE) exec -T api python scripts/submit_jobs.py

tamper: ## Edit one event in the DB and show the chain break
	$(COMPOSE) exec -T api python scripts/tamper.py

resilience: ## Kill the worker mid-run and check every job still finishes (runs on the host)
	python3 scripts/resilience_demo.py
