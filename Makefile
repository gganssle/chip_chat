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

.PHONY: infra-bootstrap infra-init infra-fmt infra-validate infra-plan infra-apply infra-destroy infra-output infra-check-uploads

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

infra-check-uploads: ## Verify uploaded photos really do expire (read-only)
	./infra/scripts/check-uploads-retention.sh

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

# --- Deploying the chat app -------------------------------------------------
#
# Terraform owns the estate; a deploy owns the image. compute.tf deliberately
# ignores changes to the container image, so `terraform apply` never drags a
# deployed app back to the quickstart placeholder -- which means the image is
# pushed and rolled here rather than there.
#
# docs/deployment.md is the write-up, including why `provisioningState:
# Succeeded` is not the same as "deployed".

IMAGE_NAME ?= chip-chat-web
IMAGE_TAG  ?= latest
# linux/amd64 explicitly: Container Apps runs amd64 and this repository is
# developed on Apple silicon, where the default build would be arm64 and would
# fail to start with an exec-format error nobody enjoys diagnosing.
IMAGE_PLATFORM ?= linux/amd64

REGISTRY = $(shell $(TF_RUN) output -raw container_registry_login_server)
IMAGE    = $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
APP      = $(shell $(TF_RUN) output -raw container_app_name)
APP_URL  = $(shell $(TF_RUN) output -raw web_url)
RG       = $(shell $(TF_RUN) output -raw resource_group_name)

.PHONY: image image-push deploy deploy-check

image: ## Build the chat app image for Container Apps
	docker buildx build --platform $(IMAGE_PLATFORM) -t $(IMAGE) --load .

image-push: ## Push it, authenticating with your own Entra token
	az acr login --name $(shell $(TF_RUN) output -raw container_registry_name)
	docker push $(IMAGE)

deploy: ## Roll the Container App onto the pushed image
	az containerapp update -n $(APP) -g $(RG) --image $(IMAGE) -o none
	@$(MAKE) deploy-check

deploy-check: ## Wait until the NEWEST revision is the one actually serving
	@echo "Waiting for $(APP_URL)"
	@echo "  provisioningState says Succeeded long before this is true."
	@for i in $$(seq 1 40); do \
		latest=$$(az containerapp show -n $(APP) -g $(RG) --query properties.latestRevisionName -o tsv); \
		ready=$$(az containerapp show -n $(APP) -g $(RG) --query properties.latestReadyRevisionName -o tsv); \
		if [ "$$latest" = "$$ready" ] && \
		   [ "$$(curl -s -o /dev/null -w '%{http_code}' $(APP_URL)/healthz)" = "200" ]; then \
			echo "  serving $$ready at $(APP_URL)"; exit 0; \
		fi; \
		sleep 10; \
	done; \
	echo "  not serving after 400s -- see docs/deployment.md section 3.3"; exit 1
