"""Command line for the Chipotle menu harvest.

Two steps, separately runnable, because they fail for different reasons and
one of them costs a third party something::

    python -m chip_chat.harvest.sources.chipotle --landing landing
    python -m chip_chat.harvest.sources.chipotle --landing landing --offline

The first fetches what is not already cached and parses it. The second parses
what is cached and refuses to fetch anything, which is both how a parser
change is iterated on and how the reproducibility claim is checked: run it
twice and compare the manifests it prints.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import HarvestError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.sources.chipotle.config import HOME_URL
from chip_chat.harvest.sources.chipotle.menu import (
    DEFAULT_RESTAURANT_IDS,
    MenuDocuments,
    harvest_menu,
    load_menu,
)
from chip_chat.harvest.sources.chipotle.parse import parse_menu
from chip_chat.harvest.sources.chipotle.records import DEFAULT_PARSED_PREFIX
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport


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
        help=f"Key prefix for the parsed tables. Defaults to {DEFAULT_PARSED_PREFIX}.",
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

    try:
        documents = _documents(args, blobs, restaurants)
        dataset = parse_menu(documents)
    except HarvestError as error:
        print(f"harvest failed: {error}", file=sys.stderr)
        return 1

    written = dataset.write(blobs, args.prefix)
    manifest = dataset.manifest()
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"wrote {len(written)} files under {args.landing / args.prefix}", file=sys.stderr
    )
    return 0


def _documents(
    args: argparse.Namespace, blobs: LocalBlobStore, restaurants: list[str]
) -> MenuDocuments:
    """Obtain the documents, fetching only when not asked to stay offline."""
    if args.offline:
        return load_menu(DocumentCache(blobs), restaurants, home_url=args.home_url)
    with Harvester(blobs, HttpxTransport(), contact=args.contact) as harvester:
        return harvest_menu(harvester, restaurants, home_url=args.home_url)


if __name__ == "__main__":
    raise SystemExit(main())
