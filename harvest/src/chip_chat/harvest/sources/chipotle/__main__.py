"""Command line for the Chipotle harvests.

Two steps, separately runnable, because they fail for different reasons and
one of them costs a third party something::

    python -m chip_chat.harvest.sources.chipotle --landing landing
    python -m chip_chat.harvest.sources.chipotle --landing landing --offline

The first fetches what is not already cached and parses it. The second parses
what is cached and refuses to fetch anything, which is both how a parser
change is iterated on and how the reproducibility claim is checked: run it
twice and compare the manifests it prints.

Four datasets, chosen with ``--dataset``: the menu of issue #19, the nutrition
and allergen data of issue #20, the policy corpus, catering and stores of issue
#21, the PDF nutrition sheets of issue #22, or ``all`` for every one. They share
the landing zone and, in the case of the first two, most of their documents, so
running them together costs less than running them apart::

    python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all

The policy dataset is much the largest of the four in requests — it reads a
locator page and a profile for each of fifty stores — and much the smallest in
bytes.

The PDF dataset is the only one that costs money, and only if it finds
something. It re-reads everything the other three harvested for links ending in
``.pdf``, fetches those, and sends what is really a PDF to Azure Document
Intelligence. Chipotle published none on 26 August 2026, so today it lands four
empty tables and a manifest saying it discovered nothing — which is a result,
not a failure. When it does find one, the endpoint comes from
``--document-intelligence-endpoint`` or ``CHIP_CHAT_DOCUMENT_INTELLIGENCE_ENDPOINT``
(a Terraform output, not a secret) and the credential from ``az login`` or the
container's managed identity. Analyses are cached beside the raw bytes, so a
second run over the same sheet costs nothing either.
"""

import argparse
import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from chip_chat.harvest.analysis import AzureDocumentIntelligence
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.errors import HarvestError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.sources.chipotle.config import HOME_URL
from chip_chat.harvest.sources.chipotle.datasets import (
    ENDPOINT_VARIABLE,
    DatasetBuilder,
)
from chip_chat.harvest.sources.chipotle.locator import DEFAULT_STORE_COUNT
from chip_chat.harvest.sources.chipotle.menu import DEFAULT_RESTAURANT_IDS
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_NUTRITION_PREFIX,
)
from chip_chat.harvest.sources.chipotle.pdf_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_PDF_PREFIX,
)
from chip_chat.harvest.sources.chipotle.policy_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_POLICY_PREFIX,
)
from chip_chat.harvest.sources.chipotle.records import DEFAULT_PARSED_PREFIX
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport

DATASETS = ("menu", "nutrition", "policy", "pdf", "all")
"""What ``--dataset`` accepts. ``menu`` is the default so that the command in
issue #19's documentation keeps meaning what it meant. ``pdf`` comes last
because it reads what the other three landed."""


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
            "allergen data (issue #20), the policy corpus, catering and stores "
            "(issue #21), the PDF nutrition sheets (issue #22), or all of "
            "them. Defaults to menu."
        ),
    )
    parser.add_argument(
        "--stores",
        type=int,
        default=DEFAULT_STORE_COUNT,
        metavar="N",
        help=(
            "How many stores the policy dataset reads from the locator. "
            f"Defaults to {DEFAULT_STORE_COUNT}; issue #21 requires at least 30."
        ),
    )
    parser.add_argument(
        "--document-intelligence-endpoint",
        default=None,
        metavar="URL",
        help=(
            "Document Intelligence account to read PDFs with. Defaults to "
            f"${ENDPOINT_VARIABLE}. Only needed if a PDF is actually found."
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
    parser.add_argument(
        "--policy-prefix",
        default=DEFAULT_POLICY_PREFIX,
        help=(
            "Key prefix for the parsed policy tables. Defaults to "
            f"{DEFAULT_POLICY_PREFIX}."
        ),
    )
    parser.add_argument(
        "--pdf-prefix",
        default=DEFAULT_PDF_PREFIX,
        help=(f"Key prefix for the parsed PDF tables. Defaults to {DEFAULT_PDF_PREFIX}."),
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
        with _harvester(args) as harvester, _analyzer(args) as analyzer:
            builder = DatasetBuilder(
                harvester,
                analyzer,
                blobs,
                restaurants=restaurants,
                home_url=args.home_url,
                store_count=args.stores,
                offline=args.offline,
            )
            prefixes = {
                "menu": args.prefix,
                "nutrition": args.nutrition_prefix,
                "policy": args.policy_prefix,
                "pdf": args.pdf_prefix,
            }
            for name in wanted:
                dataset = builder.dataset(name)
                written = dataset.write(blobs, prefixes[name])
                manifests[name] = dataset.manifest()
                print(
                    f"wrote {len(written)} files under {args.landing / prefixes[name]}",
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


@contextmanager
def _analyzer(
    args: argparse.Namespace,
) -> Iterator[AzureDocumentIntelligence | None]:
    """Yield an analyzer, or ``None`` when there is nowhere to send a document.

    Constructed but not authenticated: the credential is resolved on the first
    call, so a run that finds no PDF — which is every run against Chipotle
    today — never asks Azure for a token, and a developer without a
    subscription can still build the other three datasets.
    """
    endpoint = args.document_intelligence_endpoint or os.environ.get(
        ENDPOINT_VARIABLE, ""
    )
    if args.offline or not endpoint.strip():
        yield None
        return
    with AzureDocumentIntelligence(endpoint) as analyzer:
        yield analyzer


if __name__ == "__main__":
    raise SystemExit(main())
