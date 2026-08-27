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

.PHONY: golden-check golden photos-check adversarial-check adversarial adversarial-baseline

golden-check: ## Check the golden set's coverage, free
	$(UV) run python -m chip_chat.eval.golden --check

golden: ## Run the golden set against the week-one slice and write the baseline
	$(UV) run python -m chip_chat.eval.golden --out eval/golden/BASELINE.md

photos-check: ## Check the labeled photo set's coverage, free
	$(UV) run python -m chip_chat.eval.photos --check

# The adversarial suite has a third free mode the other two do not, and it is
# the one worth running. `--structural` attacks the week-one slice with a model
# that complies with every attack, which measures the claim RFC-001 actually
# makes about the two launch gates: that they are properties of the design
# rather than of the model behaving. It calls no model and needs no credentials.
#
# Both exit non-zero while a launch gate is unmeasured. PRD section 12 makes
# both gates blocking, and a gate nobody measured has not passed.

adversarial-check: ## Check the adversarial suite's coverage, free
	$(UV) run python -m chip_chat.eval.adversarial --check

adversarial: ## Attack the slice with a model that complies, free
	$(UV) run python -m chip_chat.eval.adversarial --structural

adversarial-baseline: ## Run the suite against a real deployment and write the baseline
	$(UV) run python -m chip_chat.eval.adversarial --out eval/adversarial/BASELINE.md

# --- Trajectory and tool selection ------------------------------------------
#
# Issue #74, and the metric the whole five-lane architecture exists to get right.
# Both targets are free and neither calls a model.
#
# `trajectory-check` holds the dataset to what #74's report will claim about it:
# a row in every lane, boundary rows that make a wrong lane nameable, rows where
# a wrong query is observable at all. An unmet clause is a build failure, or the
# gap stays.
#
# `trajectory` runs those rows through the week-one slice with lane selection
# HANDED to it and writes the baseline. Read eval/trajectory/BASELINE.md's first
# paragraph before its table: a model told the answer measures nothing about a
# model, and what the run actually measures is the wiring at its ceiling.
#
# Either run exits non-zero on ONE thing: a turn that arrived as more than one
# trace.
# That is issue #103's propagation, it makes every other number in the document
# meaningless, and it is invisible otherwise -- the tool spans are all still
# there. The accuracy itself is deliberately not gated, because the slice
# registers six of the eleven tools and a gate that is red by construction is a
# gate somebody switches off. `make trace-boundary` is the propagation check
# itself; this one holds the reader.

.PHONY: trajectory-check trajectory trajectory-baseline

trajectory-check: ## Check the dataset can support #74's numbers, free
	$(UV) run python -m chip_chat.eval.trajectory --check

trajectory: ## Score trajectories against the slice with routing handed to it, free
	$(UV) run python -m chip_chat.eval.trajectory --ceiling

trajectory-baseline: ## Refresh eval/trajectory/BASELINE.md from that same free run
	$(UV) run python -m chip_chat.eval.trajectory --ceiling --out eval/trajectory/BASELINE.md

# The credentialed run is the same command with neither flag. It needs
# CHIP_CHAT_FOUNDRY_ENDPOINT and CHIP_CHAT_FOUNDRY_API_KEY and costs at least
# one model call per row, which is why it is not a target here:
#
#     uv run python -m chip_chat.eval.trajectory --out eval/trajectory/BASELINE.md

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

# --- The corpus -------------------------------------------------------------
#
# The weekly re-harvest of issue #38 and the freshness check it enforces. Both
# read and write a landing zone; neither costs anything but a third party's
# bandwidth, which is why the re-harvest is politeness-gated and conditional
# and the check is free.
#
# `reharvest` is what .github/workflows/reharvest.yml runs. Its exit statuses
# are three, not two: 0 published and fresh, 1 the harvest failed and nothing
# was published, 2 it published but the corpus is still stale. The last is a
# different problem with a different fix and should not read as the same
# failure.

LANDING      ?= landing
STORES       ?= 30
MAX_AGE_DAYS ?= 8
REPORT       ?=

.PHONY: reharvest freshness

reharvest: ## Re-harvest the corpus, report what changed, publish if it completed
	$(UV) run python -m chip_chat.harvest.sources.chipotle.reharvest \
		--landing $(LANDING) \
		--stores $(STORES) \
		--max-age-days $(MAX_AGE_DAYS) \
		$(if $(REPORT),--report $(REPORT),)

freshness: ## Report how old the corpus is, and fail if it has stopped moving
	$(UV) run python -m chip_chat.harvest \
		--landing $(LANDING) --max-age-days $(MAX_AGE_DAYS)

# --- The retrieval index ----------------------------------------------------
#
# Issue #48, RFC-001 section 08: THE INDEX IS REBUILT, NEVER PATCHED. Each build
# creates a new index named after the corpus release it holds and points the
# `corpus` ALIAS at it in one write, so the application never learns an index
# name and a build that dies cannot leave the corpus half-updated.
#
# `search-schema` is free and needs no credential -- the index definition is a
# pure function of the chunk schema, and on a tier that allows three indexes,
# reading the definition before creating one is worth the habit.
#
# The other targets need `az login`, the two data-plane roles search.tf grants,
# and a Foundry key for the index's query-time vectorizer. The key is not a
# convenience: the Free search tier gives the service no managed identity, so
# without it the service cannot embed a query and every caller has to embed its
# own. It lives in Key Vault and is read out here rather than kept on disk.
#
# None of these are in `make ci`. They need a credential and a live service, and
# a gate that needs a credential is not a gate. What is in CI is `search/tests`,
# which builds the same 31-chunk corpus end to end against a fake service.
#
# `search-verify` COSTS a minute, a few thousand embedding tokens, and three
# index builds against the live service. It is what turns #48's third and fourth
# acceptance criteria from claims into numbers -- it queries the alias fifty
# times a second across a real swap and then fails a build on purpose.

CHUNKS  ?=
RUN_ID  ?=
ALIAS   ?= corpus
KEY_VAULT ?= kv-chip-chat-c8b63a
VECTORIZER_SECRET ?= foundry-api-key

# Both from the stack, so neither is typed twice. Override either on the command
# line to point a build at a different service.
SEARCH_ENDPOINT ?= $(shell $(TF_RUN) output -raw search_endpoint 2>/dev/null)
FOUNDRY_ENDPOINT ?= $(shell $(TF_RUN) output -raw foundry_endpoint 2>/dev/null)

SEARCH_ENV = AZURE_SEARCH_ENDPOINT="$(SEARCH_ENDPOINT)" \
	CHIP_CHAT_FOUNDRY_ENDPOINT="$(FOUNDRY_ENDPOINT)" \
	CHIP_CHAT_SEARCH_VECTORIZER_KEY="$$(az keyvault secret show \
		--vault-name $(KEY_VAULT) --name $(VECTORIZER_SECRET) \
		--query value -o tsv 2>/dev/null)"

SEARCH_SOURCE = $(if $(CHUNKS),--chunks $(CHUNKS) --run-id $(RUN_ID),--landing $(LANDING))

.PHONY: search-schema search-status search-build search-build-only search-rollback \
        search-verify

search-schema: ## Print the index definition. Free, no credential, no network
	$(UV) run python -m chip_chat.search schema

search-status: ## What the service holds and what the alias serves
	$(SEARCH_ENV) $(UV) run python -m chip_chat.search status --alias $(ALIAS)

search-build: ## Rebuild the index from the live corpus release and swap to it
	$(SEARCH_ENV) $(UV) run python -m chip_chat.search build \
		--alias $(ALIAS) $(SEARCH_SOURCE)

search-build-only: ## Build and check a new index WITHOUT making it live
	$(SEARCH_ENV) $(UV) run python -m chip_chat.search build \
		--alias $(ALIAS) --no-swap $(SEARCH_SOURCE)

search-rollback: ## Point the alias back at the index before this one
	$(SEARCH_ENV) $(UV) run python -m chip_chat.search rollback --alias $(ALIAS)

search-verify: ## Hold the live index to #48.3 and #48.4 -- costs a minute
	$(SEARCH_ENV) $(UV) run python -m chip_chat.search verify \
		--alias $(ALIAS) $(SEARCH_SOURCE)
# --- The Snowflake serving layer --------------------------------------------
#
# Issue #41. Every role, grant and warehouse in `snowflake/sql/`, so the whole
# account can be rebuilt after the trial expires -- which it will, 30 days or
# $400 of credits from 2026-08-25, whichever comes first.
#
# `snowflake-apply` is safe to run repeatedly: it creates what is missing and
# re-asserts every warehouse setting, and it never drops anything. The only
# target that destroys is `snowflake-rebuild`, which drops the database first --
# that is what makes #41's fourth criterion something you can run rather than
# something you can assert.
#
# It also attaches #43's row access policies, which is the launch gate: an apply
# that has not run since a visitor-scoped table was added leaves that table
# readable by every visitor. `make ci` fails on the missing attachment and
# `snowflake-verify` fails on the unguarded table, so the gap has two ways of
# announcing itself and neither of them is somebody remembering.
#
# `snowflake-verify` is the one target here that spends credits, and about five
# cents of them: it wakes the serving warehouse and waits to watch it suspend,
# because #41's first criterion says "verified" rather than "configured".
# `snowflake-verify-fast` skips that minute and checks the setting instead.
#
# `snowflake-cap` is #88's half. An apply gives each warehouse a daily credit
# ceiling from numbers that come off the trial's own arithmetic; the cap on the
# WHOLE trial comes off the remaining balance instead, so no checked-in file has
# a default for it and this target takes the number:
#
#   make snowflake-cap QUOTA=60
#
# It refuses a quota the account has already spent past -- the one wrong number
# that suspends every warehouse the moment you press return. A rebuild drops the
# cap and does not put it back, and `snowflake-verify` fails on that by name.
#
# `snowflake-load` and `snowflake-load-sample` are issue #42's third criterion:
# the schema loaded with published data and queryable. They are the developer
# path deliberately -- #39 publishes the marts and the catalogue nightly out of
# Databricks, and this reads JSONL out of a directory. One transaction per
# table, TRUNCATE and COPY together, so a conversation querying mid-load sees
# one generation or the other and never half of either.
#
# `snowflake-load-sample` takes the catalogue fixture committed by #24, which is
# real harvested data and needs no landing zone. `snowflake-load` takes one that
# has been harvested and generated.
#
# None of these are in `make ci`. They need a `snow` connection and a live trial,
# and a gate that needs a credential and a credit balance is not a gate. What is
# in CI is `snowflake/tests/`, which holds the SQL to `chip_chat.snowflake.account`
# and `chip_chat.snowflake.schema` for free.

.PHONY: snowflake-plan snowflake-apply snowflake-cap snowflake-verify \
        snowflake-verify-fast snowflake-rebuild snowflake-load \
        snowflake-load-sample

snowflake-plan: ## Print the SQL files an apply would run, in order
	$(UV) run python -m chip_chat.snowflake.apply --plan

snowflake-apply: ## Create or re-assert every Snowflake object from snowflake/sql
	$(UV) run python -m chip_chat.snowflake.apply

snowflake-cap: ## Cap the whole trial: make snowflake-cap QUOTA=<credits>
	@test -n "$(QUOTA)" || { \
		echo "QUOTA=<credits> is required, and no file here can guess it."; \
		echo "Snowsight -> Admin -> Cost Management has the remaining balance in"; \
		echo "dollars; Enterprise credits are about \$$3 each. Pass what you are"; \
		echo "prepared to spend from now:  make snowflake-cap QUOTA=60"; \
		exit 2; }
	$(UV) run python -m chip_chat.snowflake.apply --cap $(QUOTA)

snowflake-load-sample: ## Load the committed catalogue fixture -- #42 criterion 3
	$(UV) run python -m chip_chat.snowflake.load catalog/tests/fixtures/catalog

snowflake-load: ## Load a harvested and generated landing zone into the serving layer
	$(UV) run python -m chip_chat.snowflake.load \
		$(LANDING)/catalog $(LANDING)/accounts/synthetic

snowflake-verify: ## Check the live account against issues #41, #42, #43 and #88
	$(UV) run python -m chip_chat.snowflake.verify

snowflake-verify-fast: ## The same, minus the minute spent watching it suspend
	$(UV) run python -m chip_chat.snowflake.verify --no-watch

snowflake-rebuild: ## Tear the account down and build it back -- #41 criterion 4
	$(UV) run python -m chip_chat.snowflake.apply --reset --yes
	@$(MAKE) snowflake-verify

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
