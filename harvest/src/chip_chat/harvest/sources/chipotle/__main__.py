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

from chip_chat.harvest.analysis import (
    DEFAULT_API_VERSION,
    DEFAULT_MODEL_ID,
    AnalysisCache,
    AzureDocumentIntelligence,
)
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import DocumentAnalysisError, HarvestError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.sources.chipotle.config import HOME_URL
from chip_chat.harvest.sources.chipotle.locator import DEFAULT_STORE_COUNT
from chip_chat.harvest.sources.chipotle.menu import (
    DEFAULT_RESTAURANT_IDS,
    MenuDocuments,
    harvest_menu,
    load_menu,
)
from chip_chat.harvest.sources.chipotle.nutrition import (
    NutritionDocuments,
    harvest_nutrition,
    load_nutrition,
)
from chip_chat.harvest.sources.chipotle.nutrition_parse import parse_nutrition
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_NUTRITION_PREFIX,
)
from chip_chat.harvest.sources.chipotle.nutrition_records import NutritionDataset
from chip_chat.harvest.sources.chipotle.parse import parse_menu
from chip_chat.harvest.sources.chipotle.pdf import (
    analyze_pdfs,
    cached_analyses,
    documents_of,
    harvest_pdfs,
    load_pdfs,
)
from chip_chat.harvest.sources.chipotle.pdf_parse import parse_pdfs
from chip_chat.harvest.sources.chipotle.pdf_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_PDF_PREFIX,
)
from chip_chat.harvest.sources.chipotle.pdf_records import PdfDataset
from chip_chat.harvest.sources.chipotle.policy import (
    PolicyDocuments,
    harvest_policy,
    load_policy,
)
from chip_chat.harvest.sources.chipotle.policy_parse import parse_policy
from chip_chat.harvest.sources.chipotle.policy_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_POLICY_PREFIX,
)
from chip_chat.harvest.sources.chipotle.policy_records import PolicyDataset
from chip_chat.harvest.sources.chipotle.records import (
    DEFAULT_PARSED_PREFIX,
    MenuDataset,
)
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport

DATASETS = ("menu", "nutrition", "policy", "pdf", "all")
"""What ``--dataset`` accepts. ``menu`` is the default so that the command in
issue #19's documentation keeps meaning what it meant. ``pdf`` comes last
because it reads what the other three landed."""

ENDPOINT_VARIABLE = "CHIP_CHAT_DOCUMENT_INTELLIGENCE_ENDPOINT"
"""Where the Document Intelligence endpoint is read from when no flag gives
one. It is a Terraform output — a hostname, not a credential."""


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
            builder = _Builder(harvester, analyzer, blobs, args, restaurants)
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


class _Builder:
    """Builds datasets, and lends each one what the next needs.

    The PDF dataset of issue #22 reads the documents the other three landed and
    reconciles what it finds against the menu and nutrition tables, so asking
    for it alone still builds those two. They share this run's cache, so that
    costs the same seven documents either way.
    """

    def __init__(
        self,
        harvester: Harvester | None,
        analyzer: AzureDocumentIntelligence | None,
        blobs: LocalBlobStore,
        args: argparse.Namespace,
        restaurants: list[str],
    ) -> None:
        self._harvester = harvester
        self._analyzer = analyzer
        self._blobs = blobs
        self._cache = DocumentCache(blobs)
        self._args = args
        self._restaurants = restaurants
        self._menu: tuple[MenuDocuments, MenuDataset] | None = None
        self._nutrition: tuple[NutritionDocuments, NutritionDataset] | None = None
        self._policy: tuple[PolicyDocuments, PolicyDataset] | None = None

    def dataset(
        self, name: str
    ) -> MenuDataset | NutritionDataset | PolicyDataset | PdfDataset:
        """Return one dataset by name, building whatever it depends on."""
        if name == "menu":
            return self.menu()[1]
        if name == "nutrition":
            return self.nutrition()[1]
        if name == "policy":
            return self.policy()[1]
        return self.pdf()

    def menu(self) -> tuple[MenuDocuments, MenuDataset]:
        """Return the menu documents and the dataset parsed from them."""
        if self._menu is None:
            home = self._args.home_url
            documents = (
                load_menu(self._cache, self._restaurants, home_url=home)
                if self._harvester is None
                else harvest_menu(self._harvester, self._restaurants, home_url=home)
            )
            self._menu = (documents, parse_menu(documents))
        return self._menu

    def nutrition(self) -> tuple[NutritionDocuments, NutritionDataset]:
        """Return the nutrition documents and the dataset parsed from them."""
        if self._nutrition is None:
            home = self._args.home_url
            documents = (
                load_nutrition(self._cache, self._restaurants, home_url=home)
                if self._harvester is None
                else harvest_nutrition(self._harvester, self._restaurants, home_url=home)
            )
            self._nutrition = (documents, parse_nutrition(documents))
        return self._nutrition

    def policy(self) -> tuple[PolicyDocuments, PolicyDataset]:
        """Return the policy documents and the dataset parsed from them."""
        if self._policy is None:
            home = self._args.home_url
            stores = self._args.stores
            documents = (
                load_policy(self._cache, home_url=home, store_count=stores)
                if self._harvester is None
                else harvest_policy(self._harvester, home_url=home, store_count=stores)
            )
            self._policy = (documents, parse_policy(documents))
        return self._policy

    def pdf(self) -> PdfDataset:
        """Find the PDFs among everything harvested, read them, and check them.

        The search covers the menu and nutrition documents always, and the
        policy documents when this run built them too — searching for a
        nutrition sheet is not a reason to fetch four thousand store pages.

        Raises:
            DocumentAnalysisError: If a PDF was found and there is no endpoint
                to read it with, or none of it has been read before. An issue
                #22 dataset that quietly omitted a sheet it had in hand would
                fail the criterion in the one way nobody would notice.
        """
        menu_documents, menu = self.menu()
        nutrition_documents, nutrition = self.nutrition()
        groups = [menu_documents.documents(), nutrition_documents.documents()]
        if self._policy is not None:
            groups.append(self._policy[0].documents())
        searched = documents_of(*groups)

        found = (
            load_pdfs(self._cache, searched)
            if self._harvester is None
            else harvest_pdfs(self._harvester, searched)
        )
        if self._analyzer is None:
            if found.pdfs and not self._args.offline:
                raise DocumentAnalysisError(
                    found.pdfs[0].source_url,
                    f"a PDF was found and there is nowhere to read it: pass "
                    f"--document-intelligence-endpoint or set ${ENDPOINT_VARIABLE}",
                )
            analyzed = cached_analyses(
                found.pdfs, self._analyses(), DEFAULT_MODEL_ID, DEFAULT_API_VERSION
            )
        else:
            analyzed = analyze_pdfs(found.pdfs, self._analyzer, self._analyses())
        return parse_pdfs(found, analyzed, menu, nutrition)

    def _analyses(self) -> AnalysisCache:
        """Return the analysis cache, which lives beside the raw bytes."""
        return AnalysisCache(self._blobs)


if __name__ == "__main__":
    raise SystemExit(main())
