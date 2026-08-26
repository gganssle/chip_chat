"""Finding Chipotle's PDFs, landing them, and having them read.

The same two entry points as the other three datasets, for the same reason:
:func:`harvest_pdfs` goes through the framework and may fetch, and
:func:`load_pdfs` reads the cache and has no transport in its signature to
reach a network with even by accident.

**The PDFs are discovered, not listed.** Nothing here holds a remembered
nutrition-sheet URL. Every document the other datasets already harvested is
re-read for links whose path ends in ``.pdf``, and those are what get fetched —
so a sheet that appears next month is picked up by the next harvest, and a
sheet that is withdrawn stops being harvested rather than turning into a 404 in
a hardcoded list. It also means the answer today is *none*: on 26 August 2026
none of the pages this project harvests published a single PDF link. See
``docs/chipotle-pdf-spot-check.md``.

**And a link that ends in ``.pdf`` is not a PDF.** What lands is checked
against ``%PDF-`` before anything is sent to Azure, because a site that answers
a stale link with its HTML error page would otherwise buy a structured
extraction of the words "page not found" — and, worse, file it as nutrition
data. The URL is still recorded as discovered; it is simply not a document.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from chip_chat.harvest.analysis import (
    AnalysisCache,
    DocumentAnalysis,
    DocumentAnalyzer,
    analyze_once,
)
from chip_chat.harvest.cache import CachedDocument, DocumentCache
from chip_chat.harvest.clock import Clock
from chip_chat.harvest.errors import (
    DocumentAnalysisError,
    PermanentFetchError,
    RobotsDisallowedError,
)
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.layout import is_pdf

_LINK_ATTRIBUTES = ("href", "src")


class _PdfLinkCollector(HTMLParser):
    """Collects every link on a page whose path ends in ``.pdf``.

    Deliberately blind to which tag the link is on. A nutrition sheet is as
    likely to be linked from a ``<link>`` or an ``<iframe>`` as from an
    ``<a>``, and a collector tuned to today's markup is one content edit away
    from finding nothing and saying nothing about it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in _LINK_ATTRIBUTES and value and _looks_like_pdf(value):
                self.links.append(value)


def _looks_like_pdf(reference: str) -> bool:
    """Return whether a URL reference's *path* ends in ``.pdf``.

    The query is ignored on purpose: ``sheet.pdf?v=3`` is a PDF and
    ``search?q=pdf`` is not.
    """
    return urlsplit(reference.strip()).path.lower().endswith(".pdf")


def discover_pdf_urls(documents: Iterable[CachedDocument]) -> tuple[str, ...]:
    """Return every PDF URL linked from the given harvested documents.

    Args:
        documents: Documents a harvest already landed. Anything that is not
            HTML is skipped — a JSON menu endpoint has no links to follow, and
            a PDF does not link to itself.

    Returns:
        Absolute ``http`` and ``https`` URLs, deduplicated and sorted, so that
        two runs over the same cache discover the same list in the same order.
    """
    found: set[str] = set()
    for document in documents:
        if is_pdf(document.content):
            continue
        if "html" not in document.content_type.lower():
            continue
        collector = _PdfLinkCollector()
        collector.feed(document.text)
        collector.close()
        for reference in collector.links:
            absolute = urljoin(document.source_url, reference.strip())
            if urlsplit(absolute).scheme in ("http", "https"):
                found.add(absolute)
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class PdfDocuments:
    """The PDFs a harvest found, and what happened to the links that were not.

    Attributes:
        discovered_urls: Every URL that looked like a PDF, in the order
            :func:`discover_pdf_urls` returned it.
        pdfs: Those whose bytes actually begin ``%PDF-``.
        rejected_urls: Those that were read and were not PDFs after all — a
            stale link answered with an HTML error page.
        unread_urls: Those that could not be read at all: a 404, a path
            ``robots.txt`` forbids, or, offline, one that no harvest has
            fetched yet.

    The last two are kept apart because they call for different responses.
    "Chipotle now serves that link as HTML" is a change at the source worth
    looking at; "this landing zone predates the link" is a re-run.
    """

    discovered_urls: tuple[str, ...]
    pdfs: tuple[CachedDocument, ...]
    rejected_urls: tuple[str, ...]
    unread_urls: tuple[str, ...] = ()


def harvest_pdfs(
    harvester: Harvester,
    documents: Iterable[CachedDocument],
    *,
    refresh: bool = False,
) -> PdfDocuments:
    """Discover the PDFs linked from ``documents`` and land them.

    Every request goes through ``harvester``, so a second run over a warm
    landing zone makes no network calls at all.

    Args:
        harvester: The framework instance doing the fetching.
        documents: The already-harvested documents to look for links in.
        refresh: Ask the source again for every document, conditionally
            where the cache holds a validator. This is what the weekly
            re-harvest of issue #38 passes: without it a warm landing zone is
            never revisited, and the corpus quietly stops being current.

    Returns:
        What was found.

    Raises:
        RobotsDisallowedError: Never — a disallowed PDF is recorded as
            rejected rather than stopping the harvest, because one
            unreachable sheet is not a reason to abandon a dataset. Every
            other failure mode of :meth:`Harvester.fetch` propagates.
    """

    def fetch(url: str) -> CachedDocument | None:
        try:
            return harvester.fetch(url, refresh=refresh)
        except (PermanentFetchError, RobotsDisallowedError):
            return None

    return _collect(fetch, documents)


def load_pdfs(
    cache: DocumentCache,
    documents: Iterable[CachedDocument],
) -> PdfDocuments:
    """Read the PDFs back out of the cache, offline.

    Args:
        cache: The cache a previous harvest wrote to.
        documents: The already-harvested documents to look for links in.

    Returns:
        What was found. A discovered URL that was never harvested lands in
        ``unread_urls`` rather than raising, because an offline parse of a
        landing zone that predates the link should still produce a dataset —
        and the manifest will say the URL was discovered and not read.
    """

    def fetch(url: str) -> CachedDocument | None:
        return cache.get(url)

    return _collect(fetch, documents)


def _collect(
    fetch: Callable[[str], CachedDocument | None],
    documents: Iterable[CachedDocument],
) -> PdfDocuments:
    """Assemble the PDF set, however ``fetch`` chooses to obtain one."""
    discovered = discover_pdf_urls(documents)
    pdfs: list[CachedDocument] = []
    rejected: list[str] = []
    unread: list[str] = []
    for url in discovered:
        document = fetch(url)
        if document is None:
            unread.append(url)
        elif is_pdf(document.content):
            pdfs.append(document)
        else:
            rejected.append(url)
    return PdfDocuments(
        discovered_urls=discovered,
        pdfs=tuple(pdfs),
        rejected_urls=tuple(rejected),
        unread_urls=tuple(unread),
    )


def analyze_pdfs(
    pdfs: Sequence[CachedDocument],
    analyzer: DocumentAnalyzer,
    cache: AnalysisCache,
    *,
    clock: Clock | None = None,
) -> tuple[tuple[CachedDocument, DocumentAnalysis], ...]:
    """Return each PDF with its Document Intelligence analysis.

    A cached analysis is used without calling Azure, which is what makes
    iterating on the parser free — the same bargain the document cache strikes
    with the source site, struck again with a paid API.

    Args:
        pdfs: The documents to analyse.
        analyzer: The seam to Document Intelligence.
        cache: Where analyses are kept.
        clock: Source of ``analyzed_at``.

    Returns:
        ``(document, analysis)`` pairs, in the order given.

    Raises:
        DocumentAnalysisError: If the service refused or failed on any of them.
    """
    return tuple(
        (
            document,
            analyze_once(
                document.content,
                document.content_sha256,
                analyzer,
                cache,
                clock=clock,
            ),
        )
        for document in pdfs
    )


def cached_analyses(
    pdfs: Sequence[CachedDocument],
    cache: AnalysisCache,
    model_id: str,
    api_version: str,
) -> tuple[tuple[CachedDocument, DocumentAnalysis], ...]:
    """Return each PDF with its cached analysis, calling nothing.

    The offline counterpart of :func:`analyze_pdfs`.

    Args:
        pdfs: The documents to look up.
        cache: Where analyses are kept.
        model_id: The model whose analyses to read.
        api_version: The API version whose analyses to read.

    Returns:
        ``(document, analysis)`` pairs, in the order given.

    Raises:
        DocumentAnalysisError: If any PDF has no cached analysis. Returning a
            dataset that silently omitted a sheet already in hand would break
            issue #22's first criterion in the one way nobody would notice.
    """
    analyzed: list[tuple[CachedDocument, DocumentAnalysis]] = []
    for document in pdfs:
        analysis = cache.get(document.content_sha256, model_id, api_version)
        if analysis is None:
            raise DocumentAnalysisError(
                document.source_url,
                f"no {model_id}/{api_version} analysis is cached; "
                f"run the harvest online once to have it read",
            )
        analyzed.append((document, analysis))
    return tuple(analyzed)


def documents_of(
    *groups: Iterable[CachedDocument],
) -> tuple[CachedDocument, ...]:
    """Flatten several document groups into one deduplicated sequence.

    The three existing datasets each hold their documents in their own frozen
    dataclass, and the PDF search wants all of them at once. Deduplication is
    by requested URL, because the nutrition dataset and the menu dataset
    deliberately share the same home page.

    Args:
        *groups: Iterables of cached documents.

    Returns:
        The documents, deduplicated, in first-seen order.
    """
    seen: dict[str, CachedDocument] = {}
    for group in groups:
        for document in group:
            seen.setdefault(document.requested_url, document)
    return tuple(seen.values())
