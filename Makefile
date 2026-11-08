.PHONY: help fmt validate plan apply destroy test lint typecheck \
	local-up local-down local-wipe local-init local-smoke local-models local-watch local-serve local-eval local-sample-docs local-psql local-pg-inspect local-corpus local-bench

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
	@echo "local-up    start the local KB stack (RustFS, kb-events, OpenSearch, Dashboards, Postgres) + init"
	@echo "local-init  bucket, bucket event, least-privilege OpenSearch user, Postgres schema (idempotent)"
	@echo "local-smoke check the storage behaviours the sync depends on"
	@echo "local-models start LM Studio's server and load kb-embed + kb-llm"
	@echo "local-watch sync knowledge bases as their buckets change (Ctrl-C to stop)"
	@echo "local-serve bedrock-agent + bedrock-agent-runtime on :8099 for boto3, syncing on changes"
	@echo "local-eval  20-question RAG evaluation, SEMANTIC vs HYBRID (KB=<knowledge base id>)"
	@echo "local-sample-docs  upload local-kb/sample-docs/kb/ to s3://kb-data/kb/"
	@echo "local-psql  psql into the RDS store's Postgres as bedrock_user"
	@echo "local-pg-inspect  what the RDS store holds: chunks, Bedrock's fields, indexes (TABLE=...)"
	@echo "local-corpus  download the radio astronomy bench corpora from arXiv (slow: 3 s/request)"
	@echo "local-bench   OpenSearch vs pgvector on one corpus (CORPUS=sample|medium|large)"
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

local-watch:
	cd python && uv run local-kb watch

local-serve:
	cd python && uv run local-kb serve --watch

local-sample-docs:
	cd python && uv run python -c 'import pathlib; from aws_ai.local_kb.services import local_services; \
	s3 = local_services().s3; \
	[s3.upload_file(str(p), "kb-data", f"kb/{p.name}") or print("uploaded", p.name) \
	 for p in sorted(pathlib.Path("../local-kb/sample-docs/kb").iterdir())]'

local-psql:
	@# The password travels in the environment (-e NAME copies it), never on a command line.
	PGPASSWORD="$$(grep ^BEDROCK_PG_PASSWORD= local-kb/.env | cut -d= -f2-)" \
	  podman exec -it -e PGPASSWORD local-kb_postgres_1 psql -h 127.0.0.1 -U bedrock_user -d bedrock

local-pg-inspect:
	podman exec -i local-kb_postgres_1 psql -U postgres -d bedrock -v ON_ERROR_STOP=1 \
	  $(if $(TABLE),-v table=$(TABLE)) < local-kb/pg/inspect.sql

local-corpus:
	cd python && uv run python scripts/radio_corpus.py fetch && uv run python scripts/radio_corpus.py abstracts

local-bench:
	cd python && uv run local-kb bench --corpus $(or $(CORPUS),sample)

local-eval:
	cd python && uv run local-kb evaluate --knowledge-base-id $(KB) --output ../local-kb/eval/report.json

local-down:
	$(LOCAL) down

# Thinking stays on unless each request sends reasoning_effort "none" (see PLAN.md, Phase 2).
LMS := $(HOME)/.lmstudio/bin/lms
local-models:
	$(LMS) server start --port 1234
	-$(LMS) unload kb-embed
	-$(LMS) unload kb-llm
	$(LMS) load text-embedding-qwen3-embedding-0.6b --identifier kb-embed --gpu max -y
	$(LMS) load qwen/qwen3.5-9b --identifier kb-llm --context-length 16384 --parallel 1 --gpu max -y

local-wipe:
	$(LOCAL) down -v
