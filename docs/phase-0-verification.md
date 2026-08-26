# Phase 0 verification

The Phase 0 verification pass required by
[issue #2](https://github.com/gganssle/chip_chat/issues/2) lives in
**[service-inventory.md](service-inventory.md)**.

It records, for each service across Azure, Snowflake, Databricks and Arize, what was
checked, when, and the current answer — with a source URL and access date per row —
followed by a "What changed versus the plan" section and the region decision.

Two of that issue's acceptance criteria are answered there directly:

- **The reranker question** ([issue #10](https://github.com/gganssle/chip_chat/issues/10),
  RFC-001 §13 Q3) — see
  [The reranker decision](service-inventory.md#the-reranker-decision-issue-10).
- **Region selection for the whole stack** — see
  [Region recommendation: East US 2](service-inventory.md#region-recommendation-east-us-2).

This file exists because the ticket names `docs/phase-0-verification.md` and the
dispatched task names `docs/service-inventory.md`. The content is in the latter.

---

## Phase 0 provisioning

The Azure account groundwork — subscription, resource group, Key Vault, managed
identity and budget, created by hand for
[issue #3](https://github.com/gganssle/chip_chat/issues/3) — and the Terraform
that adopts and extends it for
[issue #5](https://github.com/gganssle/chip_chat/issues/5) are documented in
**[../infra/README.md](../infra/README.md)**, with the measured results in its
[Verified](../infra/README.md#verified) section.

Two numbers from that pass belong here because they answer questions the
planning documents left open:

- **Container Apps cold start: 0.26 s** to first byte from zero replicas, 0.21 s
  warm, measured on the default FQDN with a trivial container.
  *system-design.md* estimated "a couple of seconds" and noted that Microsoft
  publishes no figure — item 20 of [service-inventory.md](service-inventory.md)
  asked for it to be measured in Phase 0. A real FastAPI image will be slower;
  re-measure once one is deployed.
- **Full teardown: 9m20s** for a 32-resource stack, leaving no resource group and
  no soft-deleted names behind.

---

## Model deployments

[Issue #8](https://github.com/gganssle/chip_chat/issues/8)'s fourth acceptance
criterion: deployment capacity and region, recorded. Everything below was
measured on **26 August 2026** with az CLI 2.89.1 against subscription
`c8b63a71-218d-4d4c-991c-b963ed2fd1f0`.

**Region: East US 2**, unchanged and not re-litigated — it is fixed by Snowflake
Cortex Analyst, see
[Region recommendation](service-inventory.md#region-recommendation-east-us-2).

| Deployment | Model | SKU | Capacity | Lane |
| --- | --- | --- | --- | --- |
| `gpt-5-mini` | `gpt-5-mini` 2025-08-07 | GlobalStandard | 10 (10,000 TPM) | Agent chat + tool calling |
| `gpt-4.1-mini` | `gpt-4.1-mini` 2025-04-14 | GlobalStandard | 10 (10,000 TPM) | Photo lane (vision) |

Deployment names are model names; which deployment serves which *lane* is
`CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT` / `..._VISION_DEPLOYMENT`. That split is what
makes an eval experiment a new map entry plus an environment variable rather than
a rename.

### The quota finding, which decided the models

The models above are not the newest available. They are the newest with **any**
quota in East US 2 on this subscription. From

```
az cognitiveservices usage list -l eastus2 -o table
```

most families report a limit of **zero TPM** — including `gpt-5`, `gpt-5.1`,
`gpt-5.2`, `gpt-5.4`, `gpt-4o`, `gpt-4.1` and `o3`. Everything with GlobalStandard
quota, `used/limit` in thousands of TPM, before deploying anything:

| Quota | Before | After |
| --- | --- | --- |
| `OpenAI.GlobalStandard.gpt-5-mini` | 0 / 500 | **10 / 500** |
| `OpenAI.GlobalStandard.gpt4.1-mini` | 0 / 200 | **10 / 200** |
| `OpenAI.GlobalStandard.o4-mini` | 0 / 100 | 0 / 100 |
| `OpenAI.DataZoneStandard.gpt-5.4-mini` | 0 / 200 | 0 / 200 |

The "after" column is the check that capacity is what the Terraform says: the two
deployments consumed exactly the 10 they declare.

Note `gpt-5.4-mini` has 200 on **DataZoneStandard** and **zero** on
GlobalStandard. A newer model being visible in `list-models` says nothing about
being deployable, and the two commands disagree in a way that will waste an
afternoon if you trust the first. Both models carry `agentsV2: true`, which is
what the hosted agent runtime
([decision](decisions/foundry-agent-shape.md)) requires.

**Why two different models rather than one used twice.** They draw on separate
TPM pools. A burst of photo uploads cannot starve the agent's conversation, and a
long agent turn cannot stall the photo lane. Secondarily, `gpt-4.1-mini` is
non-reasoning: describing a meal is a single-shot perception call, and a
reasoning model would bill thinking tokens for it.

### Cost: zero standing charge

**Both deployments are GlobalStandard, which is pay-per-token. Neither carries a
standing hourly cost.** Creating them did not move the monthly floor. Only a
provisioned SKU (`ProvisionedManaged`, PTU) bills by the hour whether or not a
token is spent, and `var.model_deployments` now has a validation block that
refuses one outright — because nothing else in this system would stop it. The
budget from [#3](https://github.com/gganssle/chip_chat/issues/3) only *alerts*,
and the inline spend cap from
[#11's sibling](https://github.com/gganssle/chip_chat/issues/11) is a library
with no request path wired to it yet. **Capacity 10 is the actual throttle.**

Retail prices, [Azure Retail Prices API](https://prices.azure.com/api/retail/prices),
East US 2, per 1M tokens, checked 26 August 2026:

| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| `gpt-5-mini` | $0.25 | $0.025 | $2.00 |
| `gpt-4.1-mini` | $0.40 | $0.10 | $1.60 |

At 10,000 TPM sustained for a whole month a deployment would pass ~43M tokens, so
continuous saturation of both is order-$100/month — but that requires literally
never going idle, and a demo does not. The realistic figure is dominated by how
many turns are actually taken.

### Verification

```
make verify-chat      # chat call against the deployed chat model
make verify-vision    # vision call against an image in blob storage
```

Both passed on 26 August 2026, authenticating with `DefaultAzureCredential` —
no keys:

```
lane        chat            lane        vision
deployment  gpt-5-mini      deployment  gpt-4.1-mini
served by   gpt-5-mini-     served by   gpt-4.1-mini-
            2025-08-07                  2025-04-14
tokens      24 in / 12 out  tokens      990 in / 8 out
```

The vision check uploads a generated four-quadrant colour card to the `uploads`
container, reads it back **through Entra** — shared keys are disabled on that
account, so a private blob cannot simply be handed to the model as a URL, it has
to be fetched and inlined — and fails unless the model names all four colours.
The uploaded PNG is deleted by the container's own 24–48 hour lifecycle rule.

Subscription Owner does **not** imply data-plane access to a Foundry account:
Owner carries `*` in `actions` and nothing in `dataActions`, and inference is a
data action. Terraform now grants the developer *Cognitive Services User* and
*Cognitive Services OpenAI User* on the account, and *Storage Blob Data
Contributor* on the data account. Without those, `az login` credentials get a 401
from the model endpoint while every management call keeps working.

### Image size is load-bearing for the photo lane

Not asked for, found on the way, and it changes
[#53](https://github.com/gganssle/chip_chat/issues/53). The same four-quadrant
image against `gpt-4.1-mini` at three sizes:

| Size | Image tokens | Colours returned (image is green, purple, orange, blue) |
| --- | --- | --- |
| 256px | ~100 | `orange, blue, yellow, black` — wrong colours entirely |
| 512px | ~446 | right four colours, top row transposed |
| 768px | ~965 | exactly right |

Azure downsamples aggressively. Below roughly 512px this model stops resolving
which region holds which colour, and then stops resolving the colours at all.
The photo lane's question — burrito or bowl — is spatial, about the contents of a
container, so **uploads must not be thumbnailed for economy**. The cost of not
doing so is on the table above: roughly 10x the image tokens from 256 to 768.

---

## Thread retention (issue #11)

`docs/decisions/foundry-agent-shape.md` settled state ownership and left one
empirical question, routed here: **how long Microsoft-managed thread storage
retains a thread, and whether a thread can be retrieved by id after an arbitrary
gap between visits.** It matters because
[#9](https://github.com/gganssle/chip_chat/issues/9) made visitor state durable —
the app stores a `thread_id` and a returning visitor is meant to resume.

**What was established on 26 August 2026:**

1. **Threads are id-addressable from a cold client.** A thread created in one
   process, with a message written into it, was read back — thread and message —
   from a separate process with a separately acquired token. Nothing about
   retrieval depends on session continuity.
2. **The service expresses no expiry.** A thread object is exactly five fields:

   ```json
   { "id": "thread_…", "object": "thread", "created_at": 1787722871,
     "metadata": {}, "tool_resources": {} }
   ```

   There is no `expires_at`, no TTL, no retention field — checked across
   api-versions `v1`, `2025-05-01` and `2025-11-15-preview`, which answer
   identically. There is no thread-retention setting on the Foundry account
   either (`az cognitiveservices account show` exposes none).
3. `docs/service-inventory.md` establishes the ceiling — 100,000 messages per
   thread — and still states no retention period.

**What was NOT established, plainly: the retention period itself.** "Survives an
arbitrary gap" cannot be demonstrated in an afternoon, and asserting it from the
absence of an expiry field would be inference dressed as measurement. The absence
is genuine evidence — a service that expired threads on a schedule would have
little reason to hide it, and more to the point, **a retention period the API
declines to express is one the app cannot code against**: nothing would let it
pre-emptively migrate a thread that is about to lapse.

So the instrument ships instead of a claim. `chip_chat.agent.threads` creates a
probe thread and fetches one back by id:

```
uv run python -m chip_chat.agent.threads create
uv run python -m chip_chat.agent.threads fetch <thread_id>
```

**Baseline probe thread, created 26 August 2026:
`thread_aZOWnxHgCwhx7WNj9lcmvUN3`** (`created_at` 1787722871, one message).
Re-run `fetch` on it after a week, a month, a quarter. The first run that fails
is the retention answer; the last that succeeds is the lower bound.

**#11 should stay open** until a `fetch` against that id has been run after a
real gap. What #8 can contribute is the baseline and the tool, and the news is
good so far: nothing observed suggests threads expire, and if they do, the
fallback is the cheap one the decision document already describes — message
history moves into the app's own durable store, which #9 built anyway.
