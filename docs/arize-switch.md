# Repointing the exporter at Arize AX

**Issue:** [#78](https://github.com/gganssle/chip_chat/issues/78) · **Written:** 27 August 2026
**Decision record:** [`decisions/observability-backend.md`](decisions/observability-backend.md)
**Depends on:** [`decisions/foundry-agent-shape.md`](decisions/foundry-agent-shape.md) (D8, and why the agent tier costs a version)

---

## Status, stated first because it is the thing to know

**Everything up to the purchase is done and verified. The purchase is not, and it is
the blocker.** Arize AX is not provisioned against this subscription: the resource
provider `ArizeAi.ObservabilityEval` is `NotRegistered`, there is no Arize resource in
`rg-chip-chat`, there is no Marketplace SaaS subscription, and the Foundry project
`proj-chip-chat` has **zero connections** — so the `otel-secrets` connection the agent's
version manifest references does not yet exist. Acquiring AX is a transaction and this
work was not authorised to make one. [The procedure is one page, at the bottom of this
document](#the-purchase-procedure-for-the-repository-owner).

What *is* established, and can be read without an AX account:

| Claim | Established by | Verdict |
| --- | --- | --- |
| No instrumentation code changes | `otel/tests/test_export_configuration.py`, and the empty diff below | **proved** |
| The switch is expressed entirely as configuration | the variable table and the version diff below | **proved** |
| The agent side is a new version whose only diff is those variables | `make agent-version`, rendered twice, diffed | **proved** |
| App Insights export is unchanged and working throughout | `test_both_backends_are_configured_from_one_instrumentation`, and the live container app's environment | **proved** |
| AX receives complete span trees identical in shape to Phoenix's | — | **blocked on the purchase** |
| AX cost at observed traffic added to the cost dashboard | the modelling is in the decision record; the observation is not | **blocked on the purchase** |

---

## The diff, split the way the ticket asks for it

### Instrumentation code: none

```console
$ git diff --stat -- otel/ agent/src/chip_chat/agent/
(no output)
```

Nothing under `otel/` or `agent/` needs to change to move backends, and that is not a
claim about this particular change — it is a property the tree is held to on every
build. `otel/tests/test_export_configuration.py::test_the_exporter_code_names_no_vendor`
parses `exporters.py`, `config.py` and `tracing.py` with `ast`, collects every identifier
and every runtime string literal, and fails if `phoenix` or `arize` appears in any of
them. Docstrings are excluded on purpose: prose explaining why the vendor is absent from
the code is not the vendor being present in it.

`build_span_exporters` is the whole of the fan-out, and it is deliberately dull:

```python
exporters: list[SpanExporter] = []
if config.otlp_endpoint:
    exporters.append(_otlp_exporter(config))
if config.azure_monitor_connection_string:
    exporters.append(_azure_monitor_exporter(config))
if config.console_export:
    exporters.append(_console_exporter(config))
```

There is no `if backend == …`, and moving from Phoenix to AX changes the value of
`config.otlp_endpoint`. That is the sentence D6 was bought for, and it survived contact.

### Configuration: two values per tier, and one connection

#### The FastAPI tier — an environment variable and a restart

```diff
 # infra/terraform/terraform.tfvars  (or -var on the command line)
-otlp_endpoint = ""
+otlp_endpoint = "https://otlp.arize.com/v1"
```

`infra/terraform/compute.tf` already carries this through: `var.otlp_endpoint`, when
non-empty, becomes `OTEL_EXPORTER_OTLP_ENDPOINT` in `local.web_env`, which is injected
into the Container App. `terraform apply` rolls a new revision.

**One gap, and #78 asks for it to be written down rather than quietly patched.** There
is no Terraform path for `OTEL_EXPORTER_OTLP_HEADERS`, and AX needs `api_key` and
`space_id` in it. `compute.tf` says in a comment that everything in the map is *"a name,
an endpoint or a ceiling; there is not a secret among them, which is why they are plain
`env` entries rather than Container Apps secrets"* — and that is still the right rule.
Closing the gap means one Container Apps secret backed by the Key Vault entry
`.env.example` already points at, plus a `secret_name` reference in the env block. It is
configuration work in `infra/`, it is about six lines, and it is **not instrumentation
work**: `chip_chat.otel.config` already reads the variable and needs nothing.

This is the honest finding the ticket invites. It does not make the abstraction thinner
than claimed — the instrumentation reads the header the moment something sets it — but
the *delivery* of one of the two values is unfinished, and saying so is more useful than
discovering it during the switch.

#### The hosted agent — a new agent version

Environment variables are immutable per agent version, so this is a deployment. Here is
the exact diff, produced by rendering the version manifest twice with everything else
held constant:

```console
$ OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix.internal:6006 \
  CHIP_CHAT_OTEL_CONNECTION=otel-secrets \
  CHIP_CHAT_ENVIRONMENT=production \
  CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT=gpt-5-mini \
  CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT=gpt-4.1-mini \
  make agent-version ACR_LOGIN_SERVER=acrchipchat4cy39i.azurecr.io > before.json

$ OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.arize.com/v1 \
  CHIP_CHAT_OTEL_CONNECTION=arize-ax \
  ... same as above ... > after.json

$ diff -u before.json after.json
     {
       "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
-      "value": "http://phoenix.internal:6006"
+      "value": "https://otlp.arize.com/v1"
     },
     {
       "name": "OTEL_EXPORTER_OTLP_HEADERS",
-      "value": "${{connections.otel-secrets.credentials.otlp_headers}}"
+      "value": "${{connections.arize-ax.credentials.otlp_headers}}"
     },
```

**Two lines.** Not two files, not two functions — two values in a JSON document that is
itself generated from the environment. `OTEL_EXPORTER_OTLP_PROTOCOL` stays
`http/protobuf`; AX speaks it, so the one variable that would have been a code change
(it is hardcoded in `chip_chat.agent.version` with no override) does not have to move.
That is worth recording as a near miss rather than as a success: if AX had wanted gRPC,
this section would have said *"and one code change, which is a finding."*

The manifest also refuses, structurally, the three mistakes that each cost a whole agent
version to correct — a literal value for `OTEL_EXPORTER_OTLP_HEADERS` rather than a
connection reference, a moving image tag, and an empty exporter endpoint.

#### The connection

One Foundry **CustomKeys** connection on `proj-chip-chat`, named `arize-ax`, holding a
single credential `otlp_headers` whose value is `api_key=<key>,space_id=<space>`. It
does not exist yet (`GET …/connections` returns `{"value": []}`) and there is no
Terraform resource for it. Creating it is part of the purchase procedure below.

### The agent version number

**Not yet allocated**, because registering a version requires the connection, which
requires the credentials, which require the purchase. What is fixed is the shape: the
new version carries an image referenced by `@sha256:` digest (the manifest refuses a
moving tag), and its only diff from the version before it is the two lines above.
`make agent-version` prints the manifest, `python -m chip_chat.agent.version register`
POSTs it, and the version number the service returns is what belongs in this paragraph
the day it is run. Foundry allows 1,000 agent revisions, so spending one on an endpoint
is cheap.

---

## App Insights, throughout

This is a fan-out, not a migration, and it is verified in three places.

**In code.** `build_span_exporters` appends the Azure Monitor exporter whenever
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set, independently of the OTLP slot.
`otel/tests/test_export_configuration.py::test_both_backends_are_configured_from_one_instrumentation`
asserts both exporters exist together;
`test_the_same_provider_feeds_every_configured_backend` asserts one tracer provider
feeds all three processors; and `test_spans_reach_both_backends_from_one_emission`
asserts that two independent exporters receive spans with **the same span ids** from one
emission — the acceptance criterion, as a test.

**In the estate.** `infra/terraform/compute.tf` wires
`azurerm_application_insights.main.connection_string` into the Container App
unconditionally, and the ops API gets it through `site_config`. The live container app's
environment carries `APPLICATIONINSIGHTS_CONNECTION_STRING` today. Adding
`var.otlp_endpoint` does not touch that entry.

**On the agent.** `APPLICATIONINSIGHTS_CONNECTION_STRING` is platform-injected by
Foundry and **cannot be overridden** — App Insights export can only be disabled at the
project level. So the agent's App Insights export is not merely unchanged by this
switch; it is not something this switch is able to change.

One thing the deployed app is behind on, worth knowing before anyone says *"the repoint
is one `terraform apply`"*: the live revision predates two Terraform additions
(`AZURE_SEARCH_INDEX_ALIAS` and `CHIP_CHAT_FOUNDRY_EMBEDDING_DEPLOYMENT`), so the next
apply will also add those. Read `terraform plan` before running it.

---

## Verifying the switch once AX exists

In this order, because each step makes the next one's failure legible.

1. `make dev` and `make trace`, unchanged, against Phoenix. Establishes the baseline
   tree: thirty-six spans across three turns, matching RFC-001 §09 node for node.
2. `OTEL_EXPORTER_OTLP_ENDPOINT=<ax> OTEL_EXPORTER_OTLP_HEADERS=<creds> make trace`.
   The same command, a different endpoint, no code. Open AX and compare the tree to
   Phoenix's. **They must be identical in shape** — same names, same nesting, same
   attributes. A tree that is missing `retriever.search` is a tree the grounding eval
   cannot score.
3. `make trace-boundary` against AX. This is the one that matters and the one most
   likely to fail: it sends a turn across the app/agent boundary and the trace must
   arrive as **one** trace carrying two service names. Two traces means propagation
   broke, every trajectory and grounding number becomes meaningless, and
   `eval/trajectory/BASELINE.md` and `eval/grounding/BASELINE.md` both gate on exactly
   this.
4. `make dataset-upload` with `ARIZE_SPACE_ID` and `ARIZE_API_KEY` set. Confirms
   datasets land as well as spans.
5. `python -m chip_chat.eval.online --check` with the AX credentials in the environment,
   then `--drill`. Confirms the monitors and the budget line are configured against the
   backend that will actually serve them.
6. Read span volume in AX for a day and put it beside the free tier's 25,000/month. That
   is the number the tier decision turns on; see the decision record.

---

## The purchase procedure, for the repository owner

One page. Everything before it is done; nothing after it can be done without it.

**Nobody but the repository owner should run this.** It creates a billing relationship.

1. **Register the resource provider.** It is `NotRegistered` today.

   ```bash
   az provider register --namespace ArizeAi.ObservabilityEval --wait
   az provider show --namespace ArizeAi.ObservabilityEval --query registrationState -o tsv
   ```

2. **Choose the tier, and choose it against the budget.** AX **Free** is \$0 — 25,000
   spans/month, 1 GB, 15-day retention. AX **Pro** is \$50/month — 50,000 spans/month,
   10 GB, 30-day retention. The subscription budget is \$150/month with its first alert
   at \$75 and an expected steady state of \$30–60. **Pro makes that alert fire
   permanently.** Start on Free. If span volume crosses ~20,000 in a month, buy Pro
   *and* raise `monthly_budget_usd`'s first threshold in the same commit, so the alert
   keeps meaning something.

3. **Provision it in the portal.** Arize is an Azure Native Integration: search
   *Arize AI* in Marketplace, choose the plan from step 2, and create the resource. Put
   it in `rg-chip-chat` if the region allows. **Check the region offering before you
   commit** — the provider's operation statuses are advertised in East US, East US 2
   EUAP and Central US EUAP, and the rest of the estate is East US 2 GA. A different
   region is acceptable (spans travel over HTTPS) but it should be a decision.

4. **Copy two values out of the AX space settings**: the **space id** and an **API
   key**, and the **OTLP endpoint** the space publishes.

5. **Put the credentials in Key Vault**, never in `.env` and never in Terraform:

   ```bash
   az keyvault secret set --vault-name kv-chip-chat-c8b63a \
     --name arize-otlp-headers \
     --value "api_key=<key>,space_id=<space>"
   ```

6. **Create the Foundry connection** the agent's manifest references — a **CustomKeys**
   connection on `proj-chip-chat` named `arize-ax`, with one credential key
   `otlp_headers` holding the same string. Portal: *Foundry project → Connections → New
   connection → Custom keys*.

7. **Hand back**: the OTLP endpoint, the connection name, and the Key Vault secret name.
   Everything after that is in this repository and needs no further authorisation —
   `terraform apply` with `otlp_endpoint` set for the app tier, and
   `make agent-image-push && python -m chip_chat.agent.version register` for the agent
   tier.

8. **Do not** delete the Phoenix container or the compose service. It stays for local
   development; see the decision record.

---

## References

RFC-001 D6, D8, §09. System design — Observability, *"Which Arize, though"*, Phase 9.
`docs/service-inventory.md` §2.1, §5, and items 4 and 5 of *What changed versus the
plan*. `docs/decisions/foundry-agent-shape.md` for the agent-version cost.
`docs/local-tracing.md` for the local half and the repoint table.
