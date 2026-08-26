# Documents

The three planning documents Chip Chat is built from, checked in so every
contributor — human or agent — has them locally. Read them in this order.

| Document | Authoritative for |
| --- | --- |
| [system-design.md](system-design.md) | Phases, sequencing, cost guardrails |
| [cilantro-prd.md](cilantro-prd.md) | Requirements (E/K/A/P/T/V/S ids), launch gates, metrics |
| [rfc-001.md](rfc-001.md) | Components, data model, tool contracts, span schema, decisions |

The system design frames the problem, the PRD defines what to build, and the RFC
defines how. When two documents disagree, the table above decides: whichever one
owns the subject wins.

## Repository conventions

Decided in [issue #6](https://github.com/gganssle/chip_chat/issues/6) and inherited
by everything built after it.

- **Python 3.13**, managed by [uv](https://docs.astral.sh/uv/). One lockfile at the
  repository root; each of the eleven directories is a uv workspace member with its
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
