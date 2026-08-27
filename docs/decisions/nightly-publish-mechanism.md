# Decision: the Snowflake connector, not Iceberg

**Issue:** [#12](https://github.com/gganssle/chip_chat/issues/12) (RFC Q1) · **Decided:** 25 August 2026, under delegated authority · **Reversible**
**Changes:** [#39](https://github.com/gganssle/chip_chat/issues/39) — implement the nightly publish against the connector
**Does not change:** D2. "Snowflake serves, Databricks computes" is a decision about the clock, not about the storage format

---

RFC-001 framed this honestly and it is worth quoting rather than paraphrasing:
*"The connector is the shorter path. Iceberg in ADLS read by both engines is the
more current pattern and avoids a copy, at the cost of more setup and more that
can go wrong on a deadline."*

## The decision

**The Snowflake connector.** Three things settle it for V0.

**The copy Iceberg avoids is negligible here.** The nightly publish carries gold
marts for 500 synthetic customers and eighteen months of orders — `customer_360`,
`usual_order`, `item_affinity`, `spend_summary` and seven more. Measured on
2026-08-27 that is **eleven tables, 108,157 rows, about 1.53 MiB compressed**,
moved once a night, entirely off the conversational hot path. Iceberg's principal
benefit is avoiding duplication at a scale this project does not have and, per
the RFC's non-goals, explicitly is not trying to reach.

**The risk lands on the wrong side of a deadline.** This is an
evenings-and-weekends build with a five-week honest estimate. "More that can go
wrong" is a cost paid in exactly the scarce resource, and it buys a property that
cannot be demonstrated at this size.

**It forecloses nothing.** Swapping the publish mechanism later changes one job,
not the architecture. Nothing downstream of the gold marts knows or cares how the
rows arrived.

## What we give up, stated rather than dressed up

Iceberg is the more current pattern, and being able to say we ran it would have
had demonstration value in its own right — **this project exists partly to
exercise these platforms**, so declining to exercise one of them is a real loss
rather than a nominal one. It is outweighed by the deadline risk. It should not
be recorded as though it were free.

## What the connector turned out to cost, measured

Written down here because the decision was made on an estimate and the estimate
can now be checked. Full derivation in `docs/nightly-publish.md` §6; the cost
argument is `docs/cost.md` §6.1.

| | measured 2026-08-27 |
| --- | --- |
| Job wall clock | 176.7 s, of which 51 s is a cluster starting |
| Warehouse-active seconds | 112.5 |
| Snowflake credits, billed | ≈0.066 — about $0.20 |
| DBUs + VM | ≈0.060 DBU + 0.049 cluster-hours — about $0.035 |
| **Per night** | **≈$0.235**, ~3% of the publish warehouse's daily quota |

Two things the decision did not anticipate and both are recorded rather than
regretted.

**The publish is now cross-cloud.** [#104] put the Snowflake account on AWS
us-east-2 while Databricks stayed in Azure East US 2, so every night this job
crosses a cloud boundary the RFC assumed it would not. It costs nothing extra:
Snowflake charges no ingress, Azure internet egress is free under 100 GB/month,
and the only meter that charges is the Databricks NAT gateway's `Standard Data
Processed` at $0.045/GB — **$0.0325 month to date, against $1.4478 for the same
gateway merely existing.** `docs/cost.md` §7.

**Snowflake bills the warehouse being awake, not the work.** The estimator sums
eleven per-table active spans and reports 0.0312 credits; the account is billed
≈0.066, because the warehouse sits through its sixty-second auto-suspend
afterwards. A publish that touched one table would cost nearly the same. Neither
number is wrong; they answer different questions, and both are reported.

## Revisit trigger

**Not** D2's trigger, and not [#97]'s — that covers streaming for sub-hour
freshness, which is a different question. Revisit *this* decision if the
published data grows past the point where a nightly copy is meaningfully
expensive, or if a second engine needs to read the same tables directly. Either
would flip the calculus toward Iceberg. At 1.53 MiB a night, neither is close.

[#97]: https://github.com/gganssle/chip_chat/issues/97
[#104]: https://github.com/gganssle/chip_chat/issues/104
