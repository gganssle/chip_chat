# Chip Chat — developer task runner.
#
# Every target runs through `uv`, so a fresh clone needs only `uv` on PATH.
# `make setup` takes it from there.

UV ?= uv
COMPOSE ?= docker compose

# Where the local stack answers, and therefore where `make trace` sends spans.
# `?=` means an exported OTEL_EXPORTER_OTLP_ENDPOINT wins, which is how you point
# the same demo at a different backend without editing anything.
PHOENIX_PORT ?= 6006
PHOENIX_GRPC_PORT ?= 4317
PHOENIX_URL ?= http://localhost:$(PHOENIX_PORT)
OTEL_EXPORTER_OTLP_ENDPOINT ?= $(PHOENIX_URL)

# compose.yaml reads the two ports out of the environment, so they have to be
# exported rather than merely defined -- otherwise `make dev PHOENIX_PORT=6007`
# would move the endpoint and leave the container where it was.
export PHOENIX_PORT
export PHOENIX_GRPC_PORT

.DEFAULT_GOAL := help
.PHONY: help setup fmt fmt-check lint typecheck imports test ci clean \
        dev dev-down dev-logs trace

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

dev: ## Start the local stack and send one instrumented turn through it
	$(COMPOSE) up -d --wait
	@$(MAKE) trace
	@echo
	@echo "Phoenix is at $(PHOENIX_URL) — open it and read the span tree."
	@echo "docs/local-tracing.md explains what you are looking at."

dev-down: ## Stop the local stack and discard its traces
	$(COMPOSE) down

dev-logs: ## Follow the local stack's logs
	$(COMPOSE) logs -f

trace: ## Send one demo session to OTEL_EXPORTER_OTLP_ENDPOINT
	OTEL_EXPORTER_OTLP_ENDPOINT=$(OTEL_EXPORTER_OTLP_ENDPOINT) \
		$(UV) run python -m chip_chat.otel.smoke

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -not -path './.beads/*' -exec rm -rf {} +

# --- Infrastructure ---------------------------------------------------------
#
# Terraform for every Azure resource. `make infra-destroy` is the one-command
# teardown that issue #5 exists for.

TF        ?= terraform
TF_DIR    := infra/terraform
TF_RUN    := $(TF) -chdir=$(TF_DIR)

.PHONY: infra-bootstrap infra-init infra-fmt infra-validate infra-plan infra-apply infra-destroy infra-output

infra-bootstrap: ## Create the remote state storage account (once per subscription)
	./infra/scripts/bootstrap-state.sh

infra-init: ## Initialise Terraform against the remote backend
	$(TF_RUN) init

infra-fmt: ## Format the Terraform
	$(TF) fmt -recursive $(TF_DIR)

infra-validate: ## Validate the Terraform
	$(TF_RUN) validate

infra-plan: ## Show what apply would change
	$(TF_RUN) plan

infra-apply: ## Stand up the Azure estate
	$(TF_RUN) apply

infra-destroy: ## Tear the whole Azure estate down
	$(TF_RUN) destroy

infra-output: ## Print stack outputs
	$(TF_RUN) output

# --- Model deployments ------------------------------------------------------
#
# These call Azure and spend tokens (a few hundredths of a cent each). They are
# not part of `make ci` for that reason -- a gate that costs money and needs a
# logged-in human is not a gate.
#
# They read CHIP_CHAT_FOUNDRY_* from the environment. `.env.example` has the
# live values; export them or use `env $$(grep -v "^#" .env | xargs) make ...`.

.PHONY: verify-models verify-chat verify-vision

verify-models: verify-chat verify-vision ## Prove both model deployments answer

verify-chat: ## Complete a chat call against the deployed chat model
	$(UV) run python -m chip_chat.agent.verify chat

verify-vision: ## Complete a vision call against an image in blob storage
	$(UV) run python -m chip_chat.agent.verify vision
