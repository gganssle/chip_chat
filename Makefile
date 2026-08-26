# Chip Chat — developer task runner.
#
# Every target runs through `uv`, so a fresh clone needs only `uv` on PATH.
# `make setup` takes it from there.

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help setup fmt fmt-check lint typecheck imports test ci clean

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install every workspace package
	$(UV) sync --all-packages

fmt: ## Format the tree with ruff
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fmt-check: ## Fail if the tree is not formatted
	$(UV) run ruff format --check .

lint: ## Lint with ruff
	$(UV) run ruff check .

typecheck: ## Type check with mypy (strict on otel/ and agent/)
	$(UV) run mypy .

imports: ## Enforce the one-way dependency on otel/
	$(UV) run lint-imports

test: ## Run the unit tests
	$(UV) run pytest

ci: setup fmt-check lint typecheck imports test ## Everything CI runs

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -not -path './.beads/*' -exec rm -rf {} +
