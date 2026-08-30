# Documents

The three planning documents Chip Chat is built from, checked in so every
contributor — human or agent — has them locally. Read them in this order.

| Document | Authoritative for |
| --- | --- |
| [system-design.md](system-design.md) | Phases, sequencing, cost guardrails |
| [cilantro-prd.md](cilantro-prd.md) | Requirements (E/K/A/P/T/V/S ids), launch gates, metrics |
| [rfc-001.md](rfc-001.md) | Components, data model, tool contracts, span schema, decisions |
| [action-surface.md](action-surface.md) | What the four write tools take, and what they refuse — read off the real ordering flow and rewards terms |
| [service-inventory.md](service-inventory.md) | Verified service names, tiers, quotas and prices — and where the three documents above are now wrong |
| [local-setup.md](local-setup.md) | Getting a machine from clean to a passing `make ci` — CLIs, authentication, and how secrets reach a local process |
| [local-tracing.md](local-tracing.md) | The development loop with Phoenix: start the stack, send a turn, read the span tree |
| [lakehouse-catalog.md](lakehouse-catalog.md) | The Unity Catalog layout — six medallion schemas, who may write to them, and the two jobs that check both claims against the live workspace |
| [bronze-ingestion.md](bronze-ingestion.md) | How both streams get out of ADLS and into bronze — the Auto Loader options, what a row carries, where a malformed document goes, and the run that checked all four claims |
| [silver-conformance.md](silver-conformance.md) | How both streams stop being what arrived and start being what is true — what is deduplicated and on which key, what counts as boilerplate and how its removal is checked, and which violations stop the pipeline |
| [corpus-chunking.md](corpus-chunking.md) | Where a fact ends — what one chunk is, the metadata schema the retrieval index is built from, and the fixed-window chunker kept in the test suite so the two required tests can be run over it and required to fail |
| [gold-marts.md](gold-marts.md) | The four personalization marts — what every number in them means, how `usual_order.confidence` is defined so a low value hedges honestly, and why no mart reads the table holding the fields a visitor may edit |
| [recommender.md](recommender.md) | The item-affinity recommender — what it scores, why it refuses to suggest anything a visitor has ever ordered, and why a run only takes the `@champion` alias by beating a popularity baseline on *novel* hits |
| [retrieval-index.md](retrieval-index.md) | The knowledge lane's index — what a chunk becomes, the one alias write that makes a corpus live, which half of integrated vectorization this estate can have and why, and four things the live service does that its documentation does not say |
| [retrieval.md](retrieval.md) | The knowledge lane's *query* — why hybrid is not a hedge, the three constraints it refuses to guess at rather than approximate, how a question the corpus cannot answer is made legible instead of paraphrased over, and the 1,000-a-month ceiling that makes retrieval-without-a-reranker a path with a test rather than a fallback |
| [snowflake-account.md](snowflake-account.md) | The serving layer's account — two X-Small warehouses that suspend in sixty seconds, three roles that are siblings rather than a ladder, the credit ceiling that keeps a runaway from ending the trial early, and the nine Snowflake behaviours that make a security check pass while proving nothing |
| [nightly-publish.md](nightly-publish.md) | The seam between the two clocks — the eleven tables that cross from the lakehouse into the serving layer, the one statement that makes a generation live without detaching the row access policy on it, and why `derived_at` is copied rather than stamped |
| [snowflake-schema.md](snowflake-schema.md) | The eighteen tables in it — the ten columns RFC-001 §04 does not print and why each is unavoidable, the view that refuses to let a visitor-scoped table forget `demo_id`, and why nothing is clustered |
| [demo-reset.md](demo-reset.md) | The nightly reset that ages demo sessions out instead of truncating them — why a persisted visitor makes truncation the wrong shape, the four clocks a visitor's activity is read off because `last_seen` is written by only one of the four write procedures, the baseline table that makes “restores generated state exactly” checkable rather than assertable, and the decision that the nightly publish rather than the reset is what has to stop erasing live rows |
| [snowflake-isolation.md](snowflake-isolation.md) | The mechanism that keeps one visitor out of another's rows — two row access policies keyed to a session variable, why default deny is written out rather than inherited, the one table whose policy is open while nothing is bound and why that is narrower than leaving it unprotected, and the three places the coverage question is asked so the guarantee cannot decay quietly |
| [snowflake-semantic-view.md](snowflake-semantic-view.md) | The account lane's semantic view — five of the serving layer's tables and two of `menu_items`' nine columns, the nine tables left out and the argument for each, why a semantic view is not a view and `COPY GRANTS` is load-bearing, and the measured latency the PRD's turn targets are being re-baselined against |
| [ops-api.md](ops-api.md) | The only path that writes — the confirmation rule as a precondition rather than an instruction, the record each of the four actions claims before it writes, why the retry key is the card's own id and never the caller's, and what a trace has to carry for the launch gate to be auditable in it |
| [failure-isolation.md](failure-isolation.md) | The seven ways a dependency goes away and what each one is allowed to break — how every row of RFC-001 §10's table is verified by breaking that dependency for real, the three places the account lane's refusal to write its own SQL is asserted in code rather than in behaviour, why a stale mart is a nightly job that is down and not a lane that is, and the surface that turns "the demo is broken" into a lane name |
| [corpus-freshness.md](corpus-freshness.md) | Keeping the harvested corpus from going quietly stale — what a re-harvest re-fetches and what it gets a 304 for, why the weekly run is a free GitHub runner rather than a job cluster, and the freshness check that fails when the corpus has stopped moving |
| [red-team.md](red-team.md) | Both launch gates, attacked rather than argued — the write gate hit directly at the ops API with no model and no browser, the spend ceiling tripped by talking until it stopped, the five refusals that look identical and the two of them that are the finding, and what each gate asks for that is still not true |
| [phase-0-verification.md](phase-0-verification.md) | What Phase 0 actually proved against live services — the model deployments answering, the image-token arithmetic behind not thumbnailing an upload, and the thread-retention probe that ships an instrument instead of a claim |
| [content-safety.md](content-safety.md) | Moderation on inbound text and the prompt shield beside it — why the check being private to the spend gate is what makes "nothing unmoderated reaches a model" a property of the type rather than of a function's control flow, why the retrieved-content envelope carries a per-turn nonce that a planted document cannot close, the two failures that look identical to a visitor and must never be confused in a trace, and which analyzer is actually running |
| [public-demo.md](public-demo.md) | The tier a stranger touches — why the persona arrives on the second request and not in the HTML, the per-archetype grammar that keeps the opening message from reading like a template, PRD Flow 3's card and the edit that mints a new draft rather than mutating one, what the switch actually releases, the branding review, and exactly how far the streaming goes |
| [launch-readiness.md](launch-readiness.md) | The V0 go/no-go and the evidence behind it — both launch gates with what each measurement did and did not exercise, the five headline targets against what they actually scored, the requirement table with its caveats rather than its ticks, and the ordered list of what would flip a no-go to a go |
| [demo-script.md](demo-script.md) | The five-minute demo in the words you would say — the personas to pick, what to talk about during a thirty-four-second pause, the two beats that cannot currently be performed and what to do instead, and a two-minute cut made only of what is completely true |
| [deployment.md](deployment.md) | Getting the chat app onto the public URL — the procedure, the eleven things that surprised the people who did it, the measured cold start and turn latency, and the runbook for rollback, scale-to-one and takedown |
| [arize-switch.md](arize-switch.md) | Repointing the exporter from Phoenix to Arize AX — the diff split into instrumentation code (none) and configuration (two values per tier and one connection), the agent-version manifest diff that turns out to be two lines, the two gaps found and written down rather than patched, and the one-page purchase procedure that belongs to the repository owner |
| [runbook.md](runbook.md) | The procedures you will want at the moment you cannot look them up — kill switch, rollback, scale, demo reset, teardown, rebuild from cold and incident triage, each with the elapsed time it actually took, each written twice because `make` does not work from a phone and the raw `az` command does |
| [workspace-drift.md](workspace-drift.md) | Whether the Databricks workspace is running what this repository says — the nightly failure whose well-written error message named the wrong cause because the deployed module was 37 lines behind `main`, why the gap was never that Terraform cannot see this, why the check is twenty-four `databricks workspace export` calls rather than a plan, how its list of managed paths is derived from the Terraform source and what the count tripwire on top of it is for, and the four things it deliberately does not check |
| [decisions/end-of-life.md](decisions/end-of-life.md) | Why this proof of concept ends with the Snowflake trial and buys nothing — the rebuild path deliberately left untested, why 2026-09-24 is a cliff and not a slope, and why the hostname should be read from Terraform rather than copied from a document |
| [cost.md](cost.md) | What one conversation costs across four platforms — the token half that falls out of the spans, the one lane that is four hundred times the others, the standing infrastructure that outweighs both, and the guardrail audit of everything that is supposed to be keeping the bill down |
| [writeup.md](writeup.md) | The capstone — the architecture as built rather than as planned and the five lanes none of which is wired, every decision D1–D9 and every open question revisited, the numbers with their gaps printed at the same size as their successes, the seven predicted traps and which were walked into anyway, the observability diff that is empty on one side and two lines on the other, why Snowflake serves and Databricks computes tested against a bill that says the models are 1.6% of it, and seven failures worth dwelling on — including a committed baseline that contained a false conclusion and a launch gate closed by bookkeeping |
| [chipotle-nutrition-spot-check.md](chipotle-nutrition-spot-check.md) | What the harvested nutrition and allergen data was checked against by hand, and when |
| [chipotle-policy-spot-check.md](chipotle-policy-spot-check.md) | The same, for the harvested rewards, FAQ, catering and store data |
| [chipotle-pdf-spot-check.md](chipotle-pdf-spot-check.md) | The same, for the PDF path — including the finding that Chipotle publishes none, and the live Document Intelligence round trip that checks the reader anyway |
| [synthetic-population-texture.md](synthetic-population-texture.md) | Whether the synthetic population is thin — nineteen measured checks, the distributions behind them, and the customers worth a demo query. Generated, not written |
| [decisions/](decisions/) | Decision records for questions the planning documents left open |

The system design frames the problem, the PRD defines what to build, and the RFC
defines how. When two documents disagree, the table above decides: whichever one
owns the subject wins.

The action surface is a *narrowing* of the RFC rather than a fourth plan. The RFC fixes
eleven tools; that document fixes what the four write tools' arguments contain and what
they reject, from the published menu and rewards terms rather than from imagination, and
marks every claim it could not source as invented. It never adds a tool — where the real
ordering flow implies one, it says so and leaves the RFC's list alone.

The service inventory is a fourth kind of document: it does not decide anything, it
*checks*. All three planning documents close by warning that service names and tiers
move faster than the plan; where the inventory contradicts one of them on a matter of
fact — a product name, a quota, a price, a region — the inventory is right, and the
date at the top of it says how long that is likely to stay true.

The setup, tracing, deployment and lakehouse-catalogue guides are a fifth kind
again: they are *procedures*, not plans or checks. The setup guide records what to install, how to
authenticate each platform, and which steps are deliberately not done yet. The
tracing guide records the loop you run every day once it is installed. The
deployment guide records how the app reaches the public URL — and, more usefully,
what turned out not to work the way the documentation implies. The lakehouse
catalogue guide does the same for Unity Catalog: the schema layout and the
grants, and the two jobs that prove lineage resolves and the read-only principal
really is refused. The bronze-ingestion guide continues it one layer up: the
pipeline that fills those schemas, the four Auto Loader properties issue #33
asks for, and the job that asserts each of them against the live workspace. The
silver-conformance guide continues it one further, and is the first of them that
has to argue rather than record — which key a duplicate is collapsed on, what
counts as boilerplate, and why every expectation in that layer stops the pipeline
instead of writing a warning nobody reads. The corpus-chunking guide is the
medallion's other gold half and has the sharpest single decision in the set:
RFC-001 §08 says chunking follows structure rather than length, so that guide
records what a chunk is, what metadata it carries into the search index, and why
the fixed-window chunker nobody should use is kept in the test suite — the issue
asks that the two required tests *would fail if fixed-window chunking were
substituted*, and the only way to know that is to substitute it. The gold-marts
guide is the last of the five and argues hardest, because a mart is a
*definition* rather than a
transformation: what a usual order is, what a confidence of 0.31 licenses the
assistant to say, and which table the whole layer refuses to read so that a
visitor editing their display name cannot invalidate an answer. The recommender
guide is the personalization lane's last document and the only one about a
*model*: what it scores, why it will not suggest anything a visitor has ever
ordered, and why a training run takes the deployed alias only by beating a
popularity baseline on hits the visitor had not already had — which is PRD P2's
"rather than generic popularity" turned into a number rather than a hope. The two
retrieval guides are a pair, and the seam between them is worth keeping: the
index guide is about *construction* — what a chunk becomes, and the one write
that makes a corpus live — while the retrieval guide is about *a question*, and
turns out to be mostly a document about refusals, because the interesting content
of a query layer is the constraints it declines to approximate.
instead of writing a warning nobody reads. The Snowflake account guide is the
lakehouse catalogue's opposite number on the serving side, and carries the same
burden of proof: not that the read role is configured to be read-only, but that
it was asked to write and refused — and it closes, like the deployment guide,
with the things that surprised the person who did it first. The nightly publish
guide is the join between the two sides, and the only document here that has to
be true of two accounts at once: it argues the atomicity of a swap that must not
take a row access policy with it, and the one projection rule — copy the
timestamp, never compute it — that makes RFC-001 §10's "stale, with its
`derived_at`, never stale as fresh" a property of the code rather than a promise.
The semantic-view guide is the serving side's answer to the recommender guide:
the account lane's model is a *curation* rather than a schema, and almost all of
the curation is subtraction, so most of that document is the argument for each
of the nine tables and fifteen columns it leaves out — plus the seventeen
questions that were put through the live service to find out whether leaving
them out actually produces a refusal.

The texture report is evidence of a different kind again, and the only document here
that is *generated*. Issue #28 asks that "the data is interesting" become visible rather
than asserted, so `chip_chat.data_gen.texture` measures the population on every
generation and renders what it measured; `test_texture_suite.py` regenerates the file and
compares, so a retune that flattened a distribution fails the suite rather than leaving
the document quietly describing last week's population. Edit it by running it.

The three spot checks are a sixth kind: *evidence*. The unit tests run against fixtures,
so a green suite proves the harvester is self-consistent rather than that it still agrees
with what Chipotle published this afternoon. Those files record hand comparisons against
the live pages, and the dates on which they were true.

The two **operations** documents are an eighth kind, and they are a pair. The runbook
holds procedures and no arguments: it is written for the moment you cannot look
anything up, so every command appears twice — the raw `az` or `snow` form with the
resource names spelled out, because `make` needs an initialised Terraform directory and
therefore does not work from a phone, and the `make` target beside it for when you are
at a laptop. Each procedure carries the elapsed time it actually took, and the ones that
have *not* been run are listed at the end rather than left to look executed. The cost
document holds the arguments and no procedures: it is where the token counts on the
spans, the credits in `ACCOUNT_USAGE`, the DBUs and the gateway-hours are reconciled
into a number for one conversation, and where the guardrails that are supposed to keep
the bill down are audited against the running system rather than against the intention.

The `decisions/` directory is the last: one file per question that was open when the
planning documents were written and has since been settled. A decision record carries
the choice, the rationale, and what it costs — and the document it amends is edited in
the same commit, so the RFC never disagrees with the record that changed it. A record
may be filed against any issue, not only one titled `Decision:`; what makes it a record
is that a question was open and now is not. Two records are deliberately filed against
issues that are still **open**, because the choice is made and only a measurement is
outstanding — [`observability-backend.md`](decisions/observability-backend.md) and
[`snowflake-region.md`](decisions/snowflake-region.md) — and each says so in its own
first lines. The RFC's named revisit triggers are not gaps in this directory: they are
questions deliberately deferred past V0 and they belong on their issues until something
trips them.

## Repository conventions

Decided in [issue #6](https://github.com/gganssle/chip_chat/issues/6) and inherited
by everything built after it.

- **Python 3.13**, managed by [uv](https://docs.astral.sh/uv/). One lockfile at the
  repository root; each of the thirteen directories is a uv workspace member with its
  own `pyproject.toml` and its own `README.md`.
- **ruff** for both formatting and linting. **mypy** for type checking, strict on
  `otel/` and `agent/`. **pytest** for tests.
- **Package layout.** Each directory is `src/chip_chat/<name>/`, importable as
  `chip_chat.<name>` — `chip_chat.api`, `chip_chat.data_gen`, and so on. The
  `chip_chat` namespace is not decoration: top-level modules named `snowflake` and
  `databricks` would collide with the vendor SDKs of the same name, which this
  project installs later.
- **`otel/` is a leaf.** Every package may import `chip_chat.otel`; it imports none
  of them. The direction is enforced structurally by an
  [import-linter](https://import-linter.readthedocs.io/) contract in the root
  `pyproject.toml`, checked in CI by `make imports`, because retrofitting
  instrumentation is the mistake the build plan warns about twice.
- **`make ci`** runs what CI runs: format check, lint, type check, import contracts,
  tests. `make setup` takes a fresh clone to a working state.
