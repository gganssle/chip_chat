# The runbook

Issue [#89] asks for the things that need doing quickly to be written down, and
for every procedure here to be **executable from a phone** — because that is
where you will be when you need it. It also asks that every procedure have been
**run at least once with its elapsed time recorded**, which is the difference
between a runbook and a wish.

Section 1 is the thing to read before anything else. Sections 2 through 9 are the
procedures, each with a timing and where it came from. Section 10 is triage.
Section 11 is what has not been run, and why, and what that costs you.

---

## 1. The one thing that stops this working from a phone

**Do not run `make` from a phone.** Every operations target in the Makefile
resolves its own arguments through Terraform:

```make
REGISTRY = $(shell $(TF_RUN) output -raw container_registry_login_server)
APP      = $(shell $(TF_RUN) output -raw container_app_name)
APP_URL  = $(shell $(TF_RUN) output -raw web_url)
RG       = $(shell $(TF_RUN) output -raw resource_group_name)
```

`terraform output` needs an initialised working directory with backend
credentials. In Azure Cloud Shell, in a fresh clone, or in any git worktree that
is not the one somebody ran `make infra-init` in, those shell-outs return **empty
strings**, and what you get is not an error you can read:

```
$ make revisions
ERROR: argument --name/-n: expected one argument
```

That was reproduced on 2026-08-27 in a worktree of this repository. It fails
*before* it reaches Azure, which is the good version; the bad version is a
command that takes an empty `-g` and does something to the wrong thing.

**So this runbook is written twice.** Every procedure gives the raw `az` or
`snow` command with the names spelled out, and then the `make` target beside it
for when you are at a laptop with the estate initialised. The raw form is the
one that works from a phone.

If you would rather use `make` and are somewhere without Terraform, you can pass
the two variables in — command-line variables override the Makefile's `=`
assignments:

```bash
make revisions APP=ca-chip-chat-web RG=rg-chip-chat        # 1.6 s, verified
```

### The constants, so you never have to look them up

| | |
| --- | --- |
| Live URL | `https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io` |
| Container app | `ca-chip-chat-web` |
| Resource group | `rg-chip-chat` |
| Subscription | `c8b63a71-218d-4d4c-991c-b963ed2fd1f0` ("Azure subscription 1") |
| Region | `eastus2` |
| Registry | `acrchipchat4cy39i.azurecr.io`, image `chip-chat-web`, tag = commit sha |
| Revision mode | **`Single`** — rollback is *redeploy the old image*, not a traffic shift |
| App Insights | `appi-chip-chat` |
| Snowflake | `snow` connection `chipchat` — `hq72718.us-east-2.aws`, org `LLMPCWE-GS74649` |
| Databricks | `dbw-chip-chat`, managed RG `rg-chip-chat-databricks-managed` |

Those are stable across a teardown. **Everything with a random suffix is not** —
`stchipchat4cy39i`, `srch-chip-chat-4cy39i`, `aif-chip-chat-4cy39i`,
`func-chip-chat-ops-4cy39i` and the registry all rotate if the estate is rebuilt.
From a phone, ask the account rather than a document:

```bash
az resource list -g rg-chip-chat -o table
```

---

## 2. Takedown — if anyone at Chipotle asks

This is the first procedure in the runbook because of [#70]'s posture: *if anyone
at Chipotle ever asks for this to come down, take it down cheerfully. Having
built it is the point, not keeping it online.* Five minutes, not a scramble — and
in practice about one.

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --set-env-vars CHIP_CHAT_KILL_SWITCH=on --min-replicas 0 --max-replicas 0 -o none
```

or, at a laptop, `make takedown`.

**Two independent things, deliberately.** `CHIP_CHAT_KILL_SWITCH=on` is read on
every request by `chip_chat.api.killswitch` and turns every visitor into the stop
state, so the app is harmless even if a replica is still up. Capping replicas at
zero then stops it answering at all. Neither deletes anything.

**Elapsed: about 40 seconds**, measured end to end on the deployed app —
`docs/deployment.md` §3.8. The reason it is not instant is that changing an
application setting **creates a new revision**, and `provisioningState:
Succeeded` comes back long before the URL serves it. The old revision keeps
answering for that window. "A minute from a phone" holds, but only just.

Confirm it took, rather than assuming:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io/
```

**Putting it back** is the same command with `off` and `--max-replicas 1`:

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --set-env-vars CHIP_CHAT_KILL_SWITCH=off --min-replicas 0 --max-replicas 1 -o none
```

**If the request is to remove the data too.** The harvested corpus is in the
`raw` container of `stchipchat4cy39i` and the catalogue is under
`catalog/chipotle/` in the same account. Nothing in the app serves either —
`api/tests/test_public_demo.py::test_no_endpoint_serves_a_bulk_export_of_the_corpus`
is the assertion — so a takedown does not have to race an export. Uploads expire
on their own (§8).

**One honest caveat.** The runbook used to promise three kill switches. There is
one. `touch /mnt/ops/stop` does not work, because no file share is mounted on the
Container App; `FileKillSwitch` treats an unreadable path as not-thrown, so it
costs nothing and blocks nothing. The working switch is the environment variable,
and it is the one that restarts the container.

---

## 3. The kill switch alone, without stopping the app

Same command, without touching replicas. Use this when the problem is what the
app is *saying* rather than that it is up — a bad prompt version, a lane
returning something wrong, an invoice-shaped surprise you want stopped while you
look.

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --set-env-vars CHIP_CHAT_KILL_SWITCH=on -o none
```

**Elapsed: ~40 seconds**, same measurement, same reason.

Two things worth knowing under pressure. `EnvironmentKillSwitch` sits behind a
**five-second cache**, so even once the revision is serving there is a few-second
tail. And the switch is **fail-closed on nonsense**: anything not recognisably
off — the falsey set is `"", 0, false, no, off, run` — counts as thrown. A typo
stops the app. That asymmetry is deliberate and it is the right one for a switch
you reach for from a phone.

---

## 4. Rollback to a previous revision

**Revision mode is `Single`.** The previous revision is deactivated the moment a
new one is created, so rolling back is not a traffic shift — it is deploying the
previous *image* again, which only works because the tag is a commit sha rather
than `latest`.

**Step one, find the image.** This is the step that did not work until today:

```bash
az containerapp revision list -n ca-chip-chat-web -g rg-chip-chat --all \
  --query "reverse(sort_by([].{created:properties.createdTime,name:name,active:properties.active,replicas:properties.replicas,image:properties.template.containers[0].image}, &created))" \
  -o table
```

`make revisions` was missing `--all`, and without it the CLI lists only *active*
revisions — which in Single mode is exactly one row: the one you are trying to
roll back **from**. `make rollback` tells you to run `make revisions` to find the
image, and until 2026-08-27 it could not tell you. Fixed in the Makefile;
recorded here because the raw command above is the one you will paste on a phone
and it needs the flag too.

**Elapsed: 1.6 s**, measured 2026-08-27. Output looked like:

```
Created                    Name                       Active  Replicas  Image
2026-08-27T21:15:54+00:00  ca-chip-chat-web--0000018  True    1         …/chip-chat-web:2727033
2026-08-27T20:20:17+00:00  ca-chip-chat-web--0000017  False   0         …/chip-chat-web:c1e92be
```

**Step two, deploy the old image.**

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --image acrchipchat4cy39i.azurecr.io/chip-chat-web:<short-sha> -o none
```

or by digest, when the revision you want has no tag any more:

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat \
  --image acrchipchat4cy39i.azurecr.io/chip-chat-web@sha256:<digest> -o none
```

At a laptop: `make rollback TO=<short-sha>` or `TO=@sha256:<digest>`, which runs
`make deploy-check` for you.

**Step three, believe only the health check.** `provisioningState: Succeeded`
comes back 30–60 seconds before the rollback is actually serving. The only signal
worth believing is the pair of revision names agreeing *and* `/healthz`
answering:

```bash
az containerapp show -n ca-chip-chat-web -g rg-chip-chat --query properties.latestRevisionName -o tsv
az containerapp show -n ca-chip-chat-web -g rg-chip-chat --query properties.latestReadyRevisionName -o tsv
curl -s -o /dev/null -w '%{http_code}\n' \
  https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io/healthz
```

**Elapsed: three to five minutes end to end**, most of it waiting for the new
revision to pass readiness. **Tested against the live app on 2026-08-27, not
merely documented**: the Phase 8 image rolled out as `0000009`, failed its
liveness probes, and was rolled back by digest; `0000011` came up on the old
image and served `GET /healthz` in 1.70 s and `POST` in 0.17 s, which is how the
fault was localised to the new image rather than to the platform. Roll-forward
was the same command with the new tag.

---

## 5. Scale to one, and back

One replica pinned means no cold start for anybody. It costs the active vCPU rate
continuously rather than in bursts, so it is for the **hour** you are actively
sharing the link and not for the week around it.

```bash
az containerapp update -n ca-chip-chat-web -g rg-chip-chat --min-replicas 1 -o none   # warm
az containerapp update -n ca-chip-chat-web -g rg-chip-chat --min-replicas 0 -o none   # back
```

or `make scale-one` / `make scale-zero`.

**What it saves you: about 3 seconds, at most.** Measured on this app —
container start from `ContainerStarted` to `Uvicorn running` was 2.71 s and
2.15 s on two revisions, the 86.9 MB image pulls in ~2.47 s, and a cold start
against a warm platform sandbox is 0.19 s. So the honest range is *200 ms if the
platform has a sandbox ready, ~3 s if it does not*, and pinning a replica buys
the difference. It is worth doing while somebody is watching you demo and not
otherwise.

**Not separately timed here**, and the reason is deliberate: each of these
commands creates a new revision, and re-running them today would have moved the
app off `0000018` — the revision every number in `docs/cost.md` and the README's
status section was measured against. The command is a one-line variant of §2's,
which was timed at ~40 s, and it takes the same path through the same platform.
That is an inference, and it is flagged as one.

---

## 6. Manual demo-data reset

For when a demo has gone sideways — a visitor has left an account in a state you
do not want the next person to walk into.

```bash
make snowflake-demo-reset-plan   # who would be aged out. Changes nothing.
make snowflake-demo-reset        # do it
```

These need a Python environment, so they are the one procedure in this document
that is **not phone-runnable**. From a phone the equivalent is to call the
procedure directly in Snowsight, which is what both targets do:
`CHIP_CHAT.ACCOUNTS.reset_demo_sessions`.

**It is safe to run while the app is live**, by construction rather than by
timing. It ages out *inactive* sessions only, so an active visitor is out of
scope; it takes one transaction per visitor; and it runs on the **publish**
warehouse, because the serving warehouse cancels anything over 60 seconds.

**What it touches.** Only rows a visitor *added*: orders, order items and loyalty
ledger entries at or above the live band (`ord-9000001`, `loy-9000001`), every
`action_receipts` row, and the columns edited on `demo_visitors`, restored
column-by-column from `demo_visitor_baseline`. The eighteen months of generated
history, numbered from `ord-0000001`, is **never touched**. That is why it is
safe.

**Measured, first live run 2026-08-27.** One dirty visitor aged out: 1 order,
1 order line, 1 ledger entry, 3 receipts deleted, one Foundry thread retired,
restored to 42 orders / 99 lines / 61 ledger entries / a 2,098-point balance. A
second session ran the read lane bound to `demo-0048` continuously across the
reset — 20 queries in the window, all returning the same count, no errors, no
lock waits. **No wall-clock duration was recorded for the reset itself**; its
cost is "negligible", one transaction per aged visitor.

It also runs nightly on its own, as `RESET_DEMO_SESSIONS_NIGHTLY` at 09:00 UTC,
two hours after the publish. If you are wondering whether it ran:

```sql
SELECT scheduled_time, state, error_message
  FROM TABLE(CHIP_CHAT.INFORMATION_SCHEMA.TASK_HISTORY(
       TASK_NAME => 'RESET_DEMO_SESSIONS_NIGHTLY'))
 ORDER BY scheduled_time DESC;
```

**What it does not do**, so you do not go looking: it does not delete Foundry
threads (it clears `thread_id` and returns the retired ids), it does not collect
in-memory order drafts (`api.drafts.DraftStore`, 900-second TTL — they age out on
their own), and it does not touch the catalogue or the gold marts.

---

## 7. Full teardown

Teardown being one command is half the reason everything went into Terraform.

```bash
terraform -chdir=infra/terraform destroy      # or: make infra-destroy
```

**Measured: 32 resources destroyed in 9 minutes 20 seconds**, on the scratch
stack. Verified to leave no residue: `az keyvault list-deleted` and
`az cognitiveservices account list-deleted` both came back empty afterwards,
which they only do because `providers.tf` sets purge-on-destroy flags — the
defaults would reserve the Key Vault name for 7 days, three Cognitive Services
accounts for 48 hours, and the Log Analytics workspace for 14 days.

**All or nothing.** `make infra-destroy` is safe;
`terraform destroy -target=…` is not, because the subscription budget and its
action group are *inside* the state file. If you tear down part of the estate and
leave the rest running, **recreate the budget first**.

**Two things stay behind, permanently and on purpose.** The subscription itself,
and the remote-state storage account `sttfstatec8b63a` in `rg-chip-chat-tfstate`.

### The pieces Terraform does not own

| | What teardown is |
| --- | --- |
| **Snowflake** | Entirely outside Terraform — checked-in SQL under `snowflake/sql/`. `make snowflake-rebuild` resets it, or let the trial lapse on **2026-09-24**. Note `DROP` is a soft delete: `UNDROP DATABASE CHIP_CHAT` works for **one day** on this account. |
| **Databricks** | The workspace *is* in Terraform, so `infra-destroy` takes it and its managed RG — which is where the $36.50/month of NAT gateway and public IP lives. Two things are account-admin only and manual: `var.databricks_account_id`, and the on-behalf-of token toggle. |
| **Arize** | No infrastructure at all. It is an OTLP endpoint and a header. Teardown is revoking the key and unsetting `OTEL_EXPORTER_OTLP_ENDPOINT`. Nothing is currently pointed at it. |
| **Domain** | **There is none.** [#4] was closed without buying one; the app is on the Container Apps default FQDN with its automatic managed certificate. There is no DNS zone to release. |

**A teardown leaves a zero Azure bill** for everything except the remote-state
storage account, which holds kilobytes. This has not been re-verified against a
whole-month invoice since the estate was rebuilt — the scratch-stack destroy
above is the evidence, and it is the strongest available until a month passes.

---

## 8. Verifying the two things nothing else watches

Both are read-only and safe against production. Neither is in `make ci` and
neither ever can be: one needs an Azure login and the other needs a Databricks
credential, and a gate that needs a logged-in human is not a gate.

### The uploads that are supposed to expire

The one check in this repository designed to be run twice a day apart:

```bash
make infra-check-uploads      # ./infra/scripts/check-uploads-retention.sh
```

It sets the subscription and finds the storage account itself, so it needs no
Terraform. Three checks: soft delete is off, the lifecycle rule exists and is
enabled and deletes, and then it prints every blob with its age. **The procedure
is: run it, note a name, run it again tomorrow, and see that the name is gone.**

The rule reads `daysAfterCreationGreaterThan: 1.0` on prefix `uploads/`,
verified in the deployed policy on 2026-08-27. **An expiry has not yet been
observed**, which is the only part of #88's blob line still open — and querying
`daysAfterModificationGreaterThan` will return `null` and look like drift. It is
not. Query the whole policy.

### The Databricks workspace, against this repository

Eight library modules under `/Shared/chip-chat/lib` and sixteen notebooks beside
them are Terraform-managed. On 2026-08-28 two of them were not what this
repository says they are, the nightly publish failed, and its error message
blamed the row access policy — see §10 and `docs/workspace-drift.md`. This is the
check that would have said so.

The raw form. It needs a Databricks credential and **nothing else** — no
Terraform state, no Azure login, no initialised working directory — so unlike
everything else in this runbook it really does run from a phone if the Databricks
CLI is on it:

```bash
databricks auth login --host https://adb-7405614862446074.14.azuredatabricks.net

# One path, which is what you want mid-incident. Exit 0 means identical.
databricks workspace export /Shared/chip-chat/lib/publish.py > /tmp/deployed.py
diff /tmp/deployed.py databricks/src/chip_chat/databricks/publish.py

# The other path that was stale on 2026-08-28.
databricks workspace export /Shared/chip-chat/snowflake_publish > /tmp/deployed.py
diff /tmp/deployed.py databricks/notebooks/snowflake_publish.py
```

All twenty-four, with the diffs, from a laptop:

```bash
make infra-check-databricks   # uv run python -m chip_chat.infra.workspace_drift
make infra-list-databricks    # just the paths — free, no credential
```

**Elapsed: 10.9 s**, twenty-four paths, measured 2026-08-28 against `main`
(`87a78fb`), which was clean. Quiet and exit 0 when the workspace matches; a
unified diff per drifted path and exit 1 when it does not; exit **2** when the
check could not be run at all, which is a different thing and is meant to be.
Those are the module's codes — `make` reports its own exit 2 for any failed
recipe, so call the module directly if you want to tell the two apart.

It compares against **your checkout**, not against `HEAD`, because your checkout
is what an apply would upload. So an uncommitted edit to a deployed file reports
as drift, correctly: you have a change that is not in the workspace.

The repair is an apply, which is what put those files there in the first place.
From a phone, or when Terraform state is not to hand, overwrite the one path:

```bash
databricks workspace import --overwrite --format SOURCE --language PYTHON \
  --file databricks/src/chip_chat/databricks/publish.py \
  /Shared/chip-chat/lib/publish.py
```

```bash
make infra-apply              # terraform -chdir=infra/terraform apply
```

That import is exactly what repaired the 2026-08-28 outage, and it is what
Terraform would have written. **Follow it with an apply when you are back at a
laptop** — an import leaves Terraform's state believing it wrote something else,
and the next unrelated apply will show the file as changed.

---

## 9. Rebuild from cold

This matters because the Snowflake trial expires **2026-09-24** and the day-30
plan, decided in advance rather than on the 24th, is **rebuild on demand**.

### Azure

```bash
make infra-init
make infra-apply
make image && make image-push && make deploy
```

`terraform plan` fails wholesale without a Databricks login — see
`docs/deployment.md` §3.1 — so log in to Databricks first or use `-target` and
say so. **A subscription only has the resource providers somebody registered**,
and registering one takes about a minute apiece.

### Snowflake

```bash
make snowflake-apply     # idempotent: creates and tightens, never destroys
make snowflake-load landing/catalog landing/accounts/synthetic
make snowflake-verify
make snowflake-cap QUOTA=<credits>   # section 11 of docs/cost.md has the arithmetic
```

**Roughly fifteen minutes and about a credit** for a fresh account.

### ⚠️ Do not run `make snowflake-rebuild` without the landing zone in your hand

This is the single most dangerous command in the repository and it has a mild
name.

`make snowflake-rebuild` runs `apply --reset --yes`, which drops `CHIP_CHAT` and
everything in it. It restores every *object* and **no rows at all**. The
synthetic population and the catalogue come back only from `make snowflake-load`
over a landing zone, and **that landing zone is not in this repository**. A
rebuild run without one drops the whole synthetic population and eighteen months
of generated history, and the gold marts computed against that generation end up
describing customers who no longer exist — which is not an error, it is four
tables of plausible numbers about nobody.

`UNDROP DATABASE CHIP_CHAT` buys you **one day** on this account. That is the
entire safety net.

**So the rebuild path is currently untested, and that is a deliberate choice
rather than an omission.** It was tested once, on an account with nothing in it —
`2:32.41` from empty to 25/25 checks passing, recorded in
`docs/snowflake-account.md` §3.4 — and it has not been re-run since the
population was loaded, because re-running it is exactly the destructive act
above. What was exercised instead on 2026-08-27 was `make snowflake-apply`, which
is idempotent and is the half of the claim that can be tested without a landing
zone to hand.

**This matters because the day-30 plan depends on it.** On 2026-08-27 no landing
zone directory existed on disk in this checkout, and the symptom was not an
error: `demo_visitor_baseline` had been created by an apply *after* the load,
nothing had filled it, and the nightly reset would have aged nobody out for as
long as nobody looked. It was recovered from the live `demo_visitors`, which was
faithful only because no visitor had ever written through the procedures. **That
is luck and it is not available twice.** The landing zone belongs in durable
storage before this trial ends; a generated population that exists only in one
agent's working directory and one Snowflake account is a population with no
copies.

### The search index

```bash
make search-status       # what the service holds and what the alias serves
make search-build        # rebuild from the live corpus release and swap
make search-rollback     # point the alias back at the index before this one
```

The alias swap is one write, so a search rollback is close to instant. Note the
Free tier keeps no spare rollback target you did not build.

---

## 10. Incident triage

### From "someone says it did something weird" to the trace

A bug report arrives with a **session id** at best. That is why
`chip_chat.otel.spans.TurnIdentity` is stamped on *every* span in a turn rather
than only on the root: Application Insights searches attributes far more
comfortably than it walks trace trees.

```bash
az monitor app-insights query --app appi-chip-chat -g rg-chip-chat --analytics-query \
  "dependencies
   | where customDimensions['session.id'] == '<SESSION-ID>'
   | project timestamp, name, duration, success,
             turn = customDimensions['chip_chat.turn.index'],
             persona = customDimensions['chip_chat.persona.id'],
             guard = customDimensions['chip_chat.guard.reason'],
             tokens = customDimensions['chip_chat.tokens.total']
   | order by timestamp asc" -o table
```

No such query existed anywhere in the repository before this document, and the
cold-start and latency numbers in `docs/deployment.md` were taken by hand from
the portal. This is the one-liner; it works, and it is what every query in
`docs/cost.md` is a variant of.

The span tree you should see, in order:

```
chat.turn
├── guard.budget_check            0 ms — the inline cap. If this is absent, the
│                                        turn did not go through SpendGate.
├── guard.content_safety          0 ms
├── agent.step
│   ├── llm.completion            the whole turn, basically
│   └── tool.<name> → retriever.search / db.cortex_analyst / ops.<action>
└── render.response
```

Locally the same session id goes in Phoenix's filter box at
<http://localhost:6006>; `make trace` prints the id it used.

**One trace, two service names.** A hosted agent forces `service.name` to the
agent's name and ignores `OTEL_SERVICE_NAME`, so anything filtering on
`service.name` must expect **both** values. `turn_service_names()` in
`otel/service.py` is the list.

### From "a lane is down" to which dependency broke

```bash
curl -s https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io/healthz/lanes
```

**Elapsed: 0.19 s**, HTTP 200, measured 2026-08-27. It calls no model, and it
binds a session first — so an operator curling it with no cookie does not see the
Snowflake-backed lanes falsely reported down.

Four states, not two: `ok`, `DOWN`, `not_wired`, and `unprobed` (the photo lane
only, because probing it would cost a vision completion). **`not_wired` with
`healthy: true` is the correct answer, not a contradiction** — it is what
revision `0000018` returns for all five lanes today.

This is deliberately **not** a liveness probe. A lane being down is a fact an
operator wants and not a reason to restart the container; `/healthz` is what
Container Apps probes and it is never gated by the cap or the rate limiter.

Then the blast radius, so you know what else to expect:

| What broke | What the visitor sees | Blast radius |
| --- | --- | --- |
| AI Search | knowledge lane declines and says why | knowledge only |
| **Snowflake** | account, personalization and action decline; menu questions still work | **three lanes** |
| Cortex Analyst timeout or low confidence | *"I can't answer that reliably"* — **never a fallback query** | one question |
| Vision model | asks the visitor to describe the meal in words | vision only |
| Ops API | the card renders and reports ordering unavailable; nothing half-written | writes only |
| Databricks job failed | stale marts served **with their `derived_at`** | freshness, not a lane |
| Daily budget exhausted | friendly stop state, **no model calls** | everything, gracefully |

Timeouts, when something is slow rather than down: Cortex Analyst 15 s; the
warehouse's own statement timeout 60 s on serving; the ops API two attempts then
the path is called down; ingress closes any response that has sent nothing for
**60.19 seconds** — measured ten times out of ten, to two decimals.

### When a nightly job fails and the message names a cause

Check that the job is running the code you think it is **before** you act on what
its error message says:

```bash
make infra-check-databricks   # 10.9 s, read-only, needs only a Databricks credential
```

This is here because of a specific incident and the specific shape of it is worth
carrying. On 2026-08-28 `chip-chat-publish` failed with *"holds 0 rows after the
swap"* and a message telling the reader to check whether a row access policy
filters `CHIP_CHAT_PUBLISH`. The message was well written, it named a real
failure mode, and that failure mode had genuinely caused this exact error once
before (`docs/nightly-publish.md` §7). It was still the wrong answer. The
deployed `/Shared/chip-chat/lib/publish.py` was 37 lines behind `main` and did
not contain the fix, so the job was running code whose error messages describe a
version of itself that had been replaced.

The general form: **a good diagnostic describes the code that emitted it, and
that is only useful if the deployed code is the code you are reading.** Acting on
that message without this check meant editing `VISITOR_ISOLATION` — the row
access policy the entire isolation guarantee rests on — to fix a problem that was
not there. `docs/workspace-drift.md` is the write-up.

### When a write is the question

Every write emits an `ops.<action>` span carrying `chip_chat.ops.reference_id`
and `chip_chat.ops.confirmation_state`, **even when refused**. The Functions host
rejoins the agent's tool span from the inbound `traceparent` and refuses the
write if it is not there: *a write nobody can find in a trace is a write this
service declines to make.* So "did that order go through?" is answerable from the
trace alone.

### When the app is up and answering badly

Check what it is running before you debug what it is doing:

```bash
az containerapp show -n ca-chip-chat-web -g rg-chip-chat \
  --query "{revision:properties.latestReadyRevisionName, image:properties.template.containers[0].image}" -o json
```

The image tag is a commit sha. And `chip_chat.prompt.version` on the root span
tells you which system prompt the turn ran under, which is the other half of "why
is it saying that".

---

## 11. What has not been run, and what that costs you

Kept as a list rather than buried, because #89's acceptance criterion is that
every procedure has been executed and three of these have not.

| Procedure | Status |
| --- | --- |
| Takedown / kill switch | ✅ ~40 s, measured on the deployed app |
| Rollback | ✅ 3–5 min, tested against the live app 2026-08-27 |
| `revisions` | ✅ 1.6 s, run 2026-08-27 (and fixed — §4) |
| Demo reset | ✅ first live run 2026-08-27; no wall clock recorded |
| Lane health probe | ✅ 0.19 s, run 2026-08-27 |
| `snowflake-verify` | ✅ **5 min 7 s**, run 2026-08-27, ~0.5 credits |
| `snowflake-cap` | ✅ **14.9 s**, run 2026-08-27 |
| Trace lookup by session id | ✅ run 2026-08-27; §10's query is the one that worked |
| Scale one / scale zero | ⚠️ **not timed** — see §5; it would have moved the app off `0000018` |
| Teardown → zero bill → rebuild | ⚠️ destroy measured at **9m20s / 32 resources** on the scratch stack; the *round trip* has not been done on the current estate |
| `snowflake-rebuild` | ❌ **not run, deliberately** — §9. Tested once on an empty account (`2:32.41`); running it now destroys the population |
| Blob expiry actually observed | ❌ configured, never watched — §8 |
| `infra-check-databricks` | ✅ **10.9 s**, run 2026-08-28; clean against `main`, and it caught an unapplied working-tree edit on the same run |
| `infra-check-databricks` against a genuinely stale workspace | ⚠️ **not exercised** — the `chip-rxs` drift was repaired before the check existed; only the uncommitted-edit case has been seen live. `docs/workspace-drift.md` §6 |
| Budget alert actually firing | ❌ nothing has crossed 50% of $150 |

**The rebuild is the one that matters.** The trial expires 2026-09-24, the plan
for that morning is "rebuild on demand", and the command that plan rests on has
not been exercised since there was anything to lose. Getting the landing zone
into durable storage turns an untested procedure into a testable one, and it is
the highest-value operational task outstanding.

---

## References

`docs/deployment.md` §3.8, §6, §7 — the deploy story, the measured cold start and
turn latency, and where the kill-switch and rollback timings come from.
`docs/demo-reset.md` — the reset, in full. `docs/snowflake-account.md` §3.4, §10
— the rebuild, the trial clock, and the landing zone. `docs/failure-isolation.md`
— the blast-radius table and the timeouts. `docs/workspace-drift.md` — why §8's
second check exists, how its list of twenty-four paths is derived from the
Terraform, and what it deliberately does not check. `docs/cost.md` — what any of
this costs, and §14 there is the guardrail audit. `infra/README.md` — the estate, the
identifiers, and what teardown does and does not remove. RFC-001 §11 — the
circuit breaker this is the operational half of.

[#4]: https://github.com/gganssle/chip_chat/issues/4
[#70]: https://github.com/gganssle/chip_chat/issues/70
[#89]: https://github.com/gganssle/chip_chat/issues/89
