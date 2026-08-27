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
| [gold-marts.md](gold-marts.md) | The four personalization marts — what every number in them means, how `usual_order.confidence` is defined so a low value hedges honestly, and why no mart reads the table holding the fields a visitor may edit |
| [recommender.md](recommender.md) | The item-affinity recommender — what it scores, why it refuses to suggest anything a visitor has ever ordered, and why a run only takes the `@champion` alias by beating a popularity baseline on *novel* hits |
| [snowflake-account.md](snowflake-account.md) | The serving layer's account — two X-Small warehouses that suspend in sixty seconds, three roles that are siblings rather than a ladder, the credit ceiling that keeps a runaway from ending the trial early, and the nine Snowflake behaviours that make a security check pass while proving nothing |
| [deployment.md](deployment.md) | Getting the chat app onto the public URL — the procedure, and the ten things that surprised the first person to do it |
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
instead of writing a warning nobody reads. The gold-marts guide is the last of the
four and argues hardest, because a mart is a *definition* rather than a
transformation: what a usual order is, what a confidence of 0.31 licenses the
assistant to say, and which table the whole layer refuses to read so that a
visitor editing their display name cannot invalidate an answer. The recommender
guide is the personalization lane's last document and the only one about a
*model*: what it scores, why it will not suggest anything a visitor has ever
ordered, and why a training run takes the deployed alias only by beating a
popularity baseline on hits the visitor had not already had — which is PRD P2's
"rather than generic popularity" turned into a number rather than a hope.
instead of writing a warning nobody reads. The Snowflake account guide is the
lakehouse catalogue's opposite number on the serving side, and carries the same
burden of proof: not that the read role is configured to be read-only, but that
it was asked to write and refused — and it closes, like the deployment guide,
with the things that surprised the person who did it first.

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

The `decisions/` directory is a seventh: one file per question that was open when the
planning documents were written and has since been settled. A decision record carries
the choice, the rationale, and what it costs — and the document it amends is edited in
the same commit, so the RFC never disagrees with the record that changed it.

## Repository conventions

Decided in [issue #6](https://github.com/gganssle/chip_chat/issues/6) and inherited
by everything built after it.

- **Python 3.13**, managed by [uv](https://docs.astral.sh/uv/). One lockfile at the
  repository root; each of the twelve directories is a uv workspace member with its
  own `pyproject.toml`.
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
