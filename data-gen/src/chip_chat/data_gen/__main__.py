"""Command line for generating the synthetic population.

The generator composes orders out of a catalogue and earns points at Chipotle's
published rate, so the ordinary way to run it is against a landing zone that
already carries both — the catalogue of issue #24 and the policy harvest of
issue #21::

    python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
    python -m chip_chat.catalog --landing landing --offline
    python -m chip_chat.data_gen --landing landing

Nothing here fetches anything. The catalogue and the published rewards terms
are read from the landing zone and the population is written back beside them,
which is also how the first acceptance criterion is checked by hand: run it
twice and compare the ``population_version`` it prints.

There is no flag for the earn rate or for what a reward costs, and there is no
key for either in ``--config``. Issue #27 asks that they be "taken from the
real published rewards terms, not invented", so they are read from
``parsed/chipotle/policy`` and the run stops if they are not published there.

``--seed`` overrides the seed in the config file without editing it, which is
what you want when you are looking at *a* population rather than *the* one.
``--config`` replaces the whole file, which is what you want when you are
retuning it — and the point of issue #25's fourth criterion is that retuning
is an edit to a file rather than a change to this program.
"""

import argparse
import dataclasses
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from chip_chat.catalog import DEFAULT_PREFIX as CATALOG_PREFIX
from chip_chat.catalog import load_catalog
from chip_chat.catalog.errors import CatalogError
from chip_chat.data_gen.config import GeneratorConfig, load_config
from chip_chat.data_gen.errors import GeneratorError
from chip_chat.data_gen.generate import generate_population
from chip_chat.data_gen.records import DEFAULT_PREFIX, SyntheticPopulation
from chip_chat.data_gen.rewards import load_rewards_terms
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.sources.chipotle import DEFAULT_POLICY_PREFIX


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so tests can exercise it without a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.data_gen",
        description="Generate the seeded synthetic population from a catalogue.",
    )
    parser.add_argument(
        "--landing",
        type=Path,
        required=True,
        help="Directory the catalogue lives in and the population is written to.",
    )
    parser.add_argument(
        "--catalog-prefix",
        default=CATALOG_PREFIX,
        help=f"Key prefix the catalogue was written under. Defaults to {CATALOG_PREFIX}.",
    )
    parser.add_argument(
        "--policy-prefix",
        default=DEFAULT_POLICY_PREFIX,
        help=(
            "Key prefix the policy harvest was written under, which is where "
            f"the published rewards terms are read from. Defaults to "
            f"{DEFAULT_POLICY_PREFIX}."
        ),
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Key prefix for the population. Defaults to {DEFAULT_PREFIX}.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Generation parameters. Defaults to the population.toml shipped "
            "inside the package."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override the seed in the config without editing the file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the population, write it, and print its manifest.

    Args:
        argv: Command line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        A process exit status: zero on success, one if the catalogue or the
        published rewards terms cannot be read, or if the parameters do not
        describe a population that can exist.
    """
    args = build_parser().parse_args(argv)
    blobs = LocalBlobStore(args.landing)

    try:
        config = load_config(args.config)
        if args.seed is not None:
            config = dataclasses.replace(config, seed=args.seed)
        catalog = load_catalog(blobs, args.catalog_prefix)
        terms = load_rewards_terms(blobs, args.policy_prefix)
        population = generate_population(catalog, terms, config)
    except (CatalogError, GeneratorError) as error:
        print(f"population generation failed: {error}", file=sys.stderr)
        return 1

    written = population.write(blobs, args.prefix)
    print(
        f"wrote {len(written)} files under {args.landing / args.prefix}",
        file=sys.stderr,
    )
    _report_fixtures(population, config)
    print(json.dumps(population.manifest(), indent=2, sort_keys=True))
    return 0


def _report_fixtures(population: SyntheticPopulation, config: GeneratorConfig) -> None:
    """Say how many fixtures each archetype supplied, and flag any that fell short.

    Selection never pads: an archetype whose customers cannot clear its own
    bounds contributes fewer fixtures rather than a worse one. That is the right
    behaviour and the wrong thing to be silent about — a retune that quietly
    leaves the Lapsed Customer with one exemplar has broken the demo without
    breaking the run. So it is said out loud, here, where whoever just retuned
    the file is looking.
    """
    counts = Counter(row.persona_id for row in population.persona_fixtures)
    wanted = config.fixtures_per_persona
    short = [spec for spec in config.personas if counts[spec.persona_id] < wanted]
    summary = ", ".join(
        f"{spec.persona_id} {counts[spec.persona_id]}" for spec in config.personas
    )
    print(f"persona fixtures ({wanted} wanted each): {summary}", file=sys.stderr)
    for spec in short:
        print(
            f"  warning: {spec.persona_id} supplied {counts[spec.persona_id]} of "
            f"{wanted}; too few of its customers clear its own criteria, so the "
            "behaviour it exists to demonstrate is thin in this population",
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
