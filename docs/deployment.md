# Deploying the chat app, and what the story actually turned out to be

Written 2026-08-26, from doing it rather than from reading about it. Issue #16
asks for exactly this and says why:

> The point is not the feature. The point is that it forces you through every
> platform's authentication and deployment story while the scope is still small
> enough to debug. […] write down anything that surprised you, because that is
> the deliverable that makes this issue worth doing early.

Ten things surprised me. They are in section 3. Sections 1 and 2 are the
procedure, so that the next deploy is not also an investigation.

---

## 1. The shape

```
  uv workspace ──▶ Dockerfile ──▶ ACR ──▶ Container App ──▶ default FQDN
   (--package)      linux/amd64   AcrPull    one replica     managed cert
                                  by identity
```

Nothing authenticates with a password anywhere in that line. The developer
pushes with their own Entra token (`az acr login`), the app pulls as
`id-chip-chat-app`, and the same identity is what reaches Foundry, Key Vault and
blob storage. There is no registry credential to rotate and none in the
Terraform.

The live URL is

<https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io>

on the Container Apps default domain with its automatic managed certificate.
Issue #4 was closed without buying a domain, so there is no DNS zone and no
certificate resource in the stack — which also means the hostname changes if the
app is ever recreated. Read it from `terraform output web_url`, never from here.

**Issue #71's title asks for "the custom domain with a managed certificate", and
the custom-domain half of it is satisfied by a decision rather than by work.**
#4 was closed without buying one, and that is the settled state, not an
omission: the app is on the Container Apps default FQDN, and that FQDN already
carries a managed certificate that Azure provisions and renews without a
`azurerm_container_app_custom_domain` resource, a DNS zone or a TXT record to
verify. Verified 27 August 2026 — the TLS handshake presents
`CN=whitesea-eea6e4c0.eastus2.azurecontainerapps.io`, issued by Microsoft TLS G2
RSA CA OCSP 04, valid to 21 February 2027. So *"a managed certificate"* is true
today and *"the custom domain"* is a purchase nobody made. Everything else in
#71 — CI deploy, cold start, rollback, scale-to-one, telemetry — is below.

## 2. The procedure

```bash
make infra-apply                 # ACR, the AcrPull grant, the app's settings
make image                       # build for linux/amd64
make image-push                  # az acr login && docker push
make deploy                      # az containerapp update --image
make deploy-check                # poll until the NEW revision serves
```

`make deploy` is `az containerapp update`, not Terraform, and that is
deliberate: `compute.tf` has `ignore_changes = [template[0].container[0].image]`
so that the next `terraform apply` does not drag the app back to the quickstart
placeholder. Terraform owns the *estate*; a deploy owns the *image*.

## 3. What surprised me

### 3.1 `terraform plan` does not run at all without a Databricks login

The Databricks provider fails to configure without `databricks auth login`, and
a provider that fails to configure fails **the whole plan** — including the
resources that have nothing to do with Databricks:

```
Error: cannot read cluster policy: failed to validate workspace_id:
  default auth: cannot configure default credentials
```

So on a machine that is authenticated to Azure but not to Databricks, there is
no such thing as a partial plan. Everything in this issue was applied with
`-target`, which Terraform correctly complains is not for routine use. It is
worth knowing that "everything in Terraform" now carries a second
authentication prerequisite that has nothing to do with the resource you are
changing.

### 3.2 A subscription only has the resource providers somebody registered

Creating the registry failed with a 409:

```
MissingSubscriptionRegistration: The subscription is not registered to use
namespace 'Microsoft.ContainerRegistry'.
```

That reads like a permissions problem and is not one. The azurerm provider no
longer registers everything automatically; `az provider register --namespace
Microsoft.ContainerRegistry` fixes it in about a minute. `providers.tf` now
names it in `resource_providers_to_register` so the next subscription does not
rediscover this.

### 3.3 `provisioningState: Succeeded` does not mean "deployed"

This is the one that cost the most time. `az containerapp update` returns
`Succeeded`, the new revision reports `Healthy` and `Running`, its traffic weight
is 100 — and the URL still serves the **old** revision for another 30–60
seconds. When the old revision is Azure's quickstart placeholder, what you see is

```
$ curl https://…/healthz
404 page not found
$ curl https://…/
<title>Azure Container Apps</title>   ← the default service page
```

which looks exactly like an image that failed to start. It is not. The field
that actually moves is `latestReadyRevisionName`; until it equals
`latestRevisionName`, you are not deployed.

`make deploy-check` waits for exactly that, *and then* for the app's own
`/healthz`. Polling `/healthz` alone is not enough and it is worth saying why,
because it is the obvious thing to write: on the very first deploy the old
revision is Azure's placeholder and 404s, so `/healthz` happens to be a good
signal — and on every deploy after that the old revision serves `/healthz`
perfectly well, so it returns 200 immediately and tells you nothing.

### 3.4 The app never sees HTTPS, so `Secure` cookies need the forwarded scheme

Container Apps terminates TLS at ingress and forwards plain HTTP. A `Secure`
cookie decided from `request.url.scheme` would therefore never be set in
production — on a site that is entirely HTTPS. `X-Forwarded-Proto` is the answer,
and `_is_https` in `chip_chat.api.app` is the four lines that got this right on
purpose after getting it wrong by accident: the local test suite ran over
`http://testserver`, silently dropped the cookie, and every request minted a new
session. The bug looked like an order confirmation problem.

### 3.5 With one trusted proxy, the *last* `X-Forwarded-For` entry is the client

The proxy appends. Everything to the left of its entry is whatever the client
chose to claim. Taking `forwarded.split(",")[0]` — which is the form you see
most often — would let a caller re-roll its rate-limit bucket on every request by
sending a different header, which is precisely the opposite of what a
per-source rate limit is for.

### 3.6 Port 80 costs you root

An unprivileged container cannot bind below 1024 without a capability. The image
runs as uid 10001, so the app listens on 8000 and `var.web_target_port` is 8000.
Ingress is 443 either way; the visitor never sees the difference.

### 3.7 The demo's spend controls are one replica wide

The spend cap's counters are process-local (`api/README.md`). Two replicas are
two ledgers, so a daily ceiling of 2,000,000 tokens would mean 4,000,000 and the
per-session cap would apply to whichever replica happened to answer. That is why
`web_max_replicas` is **1** and the container runs **one** uvicorn worker — and
why raising either is a change to `BudgetLedger` first, not a scaling decision.

### 3.8 The kill switch works, and costs a revision

`CHIP_CHAT_KILL_SWITCH=on` as an application setting does exactly what
`api/README.md` promises: the entry page becomes the stop state, a turn returns
the stop-state message with HTTP 200, and no model is called. Measured end to
end on the deployed app.

What the runbook does not say is that changing an application setting creates a
new revision, and §3.3 applies — it took about 40 seconds from `Succeeded` to
the stop state actually being what the URL served. "A minute from a phone" holds,
but only just, and the old revision keeps answering for that window.

The runbook's second route, `touch /mnt/ops/stop`, is **not available today**:
no file share is mounted on the Container App. `FileKillSwitch` treats an
unreadable path as not thrown, so it costs nothing and blocks nothing — but the
honest statement is that there is one working kill switch, not three, and it is
the one that restarts the container.

### 3.9 Ingress sheds load before the app's rate limiter sees it

Twenty-five simultaneous requests against the deployed app produced twenty
`200`s and five `503`s — and the `503`s came from Container Apps ingress, not
from the app. With `max_replicas = 1` and an HTTP scale rule of twenty
concurrent requests, there is nowhere for the twenty-first to go.

This is not harmful — a request the app never receives costs no tokens, so the
platform is a cruder spend control sitting in front of the careful one. But two
things follow, and neither is what the design assumed:

- The **per-source rate limit was never observed firing in production.** It is
  unit-tested and concurrency-tested (`api/tests/test_source_ratelimit.py`,
  `test_concurrency.py`), and the burst that should have tripped it was shed
  upstream instead. Sequential requests do not trip it either, because the model
  takes two to three seconds and twenty of those do not fit in a sixty-second
  window. It is real, and it is waiting for a caller fast enough to reach it.
- **A burst does not produce the stop state.** It produces the platform's 503.
  The designed "Cilantro's had a busy day" copy is what a visitor sees when a
  *ceiling* refuses them, not when the front door is full.

### 3.10 The bug only the deployment could find

The first deployed run refused to place a confirmed order. Every unit test
passed. The model was calling `place_order`, the desk was correctly refusing it,
and the trace said `confirmation_state=rejected` — all working as designed.

What was missing was that **the model cannot see the Confirm button.** The press
arrives at the server, marks the draft, and the model is told nothing about it;
having been refused once, it goes on politely refusing forever. The fix is
`CONFIRMATION_NOTE` in `chip_chat.agent.loop` — a server-written note added to
the conversation only when the desk really did confirm something. It is a hint,
not the enforcement: a visitor who posts somebody else's draft id gets no note,
and `place_order` refuses them either way.

No test would have found this, because every test scripted the model's next move.
It took a real model, on the real URL, being genuinely unconvinced.

### 3.11 Default health probes cannot tell "starting" from "dead"

The Phase 8 deploy went out and the revision never became ready. It looked
healthy for about thirty-five seconds after each start, then stopped answering
`/healthz`, and Container Apps restarted it — every ninety seconds, for twenty
minutes. From outside, every `POST` hung for exactly sixty seconds and died in
curl's HTTP/2 framing layer; the container's own access log showed the request
being answered `200 OK` moments before the process was killed.

Two separate faults, and the deploy needed both fixed.

**The app was holding its own event loop.** `POST /api/chat` is an `async def`
handler and `_run_turn` is several seconds of blocking work — a model call and
whatever the tools do. `_run_turn`'s docstring says it is synchronous *on
purpose*, on the strength of FastAPI running a `def` handler's work in a thread
pool. That reasoning is correct and this handler is not a `def`. So a turn held
the only loop the process has, `/healthz` went unanswered for the length of it,
and the platform did the one thing a liveness probe can do about a process that
has stopped answering: it restarted it, mid-conversation. The fix is
`run_in_threadpool` in the handler; the streaming branch never had the problem,
because Starlette iterates a synchronous generator off the loop already.

**And Azure client construction was on the start-up path.** Assembling the photo
intake in `build_service` constructed two Azure SDK clients and a
`DefaultAzureCredential` before uvicorn started serving. It is now built on the
first upload and memoised. Nothing that talks to Azure belongs in the start-up
of a process that scales from zero and has a one-second liveness probe pointed
at it.

**The probes themselves were the amplifier.** Container Apps' defaults are a
one-second timeout with no initial delay, and a cold Python process on a
fraction of a vCPU cannot answer anything in the first second of its life — so
the platform opens a restart loop against an application that is merely
starting. `compute.tf` now sets them explicitly: ten seconds of grace, a
five-second timeout, three consecutive failures before liveness concludes the
process is gone. Readiness is deliberately more patient than liveness, because a
revision that is still importing should be *not ready*, which is correct, and
should not also be *restarted*, which is not.

The general lesson is worth the space. **A liveness probe is a statement about
what "alive" means, and the default statement is "answers HTTP within one
second, from the instant the process exists".** For an app that starts cold and
occasionally blocks, that statement is false, and the platform enforces false
statements enthusiastically. `.github/workflows/deploy.yml` now starts the image
and curls `/healthz` before it is allowed anywhere near the Container App, so
the next version of this is a red CI run rather than a restart loop in
production.

## 4. What it costs

| Thing | Charge |
| --- | --- |
| Container Registry, Basic | ~$5/month, standing — the only non-pay-per-use charge this adds |
| Container App | Per vCPU-second while a replica runs; `min_replicas = 0`, so idle is ~free |
| Model calls | Pay-per-token, GlobalStandard, no ceiling of their own — which is the spend cap's whole reason to exist |
| Log Analytics | Capped at 1 GB/day by `var.log_daily_quota_gb` |

The registry is the only new standing cost and it is a variable
(`var.container_registry_sku`) rather than a literal, because a standing charge
should be visible in a diff.

## 5. What is deployed but not shared

**The URL is live. It has not been given to anybody.** Issue #16 is explicit that
those are different things: *"Deploying is fine; publicising is not."*

The inline spend cap (`cc-fv1`) has landed **and is wired into the request
path** — see `api/README.md`, "Not callable — unconstructable-without". Every
turn passes a synchronous budget check before a model is reached, and there is
no route through the app that can skip it. Verified against the deployed URL by
throwing the kill switch and watching the stop state come back.

Two things are still true and worth saying plainly before anybody shares a link:

1. **The $150 Azure budget only alerts.** It prevents nothing, and `cc-05h`
   records that the test notification was never sent, so it has not been observed
   to fire end to end.
2. **Both model deployments are GlobalStandard**, which is pay-per-token with no
   ceiling of its own.

So the only thing bounding spend on that hostname is the code in `api/`. That is
what it was written for, and it is now genuinely in front of every request — but
it is worth knowing that it is the *only* thing there.

Before the link is shared, `#85` should trip the ceiling against the real
deployment. `SpendLimits.from_env` and `var.spend_caps` are how that is done
without a code change.

## 6. The numbers, measured

Issue #71 asks for the cold start to be *measured and recorded — the visitor-
visible number, not the container's*, and PRD §05 asks for turn latency from
Application Insights. Both were taken against
<https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io> on
27 August 2026, on revision `0000013`, from a laptop in the United States over
domestic broadband — which is deliberately the wrong place to measure from if
you want a flattering number and the right place if you want the visitor's.

### 6.1 What the app tier costs

| What | Measured |
| --- | --- |
| `GET /` warm — the whole page, one request | ~0.19 s |
| `POST /api/entry` warm — name to persona to conversation | **0.20 s** |
| `POST /api/switch` warm — release, reassign, restart | **0.17 s** |
| `guard.budget_check` span, median over 88 | **0 ms** |

The two that are acceptance criteria are the entry and the switch, and both are
asked to be *under two seconds*. They are two hundred milliseconds, and the
reason is structural rather than lucky: neither route touches a model, neither
touches a database, and the roster they choose from is already in memory. The
whole of #66's *"name → persona → conversation in one screen, under two
seconds"* is one JSON round trip against an in-process store, because the second
screen was already loaded before the visitor typed.

### 6.2 The cold start

`min_replicas = 0`, so a visitor who opens the link when nobody else has
recently pays for a container starting from nothing.

**The container's contribution is 2.1–2.7 seconds**, and that number is not
inferred — it is the interval between two lines the platform and the app each
write for themselves, taken from Log Analytics on 27 August 2026:

| Revision | `ContainerStarted` | `Uvicorn running` | Start |
| --- | --- | --- | --- |
| `0000014` | 20:02:51.90 | 20:02:54.61 | **2.71 s** |
| `0000015` | 20:05:07.83 | 20:05:09.98 | **2.15 s** |

On top of that a node that has never seen the image pays to fetch it — measured
at **2.46 s and 2.48 s** for the 86.9 MB image, twice, which is the number the
two-stage Dockerfile buys — and a *new revision* pays about **13 s** of platform
scheduling between `ContainerCreated` and the first `ContainerStarted`. A
visitor waking a scaled-to-zero replica of an existing revision pays neither of
those in full: the image is already on the node, and the replica is scheduled
rather than provisioned.

So the visitor-visible cold start is **the app's 2–3 seconds plus the
platform's scheduling**, and on the requests that were served against a revision
reporting zero replicas the whole thing came back in **0.19 s** — which is
faster than the container can possibly start, and is the platform answering from
a sandbox it had kept warm. Both are true and the honest way to quote it is as a
range: **a couple of hundred milliseconds when Container Apps has a warm sandbox
to hand, and about three seconds when it does not.**

**What could not be measured, and why it is said rather than smoothed over.**
The app did not scale to zero on demand during the measurement window — the
container that came up at 19:45 was still the same process half an hour later,
with no `Finished server process` between — because something kept sending it
traffic (several evaluation suites were running against this deployment at the
time; the same contention shows up as 429s in §6.3). So the end-to-end
"scaled-to-zero visitor waits *n* seconds" figure is a decomposition here rather
than a single stopwatch reading. The decomposition is measured; the sum is
arithmetic.

`make scale-one` removes it entirely while you are actively sharing the link;
`make scale-zero` gives it back. Section 7.1.

### 6.3 Turn latency, and why the number is what it is

**Measured, from `chat.turn` spans in Application Insights, 69 turns:**

| Metric | PRD §05 target | Measured |
| --- | --- | --- |
| Median turn latency | < 2 s | **34.2 s** |
| 95th percentile | < 4 s | **62.7 s** |

That is not a near miss, and it is worth saying exactly where it goes before
anybody tries to optimise the app tier:

| Span | n | Median | p95 |
| --- | --- | --- | --- |
| `chat.turn` | 69 | 34.2 s | 62.7 s |
| `agent.step` | 123 | 20.2 s | 39.2 s |
| `llm.completion` | 123 | 20.2 s | 39.2 s |
| `guard.budget_check` | 88 | 0 ms | 0 ms |
| `retriever.search` | 9 | 0 ms | 0 ms |

`agent.step` and `llm.completion` are the same number to three digits, which
means **the entire turn is the model call**, twice over — a turn is typically
two round trips, one to choose a tool and one to answer. Everything this
repository's app tier contributes is inside the rounding.

Two things are inflating the model call, and they should not be conflated.

**The deployment was rate limited while these were taken.** Ten consecutive
turns came back with the *"Something went wrong on my side just then"* copy, and
the container's log gives the reason without ambiguity:

```
openai.RateLimitError: Error code: 429 - {'error': {'message': 'Your requests to
gpt-5-mini for gpt-5-mini in eastus2 have exceeded rate limit.', ...
```

`gpt-5-mini` is one shared GlobalStandard deployment and several evaluation
suites were running against it at the same time. The OpenAI client retries a 429
with backoff, so a turn that eventually fails still bills thirty to sixty
seconds of wall clock to `llm.completion`. **These percentiles are therefore an
upper bound taken under contention, not a clean baseline.**

**And `gpt-5-mini` is a reasoning model.** Even the turns that succeeded ran a
median of 30.8 s. A reasoning model on the hot path of a conversational product
is a product decision, not a tuning problem, and it is the first thing to
revisit if the target matters.

**On the target itself.** Issue #104 decided that PRD §05's `< 2 s` / `< 4 s`
must be re-baselined, because they were set against a co-located, natively
supported serving layer and Snowflake is now on AWS `us-east-2` with cross-region
inference. **That re-baseline has not happened** — §05 still reads `< 2 s` and
`< 4 s` — so the numbers above are reported raw, against the stale target, and
nothing here should be read as a pass or a fail until #104's second bullet is
done. What the measurement *does* settle is where the time is not: it is not in
the app tier, not in the budget check, and not in retrieval.

### 6.4 Telemetry is arriving

Application Insights has the full span tree from the deployed app, under
`cloud_RoleName = chip-chat.chip-chat-api`:

```
chat.turn → agent.step → llm.completion
                      ↳ tool.get_points_balance, tool.get_usual_order,
                        tool.search_menu_knowledge → retriever.search
          → guard.budget_check
          → render.response
```

560 spans in the three hours the Phase 8 verification took, latest at 19:56 UTC.
The connection string is a plain `env` entry on the Container App
(`APPLICATIONINSIGHTS_CONNECTION_STRING`, set from
`azurerm_application_insights.main.connection_string`), so there is nothing to
configure at deploy time and nothing to rotate.

## 7. The runbook

### 7.1 Scale to one, and back

One replica pinned means no cold start for anybody. It costs the active vCPU
rate continuously rather than in bursts, so it is for the hour you are actively
sharing the link and not for the week around it.

```bash
make scale-one     # az containerapp update --min-replicas 1
make scale-zero    # az containerapp update --min-replicas 0
```

### 7.2 Rollback, tested

**Revision mode is `Single`**, so rolling back is not a traffic shift — the
previous revision is deactivated the moment a new one is created. The way back
is to deploy the previous *image* again, and that only works because the tag is
a commit rather than `latest`:

```bash
make revisions                  # which image each revision references
make rollback TO=<short-sha>    # or TO=@sha256:<digest> for an untagged one
```

**Tested against the live app on 27 August 2026, not merely documented.** The
Phase 8 image was rolled out as `0000009`, failed its liveness probes, and was
rolled back to the previous image by digest:

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --image acrchipchat4cy39i.azurecr.io/chip-chat-web@sha256:70976272...
```

Revision `0000011` came up on the old image and served `GET /healthz` `200` in
1.70 s and `POST` in 0.17 s, which is how the fault was localised to the new
image rather than to the platform. Roll-forward was the same command with the
new tag. **Three to five minutes end to end**, most of it waiting for the new
revision to pass readiness.

The one thing to know under pressure: `provisioningState: Succeeded` comes back
long before the rollback is serving (§3.3). `make deploy-check` — which
`make rollback` runs for you — polls `latestReadyRevisionName` and the live
`/healthz` and is the only signal worth believing.

### 7.3 Takedown

Issue #70's posture: *if anyone at Chipotle ever asks for this to come down,
take it down cheerfully. Having built it is the point, not keeping it online.*
So this is one command, needs no build and no code change, and takes effect in
about a minute:

```bash
make takedown
```

which is

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --set-env-vars CHIP_CHAT_KILL_SWITCH=on --min-replicas 0 --max-replicas 0
```

Two independent things, deliberately. `CHIP_CHAT_KILL_SWITCH=on` is read on
every request by `chip_chat.api.killswitch` and turns every visitor into the
stop state, so the app is harmless even if a replica is still up; capping
replicas at zero then stops it answering at all. Neither deletes anything, so
putting it back is the same command with `off` and `--max-replicas 1`.

If the request is to remove the data as well: the harvested corpus is in the
`raw` container of the storage account and the catalogue is under
`catalog/chipotle/` in the same place. Nothing in the app serves either
(`api/tests/test_public_demo.py::test_no_endpoint_serves_a_bulk_export_of_the_corpus`),
so a takedown does not have to race an export.

### 7.4 Deploying from CI

`.github/workflows/deploy.yml` runs on every push to the default branch that
touches the image: build for `linux/amd64`, start the image and curl its own
`/healthz` and `/robots.txt`, push under the commit sha, roll the Container App,
poll until the new revision is the one serving, and finally fetch the live URL
and assert that the unaffiliated-demo disclosure and both halves of `noindex`
are still on it.

Like `agent-image.yml`, the deploy half is conditional on credentials existing,
so a clone with no Azure federation still gets the build gate. It needs
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`,
`CONTAINER_APP_NAME` and `RESOURCE_GROUP` as repository secrets, and the
federated identity needs `AcrPush` on the registry and
`Microsoft.App/containerApps/write` on the app.
