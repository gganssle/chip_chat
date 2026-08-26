# Local setup

From a clean Mac to a passing test suite and a working connection to the dev
environment. Every command here was run on 2026-08-25 and its output is what is
recorded below; where a step is deliberately *not* done yet, it says so and says
why.

The short version:

```bash
brew install uv gh
brew install azure-cli
brew install hashicorp/tap/terraform
brew install databricks/tap/databricks
az login
make setup && make ci
```

The rest of this document explains each of those, what to expect, and the two
things that cost an hour to discover.

## What gets installed

| Tool | Version verified | Where it comes from | Needed for |
| --- | --- | --- | --- |
| `uv` | 0.12.6 | `brew install uv` | Python, the virtualenv, the lockfile, every `make` target |
| `az` | 2.89.1 | `brew install azure-cli` | Everything Azure: the subscription, the resource group, Key Vault |
| `terraform` | 1.15.8 | `brew install hashicorp/tap/terraform` | `infra/` — all Azure resources |
| `databricks` | 1.13.0 | `brew install databricks/tap/databricks` | `databricks/` — the nightly lakehouse |
| `snow` | **not installed** | `brew install snowflake-cli` (3.25.0) | `snowflake/` — deliberately deferred, see below |
| `gh` | any recent | `brew install gh` | Issues are the tracker; several `make` and agent flows shell out to it |
| `docker` | 29.5.2 | Docker Desktop | `make dev` — the local Phoenix container, see [local-tracing.md](local-tracing.md) |

Python itself is not on that list on purpose. `uv` downloads and pins the
interpreter named in [`.python-version`](../.python-version) (3.13), so a system
Python — pyenv, Homebrew, or the one Apple ships — is neither required nor
consulted. Terraform's pin lives in [`.terraform-version`](../.terraform-version)
in the same spirit; `tenv` and `tfenv` both read it, and if you installed
Terraform straight from the tap, treat it as the version this repository is known
to work with.

### Two things that cost time

**Terraform is not in homebrew-core.** HashiCorp's 2023 licence change moved it
out, and `brew install terraform` now fails with *"No available formula"*. It
installs from HashiCorp's own tap:

```bash
brew install hashicorp/tap/terraform
```

`brew tap` afterwards should list `hashicorp/tap`. The same is true of the
Databricks CLI, which lives in `databricks/tap` — the `databricks` name in
homebrew-core is a different, unrelated formula.

**Building anything in this workspace that links Dolt needs the icu4c headers.**
The beads issue tracker is backed by Dolt, and Dolt's cgo build reaches for
ICU. Without the headers the build dies on:

```
'unicode/regex.h' file not found
```

The fix is the keg-only `icu4c` formula plus the flags that point cgo at it:

```bash
brew install icu4c
export CGO_CFLAGS="-I$(brew --prefix icu4c)/include"
export CGO_LDFLAGS="-L$(brew --prefix icu4c)/lib"
```

On this machine that resolves to `/opt/homebrew/opt/icu4c@78`, which does contain
`include/unicode/regex.h`. This only matters if you are building `bd` or Dolt from
source; installing the released binaries needs none of it.

## Authentication, one platform at a time

Four platforms, four authentication stories, and two of them are on purpose not
finished yet.

### Azure — done, and it is the root of everything else

```bash
az login
az account show
```

`az login` opens a browser; `az login --use-device-code` is the fallback over SSH.
Verified working, signed in as `grahamganssle@gmail.com`:

| Field | Value |
| --- | --- |
| Subscription | `Azure subscription 1` |
| Subscription id | `c8b63a71-218d-4d4c-991c-b963ed2fd1f0` |
| Tenant id | `afededb7-6b20-4ec3-afd5-b27ac9242bbf` |
| Tenant domain | `grahamgansslegmail.onmicrosoft.com` |
| State | Enabled |
| Region | East US 2 — see [the region decision](service-inventory.md#region-recommendation-east-us-2) |

The resource group and Key Vault from
[issue #3](https://github.com/gganssle/chip_chat/issues/3) already exist in that
subscription:

| Resource | Value |
| --- | --- |
| Resource group | `rg-chip-chat` (eastus2) |
| Key Vault | `kv-chip-chat-c8b63a` |
| Vault URI | `https://kv-chip-chat-c8b63a.vault.azure.net/` |
| Authorization | Azure RBAC, not vault access policies |

Verify your own access reaches the vault — an empty list is the correct answer
today, an authorization error is not:

```bash
az group show -n rg-chip-chat -o table
az keyvault secret list --vault-name kv-chip-chat-c8b63a -o table
```

This is the step everything else hangs off. `az login` is not just for the `az`
CLI: it writes the credential that `DefaultAzureCredential` picks up, which is how
local Python processes reach Key Vault without a secret ever existing on disk.

### Databricks — installed, authenticated later

The CLI is installed and on PATH. It is **not** authenticated, because there is no
workspace to authenticate against yet — that arrives with the Phase 2 lakehouse.
Today `databricks auth describe` reports exactly that:

```
Unable to authenticate: default auth: cannot configure default credentials
```

That is the expected state, not a broken install. When the workspace exists, the
one command to run is OAuth user-to-machine, which stores a token in
`~/.databrickscfg` and never touches your shell history:

```bash
databricks auth login --host https://<workspace>.azuredatabricks.net --profile chip-chat
databricks auth describe --profile chip-chat        # verifies
databricks current-user me --profile chip-chat      # verifies harder
```

Do **not** use `databricks configure --token`: it prompts for a personal access
token, and a PAT is a long-lived secret that we would then have to store. OAuth
U2M refreshes itself and expires on its own. If a PAT is ever genuinely needed —
for a job principal, not for a human — it goes in Key Vault, never in
`~/.databrickscfg` by hand.

### Snowflake — deliberately not installed

`snow` is **not installed on this machine, and that is on purpose.**

The Snowflake trial is a 30-day clock carrying roughly $400 of credits, and
[issue #40](https://github.com/gganssle/chip_chat/issues/40) holds it until Phase 4
for one reason: started now, the clock burns through the whole lakehouse build and
expires somewhere around the vision lane. Day one of the trial should be a
productive day. Installing the CLI early invites someone to run `snow connection
add`, which invites starting the trial, so the install waits with the trial.

When Phase 4 arrives, the formula is in homebrew-core (3.25.0 as of this writing):

```bash
brew install snowflake-cli
snow --version
snow connection add --connection-name chip-chat   # prompts; see the secrets section
snow connection test --connection-name chip-chat  # verifies
```

If you are reading this and `snow` is missing, nothing is wrong. Check
[#40](https://github.com/gganssle/chip_chat/issues/40) before installing it.

### Arize — no CLI

Arize has no CLI to install. It is an API key, and the API key lives in Key Vault
like every other non-Azure credential. Phase 7 wires it up; there is nothing to
do locally before then.

## How secrets reach a local process

One mechanism, used everywhere:

> **Key Vault holds every secret. `az login` is how a local process is allowed to
> read them. Nothing else is a secret store — not `.env`, not `~/.databrickscfg`,
> not the shell.**

Concretely:

- **Azure services** need no secret at all. `DefaultAzureCredential` finds the
  token `az login` already wrote, and in Azure it finds the managed identity
  instead. The same code works in both places with nothing configured.
- **Non-Azure credentials** — Snowflake, the Arize API key, the ops API function
  key, a Databricks PAT if one ever exists — are stored as Key Vault secrets and
  fetched at process start over that same credential.
- **`.env` carries configuration, never credentials.** Copy
  [`.env.example`](../.env.example) to `.env` and fill in resource *names*:
  subscription id, resource group, vault URI. `.env` is gitignored; `.env.example`
  is committed and contains no values worth protecting.

```bash
cp .env.example .env      # then edit — every value in it is a name, not a secret
```

### No secret is ever pasted into a shell

This is a hard rule, and it has teeth: a value typed on a command line lands in
`~/.zsh_history`, in the process table for every other process on the machine, and
in any terminal scrollback that gets screenshotted. Three consequences:

- **Writing a secret to the vault reads it from a file or a pipe, never an
  argument.** `--file` avoids the history entirely:

  ```bash
  az keyvault secret set --vault-name kv-chip-chat-c8b63a --name arize-api-key --file ./key.txt
  shred -u ./key.txt 2>/dev/null || rm -P ./key.txt
  ```

  `--value "$SOMETHING"` is acceptable only when `$SOMETHING` came from another
  command's output and was never typed. `--value sk-abc123...` is not.

- **Reading a secret goes into a variable, not onto the screen.** `az keyvault
  secret show --query value -o tsv` prints to stdout, so redirect or capture it;
  do not leave it in scrollback.

- **Interactive prompts are preferred over flags** for the tools that offer them.
  `snow connection add` and `databricks auth login` both prompt, and neither
  echoes. That is why the commands above use them instead of the flag forms.

If a secret does end up in your history, treat it as disclosed: rotate it, then
clean the history. The rotation is the part that matters.

## Getting the repository running

With `uv` on PATH and the repository cloned:

```bash
make setup      # uv sync --all-packages — creates .venv, installs all 11 members
make ci         # fmt-check, lint, typecheck, imports, test — what CI runs
```

`make setup` needs no Python of its own; `uv` fetches 3.13 per
`.python-version`. On a clean machine the whole thing is under a minute.

`make ci` is green as of 2026-08-25 — 13 files analyzed for import contracts (1
contract kept, 0 broken) and 18 tests passing across the eleven workspace
members. `make help` lists the individual targets if you want to run one stage at
a time.

Two useful smaller commands:

```bash
make test       # just pytest
make imports    # just the one-way dependency contract on otel/
```

With Docker running, `make dev` adds the local observability stack — Phoenix in a
container, plus one instrumented session sent through it so the UI is not empty:

```bash
make dev        # up, healthy, and one session sent
make dev-down   # down, and the traces go with it
```

The loop, the span tree it produces and what to do when it does not work are in
[local-tracing.md](local-tracing.md).

## Hitting the dev environment

There is no application to point at yet — Phase 0 is scaffolding, and the FastAPI
service arrives in Phase 8. What "hitting the dev environment" means today is
proving the credential chain works end to end, which is the part that is easy to
get wrong and expensive to discover late:

```bash
az account show --query "{sub:name, id:id, state:state}" -o table
az group show -n rg-chip-chat -o table
az keyvault secret list --vault-name kv-chip-chat-c8b63a -o table
```

Three successes mean: you are signed in, the resource group is reachable, and your
identity has an RBAC role on the vault. From Python, the same three facts:

```python
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

client = SecretClient(
    vault_url="https://kv-chip-chat-c8b63a.vault.azure.net/",
    credential=DefaultAzureCredential(),
)
print(list(client.list_properties_of_secrets()))  # [] today, and that is correct
```

An empty list is success. `ClientAuthenticationError` means `az login` has
expired — run it again. A 403 means the RBAC role assignment is missing, which is
[issue #3](https://github.com/gganssle/chip_chat/issues/3) territory, not a local
setup problem.

Terraform is the fourth check, once `infra/` has configuration in it
([#5](https://github.com/gganssle/chip_chat/issues/5)):

```bash
cd infra && terraform init && terraform plan
```

The Azure provider authenticates through the same `az login` session, so if the
three commands above passed, this one has what it needs.

## Verification checklist

Run all of these on a machine you believe is set up. Every line should succeed
except the two marked otherwise.

| Command | Expected |
| --- | --- |
| `uv --version` | 0.12.6 or later |
| `az account show` | The subscription table above |
| `terraform version` | v1.15.8, matching `.terraform-version` |
| `databricks version` | v1.13.0 |
| `databricks auth describe` | **Fails** — no workspace yet, see above |
| `snow --version` | **Not found** — deliberate, see [#40](https://github.com/gganssle/chip_chat/issues/40) |
| `az keyvault secret list --vault-name kv-chip-chat-c8b63a` | Empty, no error |
| `make setup && make ci` | Green |
| `docker version` | A `Server:` section — the daemon is running |
| `make dev` | Container healthy, three turns sent, tree visible at localhost:6006 |

## Where the identifiers live

This document is the reader-facing copy. The authoritative record of what exists
in Azure is [service-inventory.md](service-inventory.md), which carries a source
URL and an access date per row and says how long each answer is likely to stay
true. Where the two disagree about a fact, the inventory wins; where they disagree
about a procedure, this file wins.
