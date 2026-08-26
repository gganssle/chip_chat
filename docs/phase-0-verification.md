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

**Region and deployment capacity for the model deployments** ([issue #8](https://github.com/gganssle/chip_chat/issues/8)'s
fourth acceptance criterion) are not recorded yet: the region is East US 2, but
no models are deployed. `var.model_deployments` is deliberately empty — #5 built
the environment, #8 picks the models and confirms in-region quota.
