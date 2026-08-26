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
        dev dev-down dev-logs trace trace-boundary

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

trace-boundary: ## Send one turn ACROSS the app-agent boundary (two service names, one trace)
	OTEL_EXPORTER_OTLP_ENDPOINT=$(OTEL_EXPORTER_OTLP_ENDPOINT) \
	CHIP_CHAT_AGENT_COMMAND="$(AGENT_COMMAND)" \
		$(UV) run python -m chip_chat.otel.boundary

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -not -path './.beads/*' -exec rm -rf {} +

# By default the agent half is emitted in this process under its own provider,
# which proves the propagation but not the image. Set AGENT_COMMAND to run the
# real container as the second process -- which is what issue #103 asks for, and
# what `make agent-image-boundary` does for you.
AGENT_COMMAND ?=

# --- Agent image ------------------------------------------------------------
#
# Decision D8 made the agent a hosted agent: our container, run by Foundry. The
# image is therefore a build artefact of this repository, and the registry is in
# the Terraform below rather than made by hand.

AGENT_IMAGE_NAME ?= chip-chat-agent
AGENT_IMAGE_TAG  ?= dev
AGENT_IMAGE      ?= $(AGENT_IMAGE_NAME):$(AGENT_IMAGE_TAG)

# Empty means "local build only". `make infra-output` prints the login server as
# container_registry_login_server; export it to push.
ACR_LOGIN_SERVER ?=

.PHONY: agent-image agent-image-check agent-image-push agent-image-boundary agent-version

agent-image: ## Build the agent container image
	docker build -f agent/Dockerfile -t $(AGENT_IMAGE) .

agent-image-check: agent-image ## Build, then ask the image what it is and where its spans go
	docker run --rm $(AGENT_IMAGE) check

agent-image-push: ## Push the agent image to the registry (needs `az acr login`)
	@test -n "$(ACR_LOGIN_SERVER)" || { \
		echo "ACR_LOGIN_SERVER is empty. Read it with:"; \
		echo "  make infra-output | grep container_registry_login_server"; \
		echo "then: az acr login --name <registry> && make agent-image-push ACR_LOGIN_SERVER=..."; \
		exit 1; }
	docker tag $(AGENT_IMAGE) $(ACR_LOGIN_SERVER)/$(AGENT_IMAGE_NAME):$(AGENT_IMAGE_TAG)
	docker push $(ACR_LOGIN_SERVER)/$(AGENT_IMAGE_NAME):$(AGENT_IMAGE_TAG)

agent-image-boundary: agent-image ## One turn across a REAL process boundary: the app here, the agent in the container
	@$(MAKE) trace-boundary AGENT_COMMAND="docker run --rm --network host \
		-e TRACEPARENT -e TRACESTATE -e BAGGAGE \
		-e OTEL_EXPORTER_OTLP_ENDPOINT=$(OTEL_EXPORTER_OTLP_ENDPOINT) \
		$(AGENT_IMAGE) agent-half"

agent-version: ## Print the hosted-agent version manifest for the current image
	$(UV) run python -m chip_chat.agent.version render \
		--image $(if $(ACR_LOGIN_SERVER),$(ACR_LOGIN_SERVER)/,)$(AGENT_IMAGE_NAME):$(AGENT_IMAGE_TAG)

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

.PHONY: verify-models verify-chat verify-vision verify-tools verify-tools-bare

verify-models: verify-chat verify-vision ## Prove both model deployments answer

verify-chat: ## Complete a chat call against the deployed chat model
	$(UV) run python -m chip_chat.agent.verify chat

verify-vision: ## Complete a vision call against an image in blob storage
	$(UV) run python -m chip_chat.agent.verify vision

# Registers the eleven tools against the deployed model and reports which lane it
# picked for each case. `verify-tools-bare` sends the same cases with no system
# prompt: the gap between the two runs is how much of lane selection the prompt
# is carrying, and issue #60 wants that gap small. Pass --deployment to compare
# models, which is the variable the first runs found actually mattered.

verify-tools: ## Measure tool selection across the five lanes
	$(UV) run python -m chip_chat.agent.selection

verify-tools-bare: ## The same cases, with no system prompt at all
	$(UV) run python -m chip_chat.agent.selection --no-prompt

# --- Evaluation sets --------------------------------------------------------
#
# Both checks are free: they load a set, refuse one that contradicts itself, and
# report which of its ticket's scope clauses it meets. Neither calls a model, so
# both belong in CI. Pass --catalog a build the deployment actually serves —
# that is what turns the golden set's menu terms into a staleness detector
# rather than a comment.

.PHONY: golden-check golden photos-check

golden-check: ## Check the golden set's coverage, free
	$(UV) run python -m chip_chat.eval.golden --check

golden: ## Run the golden set against the week-one slice and write the baseline
	$(UV) run python -m chip_chat.eval.golden --out eval/golden/BASELINE.md

photos-check: ## Check the labeled photo set's coverage, free
	$(UV) run python -m chip_chat.eval.photos --check

# --- The versioned dataset --------------------------------------------------
#
# Issue #72. Both sets, promoted into one dataset with a content hash for a
# version. The build is a pure function of two committed JSON files, so
# `dataset-check` is free and belongs in CI -- and it fails when the committed
# eval/dataset/DATASET.json is not what those files currently build, which is
# what stops the version from being a number somebody forgets to move.
#
# `dataset-upload` is the only target here that talks to anything. It needs
# ARIZE_SPACE_ID and ARIZE_API_KEY, and `--with arize` because the SDK is
# deliberately not in the lockfile -- eval/pyproject.toml has the argument.

.PHONY: dataset-check dataset dataset-upload

dataset-check: ## Build the eval dataset and hold the committed build to it, free
	$(UV) run python -m chip_chat.eval.dataset --check

dataset: ## Rebuild eval/dataset/DATASET.json after adding a case or a frame
	$(UV) run python -m chip_chat.eval.dataset --write

dataset-upload: ## Create the Arize dataset, or add a version holding the new entries
	$(UV) run --with arize python -m chip_chat.eval.dataset --upload

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
