"""Rewrite the committed texture report from the shipped config.

    uv run python data-gen/tests/regenerate_texture_report.py

Issue #28 asks for "a notebook or report rendering these distributions,
committed, so 'the data is interesting' becomes something visible rather than
asserted". This writes it, and ``test_texture_suite.py`` regenerates it on
every run and compares — so a retune that flattened a distribution fails the
suite with an instruction rather than leaving a committed document quietly
describing last week's population.

The report is of the population generated from the *committed fixture
catalogue*, because that is the one every machine has. It says so in its own
first paragraph, and every check in it is measured relative to what that
catalogue makes possible — so the same document regenerated against a real
harvest is the same claims about a bigger menu, not different claims.

Run this after any deliberate change to the generator or to ``[texture]``,
read the diff, and commit it with the change that caused it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from population_fixtures import (
    REPOSITORY,
    fixture_catalog,
    fixture_population,
    shipped_config,
)

from chip_chat.data_gen import measure_texture, render_report

REPORT = REPOSITORY / "docs" / "synthetic-population-texture.md"
"""Where the committed report lives."""

TITLE = "The synthetic population is not thin"
"""Its heading, which is also the claim it exists to support."""

REGENERATE = "`uv run python data-gen/tests/regenerate_texture_report.py`"
"""What to run when the committed report and the population disagree."""

PREAMBLE = """
> **Issue:** [#28](https://github.com/gganssle/chip_chat/issues/28) (bead `cc-aho`) ·
> **Generated, not written.** Every number below was measured by
> `chip_chat.data_gen.texture` from the population the shipped `population.toml`
> produces against the committed fixture catalogue, which is the catalogue every
> machine has. That catalogue publishes nine orderable things; a real harvest
> publishes hundreds. Every check is therefore measured *relative to what the
> catalogue makes possible* — coverages, ratios, shares and effect sizes, never
> counts of foods — so this same suite is meaningful at both sizes and bites on a
> real harvest where a generator that only ever composed three baskets would sail
> past an absolute threshold.
>
> Issues [#25](https://github.com/gganssle/chip_chat/issues/25) and
> [#26](https://github.com/gganssle/chip_chat/issues/26) both declined to claim
> food variety and both named #28 as the ticket that would settle it. This is that
> claim, and the suite behind it runs on every generation rather than here.
"""
"""What the report says about itself before it says anything about the data."""


def document() -> str:
    """Return the report exactly as the committed file should hold it.

    A function rather than a step inside :func:`main` so that
    ``test_texture_suite.py`` can compare the committed file against it
    *without writing anything*. A staleness test that regenerates first can
    only prove the generator is deterministic; this one proves the file in git
    is the file this population produces.
    """
    population = fixture_population()
    texture = measure_texture(population, fixture_catalog(), shipped_config())
    heading, _, rest = render_report(texture, population, TITLE).partition("\n")
    return f"{heading}\n{PREAMBLE}{rest}"


def main() -> int:
    """Regenerate the committed report.

    Returns:
        A process exit status.
    """
    population = fixture_population()
    texture = measure_texture(population, fixture_catalog(), shipped_config())
    REPORT.write_text(document(), encoding="utf-8")

    failed = texture.failures()
    for check in texture.checks:
        print(
            f"{'ok  ' if check.held else 'FAIL'} {check.name:24} {check.measured:12,.3f}"
        )
    print(f"{len(texture.standouts)} standout customers")
    print(f"wrote {REPORT.relative_to(REPOSITORY)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
