.PHONY: help fmt validate plan apply destroy test lint typecheck \
	local-up local-down local-wipe local-init local-smoke

help:
	@echo "fmt        terraform fmt -recursive"
	@echo "validate   terraform init -backend=false + validate"
	@echo "plan       terraform plan (needs AWS credentials)"
	@echo "apply      terraform apply"
	@echo "destroy    terraform destroy  <- run this when you finish a session"
	@echo "test       pytest (offline)"
	@echo "lint       ruff check"
	@echo "typecheck  mypy"
	@echo ""
	@echo "local-up    start the local KB stack (RustFS, kb-events, OpenSearch, Dashboards) + init"
	@echo "local-init  bucket, bucket event, least-privilege OpenSearch user (idempotent)"
	@echo "local-smoke check the storage behaviours the sync depends on"
	@echo "local-down  stop the stack, keep data"
	@echo "local-wipe  stop the stack and delete its volumes  <- the local terraform destroy"

fmt:
	cd terraform && terraform fmt -recursive

validate:
	cd terraform && terraform init -backend=false && terraform validate

plan:
	cd terraform && terraform plan

apply:
	cd terraform && terraform apply

destroy:
	cd terraform && terraform destroy

test:
	cd python && uv run pytest

lint:
	cd python && uv run ruff check .

typecheck:
	cd python && uv run mypy src

# ------------------------------------------------------------------ local KB (local-kb/PLAN.md)
LOCAL := cd local-kb && podman-compose

local-up:
	cd python && uv run python scripts/local_init.py env
	$(LOCAL) up -d
	$(MAKE) local-init

local-init:
	cd python && uv run python scripts/local_init.py init

local-smoke:
	cd python && uv run python scripts/local_smoke.py

local-down:
	$(LOCAL) down

local-wipe:
	$(LOCAL) down -v
