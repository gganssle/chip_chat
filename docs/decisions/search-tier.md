# Decision: Azure AI Search stays on the Free tier, and the ceiling is stated

**Issue:** [#10](https://github.com/gganssle/chip_chat/issues/10) (RFC Q3) · **Decided:** 25 August 2026 · **Consequences found:** 27 August 2026
**Changes:** [#49](https://github.com/gganssle/chip_chat/issues/49) (the degrade path becomes first-class), [#17](https://github.com/gganssle/chip_chat/issues/17) (a second exhaustible budget)
**Does not change:** the retrieval design. Hybrid plus a reranker is still what production sends

---

## The question, and why it was worth asking rather than assuming

RFC-001 assumed the semantic reranker and the cost model assumed the Free tier,
and older Microsoft guidance said the Free tier does not support semantic
ranking. If both were true the design was already over budget by $73.73 a month
before anything else was decided.

**Verified 2026-08-25, and the older guidance is out of date.** Microsoft's
current wording in *Choose a pricing model and service tier* (ms.date 2026-08-04)
is: *"Semantic ranker — Runs on the Free tier but not recommended for large
workloads."* Full evidence in `docs/service-inventory.md`.

## The decision

**Stay on Free.** Retrieval quality assumed the reranker and gets it; Basic's
$73.73/month — $0.101/hour over a 730-hour month, and the plan's "roughly
$75/month" estimate was accurate — stays out of the cost model.

**With the ceiling stated rather than discovered.** The Free semantic billing
plan grants **the first 1,000 semantic requests a calendar month**, and past that
a request returns a *billing error rather than silently charging*, which is the
good failure mode. The standard billing plan requires Basic or higher, so on Free
1,000 a month is not a soft limit you can pay past. **It is roughly 33 a day.**

## What followed, in the order it was found

**Immediately: a second exhaustible budget.** [#17]'s spend cap counts tokens.
Semantic requests are a different budget with a different limit and a different
failure mode — hitting it degrades retrieval *quality* rather than costing money.
`search/src/chip_chat/search/allowance.py` counts them at issue time and persists
the count under the landing root rather than in a process, because a sweep that
spent forty of them in one command should not forget by lunchtime.
`make retrieval-baseline` does exactly that, and its help text says so.

**Immediately: the degrade path had to become first-class.** [#49] degrades to
hybrid-without-reranking when the allowance is exhausted rather than erroring,
because the knowledge lane declining entirely is a worse outcome than slightly
weaker ranking. `docs/retrieval.md` §6 and §7 are that argument.

**And one finding that is not about the reranker at all.** The Free tier has **no
managed identity**. AI Search must be reached with an API key, and there are no
customer-managed keys, no IP firewall and no private endpoints. RFC-001 leans on
identity-bound access as a design property, so this is a *stated* proof-of-concept
trade-off rather than a surprise later. It is the strongest non-reranker argument
for Basic and it was not the reason the tier was chosen.

**Two days later, the expensive one.** `docs/retrieval.md` §9 is a defect report:
**Free-tier vector search silently returns empty results under load.** A vector
query returns HTTP 200, no error, no warning, and `"value": []`, at a rate that
climbs from ~25% on a rested service to ~85–90% after a few hundred queries. A
hybrid query fuses two rankers by reciprocal rank and carries no field saying
which ranker contributed, so the application receives a well-formed hybrid
response that is silently lexical-only. Six hypotheses were eliminated by
experiment rather than argument — the vectorizer, the embedding deployment, four
API versions, rescoring, quantization, `k`, and the quota — and what is left is
the service.

**This does not reverse the decision, and it does change what the decision
means.** The reranked arm — the one production sends — is unaffected in every
measurement, so the blast radius is the degrade path and the ablation's two
vector arms. But the honest sentence is not "we saved $73.73". It is: *we saved
$73.73 and hybrid retrieval is sometimes not hybrid, silently, and we found out
by re-running an ablation.* `docs/cost.md` §8 is where that lives in the cost
argument, because a cost review that only looked at the bill would have recorded
this line as a clean win.

## Revisit trigger

Basic settles all three of these at once — the 1,000/month ceiling, the missing
managed identity, and the vector degradation. Revisit when **any** of: the demo
is shown to enough people that 33 semantic requests a day is the binding
constraint; the degrade path stops being acceptable; or somebody wants the
identity-bound access RFC-001 §05 describes to extend to the search service
rather than stopping at Snowflake. Tracked as `chip-wez`.
