# Linux/macOS equivalent of scripts/tasks.ps1.
#   make init   first-time setup (Vault init, migrations, admin user)
#   make up     start everything and unseal Vault      (make up LITE=1 leaves out CDC)
#   make down   stop (data is kept)
#   make test   unit tests; make test-integration runs against the running stack
#   make reset  DELETE all platform data and bootstrap material

export DATAPLAT_HOME ?= $(HOME)/.dataplat
ifdef LITE
export COMPOSE_PROFILES :=
else
export COMPOSE_PROFILES ?= full
endif

INFRA := postgres minio vault redpanda oxigraph
COMPOSE := docker compose

.PHONY: init up down vault unseal migrate test test-integration lint logs reset

init:
	mkdir -p $(DATAPLAT_HOME)
	$(COMPOSE) build
	$(COMPOSE) run --rm -T bootstrap bootstrap prepare
	$(COMPOSE) up -d $(INFRA)
	$(COMPOSE) run --rm -T bootstrap bootstrap vault
	$(COMPOSE) run --rm api migrate
	$(COMPOSE) run --rm api create-admin --username admin
	$(COMPOSE) up -d
	@echo "Console: http://localhost:3000  (log in as 'admin' with the password shown above)"

up:
	$(COMPOSE) up -d vault
	$(COMPOSE) run --rm -T bootstrap bootstrap unseal
	$(COMPOSE) up -d
	$(COMPOSE) run --rm api migrate

vault:
	$(COMPOSE) run --rm -T bootstrap bootstrap vault

unseal:
	$(COMPOSE) run --rm -T bootstrap bootstrap unseal

migrate:
	$(COMPOSE) run --rm api migrate

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail 100

test:
	cd backend && uv run --extra dev pytest -q

test-integration:
	cd backend && uv run --extra dev pytest -q -m integration

lint:
	cd backend && uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
	cd console && npm run lint && npm run typecheck

reset:
	@read -p "This deletes ALL platform data and $(DATAPLAT_HOME). Type 'reset' to continue: " a && [ "$$a" = reset ]
	$(COMPOSE) --profile full --profile tools down -v
	docker run --rm -v $(DATAPLAT_HOME):/h alpine sh -c 'rm -rf /h/*' || true
	rm -rf $(DATAPLAT_HOME)
