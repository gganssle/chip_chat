"""Command line for the Chipotle harvests.

Two steps, separately runnable, because they fail for different reasons and
one of them costs a third party something::

    python -m chip_chat.harvest.sources.chipotle --landing landing
    python -m chip_chat.harvest.sources.chipotle --landing landing --offline

The first fetches what is not already cached and parses it. The second parses
what is cached and refuses to fetch anything, which is both how a parser
change is iterated on and how the reproducibility claim is checked: run it
twice and compare the manifests it prints.

Two datasets, chosen with ``--dataset``: the menu of issue #19, the nutrition
and allergen data of issue #20, or ``all`` for both. They share the landing
zone and most of their documents, so running both costs barely more than
running either::

    python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
"""

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import HarvestError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.sources.chipotle.config import HOME_URL
from chip_chat.harvest.sources.chipotle.menu import (
    DEFAULT_RESTAURANT_IDS,
    harvest_menu,
    load_menu,
)
from chip_chat.harvest.sources.chipotle.nutrition import (
    harvest_nutrition,
    load_nutrition,
)
from chip_chat.harvest.sources.chipotle.nutrition_parse import parse_nutrition
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_NUTRITION_PREFIX,
)
from chip_chat.harvest.sources.chipotle.nutrition_records import NutritionDataset
from chip_chat.harvest.sources.chipotle.parse import parse_menu
from chip_chat.harvest.sources.chipotle.records import (
    DEFAULT_PARSED_PREFIX,
    MenuDataset,
)
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport

DATASETS = ("menu", "nutrition", "all")
"""What ``--dataset`` accepts. ``menu`` is the default so that the command in
issue #19's documentation keeps meaning what it meant."""


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so tests can exercise it without a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.harvest.sources.chipotle",
        description="Harvest and parse Chipotle's published menu.",
    )
    parser.add_argument(
        "--landing",
        type=Path,
        required=True,
        help="Directory the raw and parsed blobs live in.",
    )
    parser.add_argument(
        "--restaurant",
        dest="restaurants",
        action="append",
        metavar="ID",
        help=(
            "A restaurant to price the catalogue at. Repeatable. The first "
            f"defines the catalogue's structure. Defaults to "
            f"{', '.join(DEFAULT_RESTAURANT_IDS)}."
        ),
    )
    parser.add_argument(
        "--dataset",
        choices=DATASETS,
        default="menu",
        help=(
            "Which dataset to build: the menu (issue #19), the nutrition and "
            "allergen data (issue #20), or both. Defaults to menu."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Parse what is already cached and make no requests at all.",
    )
    parser.add_argument(
        "--home-url",
        default=HOME_URL,
        help=f"Page the API configuration is read from. Defaults to {HOME_URL}.",
    )
    parser.add_argument(
        "--contact",
        default=DEFAULT_CONTACT,
        help="Address a site owner can reach you at; goes in the User-Agent.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PARSED_PREFIX,
        help=(
            f"Key prefix for the parsed menu tables. Defaults to {DEFAULT_PARSED_PREFIX}."
        ),
    )
    parser.add_argument(
        "--nutrition-prefix",
        default=DEFAULT_NUTRITION_PREFIX,
        help=(
            "Key prefix for the parsed nutrition tables. Defaults to "
            f"{DEFAULT_NUTRITION_PREFIX}."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harvest, or the offline parse, and print the manifest.

    Args:
        argv: Command line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        A process exit status: zero on success, one on a harvest failure.
    """
    args = build_parser().parse_args(argv)
    blobs = LocalBlobStore(args.landing)
    restaurants = args.restaurants or list(DEFAULT_RESTAURANT_IDS)
    wanted = DATASETS[:-1] if args.dataset == "all" else (args.dataset,)

    manifests: dict[str, Any] = {}
    try:
        with _harvester(args) as harvester:
            for name in wanted:
                dataset = _build(name, harvester, blobs, args, restaurants)
                prefix = args.prefix if name == "menu" else args.nutrition_prefix
                written = dataset.write(blobs, prefix)
                manifests[name] = dataset.manifest()
                print(
                    f"wrote {len(written)} files under {args.landing / prefix}",
                    file=sys.stderr,
                )
    except HarvestError as error:
        print(f"harvest failed: {error}", file=sys.stderr)
        return 1

    payload = manifests["menu"] if wanted == ("menu",) else manifests
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


@contextmanager
def _harvester(args: argparse.Namespace) -> Iterator[Harvester | None]:
    """Yield a harvester, or ``None`` when the run is forbidden to fetch.

    One instance for the whole run, so that building both datasets shares one
    politeness gate and one warm cache rather than opening a second transport
    and asking the site for the home page twice.
    """
    if args.offline:
        yield None
        return
    with Harvester(
        LocalBlobStore(args.landing), HttpxTransport(), contact=args.contact
    ) as harvester:
        yield harvester


def _build(
    name: str,
    harvester: Harvester | None,
    blobs: LocalBlobStore,
    args: argparse.Namespace,
    restaurants: list[str],
) -> MenuDataset | NutritionDataset:
    """Obtain one dataset's documents and parse them."""
    if name == "menu":
        if harvester is None:
            return parse_menu(
                load_menu(DocumentCache(blobs), restaurants, home_url=args.home_url)
            )
        return parse_menu(harvest_menu(harvester, restaurants, home_url=args.home_url))
    if harvester is None:
        return parse_nutrition(
            load_nutrition(DocumentCache(blobs), restaurants, home_url=args.home_url)
        )
    return parse_nutrition(
        harvest_nutrition(harvester, restaurants, home_url=args.home_url)
    )


if __name__ == "__main__":
    raise SystemExit(main())
