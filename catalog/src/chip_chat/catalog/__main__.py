"""Command line for building the catalogue.

The catalogue is a consolidation, not a fetch, so the ordinary way to run this
is against a landing zone that has already been harvested::

    python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
    python -m chip_chat.catalog --landing landing --offline

``--offline`` parses what is cached and makes no requests at all, which is both
how the catalogue is rebuilt after a parser change and how its reproducibility
is checked: run it twice and compare the ``content_version`` it prints. Without
``--offline`` it harvests whatever the landing zone is missing first, at the
politeness rate the harvest framework enforces.

``--vocabulary`` writes the generated vision enum module. Nothing in this
repository commits one: RFC-001 section 07 wants the model's vocabulary
generated from the *live* catalogue at build time, and a module generated from
a smaller catalogue and then committed is exactly the hand-maintained list the
generation exists to replace. The one copy under ``catalog/tests/fixtures`` is
generated from the fixture site and exists so that a test can regenerate it and
compare.
"""

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from chip_chat.catalog.build import build_catalog
from chip_chat.catalog.errors import CatalogError
from chip_chat.catalog.load import CatalogLoadError, load_catalog
from chip_chat.catalog.records import DEFAULT_PREFIX, MenuCatalog
from chip_chat.catalog.vocabulary import render_module
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import HarvestError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.sources.chipotle import (
    DEFAULT_RESTAURANT_IDS,
    DEFAULT_STORE_COUNT,
    HOME_URL,
    harvest_menu,
    harvest_nutrition,
    harvest_policy,
    load_menu,
    load_nutrition,
    load_policy,
    parse_menu,
    parse_nutrition,
    parse_policy,
)
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so tests can exercise it without a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.catalog",
        description="Consolidate the harvested menu, nutrition and store data.",
    )
    parser.add_argument(
        "--landing",
        type=Path,
        required=True,
        help="Directory the raw, parsed and catalogue blobs live in.",
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
        "--stores",
        type=int,
        default=DEFAULT_STORE_COUNT,
        metavar="N",
        help=(
            "How many stores to read from the locator. Defaults to "
            f"{DEFAULT_STORE_COUNT}."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Build from what is already cached and make no requests at all.",
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
        default=DEFAULT_PREFIX,
        help=f"Key prefix for the catalogue tables. Defaults to {DEFAULT_PREFIX}.",
    )
    parser.add_argument(
        "--from-built",
        action="store_true",
        help=(
            "Read the catalogue already built under --landing rather than "
            "building one, and write only the vocabulary. Needs no harvest "
            "cache and makes no requests."
        ),
    )
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Write the generated vision enum module here. Defaults to not "
            "writing one; RFC-001 section 07 wants it generated at build time "
            "rather than committed."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build the catalogue, write it, and print its manifest.

    Args:
        argv: Command line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        A process exit status: zero on success, one on a harvest or build
        failure.
    """
    args = build_parser().parse_args(argv)
    blobs = LocalBlobStore(args.landing)
    restaurants = args.restaurants or list(DEFAULT_RESTAURANT_IDS)

    if args.from_built:
        return _vocabulary_from_built(blobs, args)

    try:
        with _harvester(args) as harvester:
            catalog = _catalog(harvester, blobs, args, restaurants)
    except (HarvestError, CatalogError) as error:
        print(f"catalogue build failed: {error}", file=sys.stderr)
        return 1

    written = catalog.write(blobs, args.prefix)
    print(
        f"wrote {len(written)} files under {args.landing / args.prefix}",
        file=sys.stderr,
    )
    if args.vocabulary is not None:
        module = render_module(catalog.vocabulary, catalog.content_version())
        args.vocabulary.parent.mkdir(parents=True, exist_ok=True)
        args.vocabulary.write_text(module, encoding="utf-8")
        print(f"wrote the vision vocabulary to {args.vocabulary}", file=sys.stderr)

    print(json.dumps(catalog.manifest(), indent=2, sort_keys=True))
    return 0


def _vocabulary_from_built(blobs: LocalBlobStore, args: argparse.Namespace) -> int:
    """Render the vision vocabulary from a catalogue that is already built.

    **Why this exists, which is a CI story rather than a catalogue one.** The
    ordinary path above rebuilds the catalogue from the harvest cache, and that
    cache is twenty-odd megabytes of fetched pages that this repository
    deliberately does not commit. So the image build could only ever run
    somewhere that had already harvested -- which in practice meant a laptop,
    which in practice meant the deploy workflow had been failing on every push
    since the vocabulary was first copied into the image. Every deployment since
    was made by hand, and that is exactly how an image whose vocabulary sat in a
    directory on no ``sys.path`` reached production without anybody noticing.

    RFC-001 section 07 is not weakened by this. It says the vocabulary is
    *generated* rather than committed, so that it cannot drift from what is
    orderable; it does not say which artefact it must be generated from. A built
    catalogue is a strictly better source than the harvest cache for this
    purpose, because a built catalogue is the same thing the matcher will be
    checked against at runtime -- and if the two disagree,
    :class:`~chip_chat.vision.matcher.CatalogueDriftError` is raised on the first
    photograph rather than a term being quietly resolved from last month's
    salsa.

    Args:
        blobs: The directory holding the built catalogue.
        args: The parsed arguments. ``--vocabulary`` is required here, because
            the vocabulary is the only thing this branch produces.

    Returns:
        A process exit status.
    """
    if args.vocabulary is None:
        print("--from-built needs --vocabulary: it writes nothing else", file=sys.stderr)
        return 2
    try:
        catalog = load_catalog(blobs, args.prefix)
    except CatalogLoadError as error:
        print(f"no built catalogue under {args.landing}: {error}", file=sys.stderr)
        return 1
    module = render_module(catalog.vocabulary, catalog.content_version())
    args.vocabulary.parent.mkdir(parents=True, exist_ok=True)
    args.vocabulary.write_text(module, encoding="utf-8")
    print(
        f"wrote the vision vocabulary to {args.vocabulary} from the catalogue "
        f"under {args.landing / args.prefix}, content version "
        f"{catalog.content_version()}",
        file=sys.stderr,
    )
    print(json.dumps(catalog.manifest(), indent=2, sort_keys=True))
    return 0


@contextmanager
def _harvester(args: argparse.Namespace) -> Iterator[Harvester | None]:
    """Yield a harvester, or ``None`` when the run is forbidden to fetch."""
    if args.offline:
        yield None
        return
    with Harvester(
        LocalBlobStore(args.landing), HttpxTransport(), contact=args.contact
    ) as harvester:
        yield harvester


def _catalog(
    harvester: Harvester | None,
    blobs: LocalBlobStore,
    args: argparse.Namespace,
    restaurants: list[str],
) -> MenuCatalog:
    """Parse the three datasets out of one landing zone and consolidate them.

    One harvester and one cache for all three, so building the catalogue costs
    the same documents as building the datasets separately would — and, warm,
    costs none.
    """
    cache = DocumentCache(blobs)
    home = args.home_url
    if harvester is None:
        menu = parse_menu(load_menu(cache, restaurants, home_url=home))
        nutrition = parse_nutrition(load_nutrition(cache, restaurants, home_url=home))
        policy = parse_policy(load_policy(cache, home_url=home, store_count=args.stores))
    else:
        menu = parse_menu(harvest_menu(harvester, restaurants, home_url=home))
        nutrition = parse_nutrition(
            harvest_nutrition(harvester, restaurants, home_url=home)
        )
        policy = parse_policy(
            harvest_policy(harvester, home_url=home, store_count=args.stores)
        )
    return build_catalog(menu, nutrition, policy)


if __name__ == "__main__":
    raise SystemExit(main())
