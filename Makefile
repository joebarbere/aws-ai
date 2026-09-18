.PHONY: help fmt validate plan apply destroy test lint typecheck

help:
	@echo "fmt        terraform fmt -recursive"
	@echo "validate   terraform init -backend=false + validate"
	@echo "plan       terraform plan (needs AWS credentials)"
	@echo "apply      terraform apply"
	@echo "destroy    terraform destroy  <- run this when you finish a session"
	@echo "test       pytest (offline)"
	@echo "lint       ruff check"
	@echo "typecheck  mypy"

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
