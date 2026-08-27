# Decision: Phoenix through development, Arize AX when public traffic justifies it

**Issue:** [#13](https://github.com/gganssle/chip_chat/issues/13) · **Path fixed by:** RFC-001 D6 · **Verified:** 25 August 2026 · **Switch not yet made:** [#78](https://github.com/gganssle/chip_chat/issues/78)
**Reads with:** `docs/local-tracing.md` (the development loop), `otel/README.md` (the span schema)
**Status:** the issue is still open, deliberately, and the last section says why

---

The path was never in question — RFC-001 D6 fixes it: **Phoenix self-hosted
during development, the exporter repointed at Arize AX for the public phase.**
Tracing an agent while you build it is how you debug it, and paying for that is
silly. What #13 asked was to confirm the switch *cost*, and that is what this
record carries.

## What "switching is a config change" concretely means here

It means two environment variables, and it is a structural claim rather than an
aspiration.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006          # Phoenix, local
OTEL_EXPORTER_OTLP_HEADERS=

OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.arize.com/v1      # Arize AX
OTEL_EXPORTER_OTLP_HEADERS=api_key=…,space_id=…
```

**Nothing in this repository imports Phoenix, names Phoenix, or branches on
whether Phoenix is what is listening.** `otel/exporters.py` has one OTLP slot.
`otel/tests/test_export_configuration.py` is what makes that a property rather
than a claim, and `docs/local-tracing.md` §"Swapping the backend" is the prose.
The `compose.yaml` service pins `arizephoenix/phoenix:version-20.3.0` and mounts
no volume, so its database is deliberately ephemeral — a local trace store you
can throw away is the point.

**Datasets are a separate door, on purpose.** `ARIZE_SPACE_ID` and
`ARIZE_API_KEY` are read only by `make dataset-upload`. They are not the span
path, and conflating them is the easy mistake.

**One thing that is not a config change**, and it is the sharpest finding under
this ticket: a *hosted* Foundry agent's exporter environment is immutable per
agent version, so repointing the agent half of a trace means cutting a new agent
version rather than editing a setting. RFC-001 §09 records it; the app half is
still two variables.

## Cost at demo volume

| | |
| --- | --- |
| **Phoenix self-hosted** | $0 plus a container. No feature gating |
| **AX Free** | **$0** — 25,000 spans/month, 1 GB/month, 15-day retention |
| AX Pro | $50/month — 50,000 spans/month, 10 GB/month, 30-day retention |

**Observed volume: 641 dependency spans and 78 exception records in one day** of
Phase 8 and Phase 10 verification against the deployed app; `docs/deployment.md`
records 560 spans in the three hours one verification pass took. Against AX
Free's 25,000 a month that is about **2.5% of the allowance in a busy day**, so
roughly thirty-nine such days fit.

**AX Free covers this demo.** The public phase can run on it without paying, and
the 15-day retention is the binding constraint rather than the span count — long
enough to investigate an incident, not long enough to be an archive. That is the
number to add to the cost model, and `docs/cost.md` §12 carries it flagged as a
projection from a span count rather than a bill, because nothing has ever been
sent to Arize.

## What was confirmed, and what was not

**Confirmed 2026-08-25** (evidence in `docs/service-inventory.md`): the AX tier
structure and quotas above; that Phoenix self-hosts free with no feature gating;
that both speak OpenInference, so the same instrumentation targets both
unchanged.

**Not confirmed, and the ticket's own wording asked for it:** whether the AX
Azure Marketplace listing is genuinely *transactable against this subscription*.
It has not been tested because AX Free needs no transaction, and the session
authority for this project excludes Marketplace purchases. If the demo ever needs
Pro, that question is unanswered.

**Not confirmed either:** whether Foundry's own tracing export and these
OpenInference spans coexist cleanly or duplicate. The deployed app exports only
to Application Insights, so the two have never run side by side.

## Why this issue is still open

Because the acceptance criterion is not "the path is chosen", it is *"exporter
endpoint and headers confirmed as environment configuration"* **and** the switch
demonstrated. `var.otlp_endpoint` in the Terraform is `""` today; the deployed app
sends spans to Application Insights and nowhere else. [#78] is the ticket that
has to prove the switch was a config change and nothing else, and until it runs,
this record documents a path that has been argued and instrumented but not
walked.

That distinction is the whole reason the record says so rather than closing on
the strength of a test that passes locally.
