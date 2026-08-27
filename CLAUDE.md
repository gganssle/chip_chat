# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Beads ↔ GitHub Issues

GitHub Issues stays the public, human-facing tracker; beads is the working tracker agents read
and write. Every bead imported from GitHub keeps its `External:` link back to the issue.

The sync needs a token in the environment (it is deliberately not stored in `.beads/config.yaml`,
which is committed):

```bash
export GITHUB_TOKEN=$(gh auth token)

bd github status                 # verify owner/repo/token
bd github sync --pull-only       # GitHub -> beads (safe, local only)
bd github pull <issue-number>    # pull one issue the bulk sync missed
bd github sync --push-only       # beads -> GitHub (creates/edits real issues — ask first)
bd github sync                   # bidirectional
```

Owner and repo are already configured (`gganssle/chip_chat`) via `bd config`.

Conventions:
- Priority mirrors the GitHub `P0`–`P3` label; labels are copied onto the bead on import.
- Pulling is local and reversible. Pushing writes to the public repo — confirm before running it.
- `bd github sync --pull-only` can skip an issue created seconds earlier; re-run it, and use
  `bd github pull <n>` for stragglers.


## The four invariants

These are the rules that are easy to violate **innocently** — each one is a
change a reasonable contributor would make in good faith, and each one breaks
something the project's whole point rests on. Everything else in this file is
convention. These four are not.

### 1. Identity is never a tool argument

No tool signature, no endpoint, and no function reachable from the model may
accept a visitor identifier. Identity is bound to the Snowflake session by the
app and enforced by row access policies. **The absence of the parameter is the
enforcement mechanism** — adding a `demo_id` parameter anywhere model-reachable
is the one change that breaks the system's primary guarantee, and it would look
like a helpful refactor in the diff.

The vocabulary is `chip_chat.snowflake.procedures.IDENTITY_VOCABULARY`:
`demo_id`, `visitor`, `visitor_id`, `customer`, `customer_id`, `user`, `user_id`,
`account`, `account_id`, `session_id`, `persona_id`. It is enforced at four tiers,
each with its own test:

| Tier | Where | Test |
| --- | --- | --- |
| The tool surface | `agent/src/chip_chat/agent/surface.py` — `ARGUMENT_NAMES` is *derived* from the JSON schemas at every depth, so a parameter added later lands in it automatically | `agent/tests/test_sabotage.py::test_no_tool_in_the_surface_accepts_anything_identifier_shaped` |
| Stored procedures | `snowflake/sql/` | `snowflake/tests/test_procedure_layout.py` |
| The ops API | `api/src/chip_chat/api/ops.py` — `OpsService.session` is the *only* place `demo_id` appears | `api/tests/test_ops.py::test_no_write_method_names_a_visitor` |
| The request | every Pydantic model is `extra="forbid"` | `api/tests/test_identity_binding.py` |

`chip_chat.demo.id` **does** appear on spans. That is an opaque correlation
attribute for reading a trace. Never read it back off a span to make an
authorisation decision.

### 2. No write without explicit confirmation, checked in code

Not by prompt instruction and not by UI convention. `OpsService._write()` in
`api/src/chip_chat/api/ops.py` calls `claim()` **before a Snowflake session is
acquired**; an unconfirmed, missing or expired record ends the call there, marks
the span `ConfirmationState.REJECTED` or `UNCONFIRMED`, and reaches no procedure
at all. `DraftStore.claim()` deletes the draft as it hands it over, so one draft
is at most one order under retry.

If you find yourself adding "always ask before ordering" to a system prompt as
the mechanism, the mechanism is already there and you are weakening it.

### 3. The spend cap is inline, in the request path — not observability

Azure budget alerts notify after the fact; Arize reports what was spent. **Neither
prevents anything.** A public endpoint with no authentication needs a synchronous
budget check in front of every model call, and it is at
`api/src/chip_chat/api/app.py`'s `service.gate.turn(...)`, inside `chat_turn`.

The shape is *unconstructable-without*: `FundedTurn.__init__` raises
`UnfundedTurnError` if the budget was refused, and `SpendGate` privately holds
the model and the moderator, so holding a `FundedTurn` **is** the proof the check
passed. `api/tests/test_spend_gate.py` asserts that nothing but the gate holds a
model and that every route which can spend goes through it.

From `guard.py`'s own docstring: *if an implementation of this could fairly be
described as observability, it is the wrong implementation.*

### 4. Real published menu, entirely synthetic accounts. Never blurred

Everything Cilantro says about food comes from what Chipotle publishes.
Everything it says about "you" comes from a generated customer. The boundary is
structural in four places rather than documented in one:

- `databricks/src/chip_chat/databricks/catalog.py` — `schema(layer, stream)`
  takes the stream as a **required** argument, so you must say which population
  before you can name a table.
- `snowflake/sql/02_database.sql` — `CATALOGUE` is real, `ACCOUNTS` is synthetic
  and visitor-scoped, and they are different schemas.
- `data-gen/` — `OrderableMenu` is the only source of an identifier in the
  package, which makes an invented SKU unreachable rather than merely untested.
  `data-gen/tests/test_referential_integrity.py` holds it.
- `web/src/chip_chat/web/copy.py` — the visitor is told, in a banner that cannot
  be dismissed.

## Build & Test

```bash
make setup      # fresh clone -> working state. `uv sync --all-packages`
make ci         # THE GATE: setup, fmt-check, lint, typecheck, imports, test
make test       # just pytest
make fmt        # ruff format . && ruff check --fix .
make help       # ~80 targets, ten families
```

**`make ci` must pass on your branch.** It runs `ruff format --check`,
`ruff check`, `mypy .` (strict on `otel/` and `agent/`), `lint-imports`, and
`pytest` over all thirteen packages' `tests/` directories.

One package at a time: `uv run pytest <pkg>/tests`.

**Nothing that costs money or needs a credential is in `make ci`**, and that is a
rule rather than an oversight — a gate that needs a logged-in human is not a
gate. So `make verify-*`, `make search-*`, `make snowflake-*`, `make infra-*`,
`make deploy`, and the live eval targets are all outside it. The eval targets
whose help text says **free** run against the slice with no network and are safe
to run at will: `golden-check`, `adversarial`, `adversarial-sabotaged`,
`adversarial-gate2`, `trajectory`, `grounding`, `retrieval`, `dietary`,
`dataset-check`, `photos-check`, `search-schema`.

`make dev` brings up Phoenix in a container and sends one instrumented turn
through the local stack, so there is a trace tree to read at
<http://localhost:6006> the first time you open it. Tracing is not a late
deliverable here; it is how you find out why something does not work.

## Architecture Overview

**Two clocks.** *Nightly*, Databricks ingests Chipotle's published pages and a
seeded synthetic-account generator through a medallion lakehouse in Unity
Catalog, then publishes a chunked knowledge index to Azure AI Search and four
personalization marts to Snowflake. *Per turn*, a visitor types a name, is
assigned a demo persona, and talks to one Azure AI Foundry agent that calls
**eleven tools across five lanes**.

| Lane | Tools | Behind it |
| --- | --- | --- |
| Knowledge | `search_menu_knowledge` | Hybrid RAG over Azure AI Search |
| Account | `ask_account_question`, `get_points_balance` | Snowflake Cortex Analyst |
| Personalization | `get_usual_order`, `get_recommendations` | Gold marts, computed nightly |
| Vision | `match_meal_from_photo` | Vision model describes, deterministic matcher resolves |
| Action | `propose_order`, `place_order`, `cancel_order`, `redeem_points`, `update_preferences` | Azure Functions ops API → Snowflake procedures, behind a confirmation card |

A lane may fail; the conversation may not. `docs/failure-isolation.md` has the
blast radius of each dependency and the test that verifies it.

Package layout: thirteen `uv` workspace members, one importable package each
under `<dir>/src/chip_chat/<name>/`, one lockfile at the root. **`otel/` is a
leaf** — everything may import it, it imports nothing — enforced by an
import-linter contract and checked by `make imports`. The `chip_chat` namespace
exists because top-level `snowflake` and `databricks` would collide with the
vendor SDKs.

## Conventions & Patterns

**Commit messages are a declarative sentence about what became true**, not a
`feat:` prefix. Read `git log --oneline` and match it.

**Comments and docs explain *why*, in prose, at length.** Match that register; do
not write bullet-point stubs. Every substantive piece of work gets a
`docs/<topic>.md`, and every question the plans left open gets a
`docs/decisions/<name>.md`. Follow the existing files as templates —
`docs/decisions/snowflake-region.md` is the one to imitate, including its habit of
recording the number that *could not* be measured and why.

**Where a number is not measured, say it is not measured.** Do not estimate and
present the estimate as a measurement. Several documents here have a section
whose entire job is listing what was not measured; that is a feature.

### The span vocabulary

`otel/src/chip_chat/otel/schema.py` is the schema of record and it is executable:
nesting is enforced, and a tree RFC-001 does not describe raises
`SpanSchemaError`. Twenty-five span names — ten fixed (`chat.turn`,
`guard.budget_check`, `guard.content_safety`, `agent.step`, `llm.completion`,
`retriever.search`, `db.cortex_analyst`, `vision.describe`, `matcher.resolve`,
`render.response`), eleven `tool.<name>`, four `ops.<action>`.

Attributes come from three namespaces, in precedence order: OpenInference, the
OpenTelemetry `db.*` conventions (on `db.cortex_analyst` only), and
`chip_chat.*`. `otel/README.md` is the prose.

**Two token vocabularies, and merging them is a real bug.** `llm.token_count.*`
belongs to spans that *are* a model call; `chip_chat.tokens.*` is a rollup on
spans that merely *contain* model calls. Summing `llm.token_count.*` across a
trace is exactly the provider's reported usage, which
`chip_chat.otel.testing.assert_token_counts_sum` verifies — a rollup written
under the same keys would double-count every ancestor and quietly destroy that
property. The rollups exist because Application Insights searches attributes and
does not walk trace trees.

**Identity is stamped on every span, not just the root**, because a bug report
arrives with a session id at best. `docs/runbook.md` §10 is the query.

### Operations

`docs/runbook.md` — kill switch, rollback, scale, reset, teardown, triage. Every
procedure is written twice: the raw `az`/`snow` command with names spelled out
(the phone-runnable form) and the `make` target beside it. **Do not run `make`
from a phone** — every ops target resolves its arguments through
`terraform output` and fails before it reaches Azure without an initialised
working directory.

`docs/cost.md` — what a conversation costs, what the guardrails are, and what has
not been measured. §14 is the guardrail audit.
