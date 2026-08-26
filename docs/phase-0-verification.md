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
