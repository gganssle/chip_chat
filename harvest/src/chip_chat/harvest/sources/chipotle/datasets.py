"""Building the four Chipotle datasets, and lending each one what the next needs.

This was the ``_Builder`` inside ``__main__.py`` and moved here unchanged when
issue #38 needed a second command — the weekly re-harvest in
:mod:`chip_chat.harvest.sources.chipotle.reharvest` — to build the same four
datasets the same way. Two commands that each assembled the datasets themselves
would be two commands that could disagree about what a harvest is, and the
first symptom of that would be a change report describing a corpus nobody
publishes.

The dependency it exists to manage is real: the PDF dataset of issue #22 reads
the documents the other three landed and reconciles what it finds against the
menu and nutrition tables, so asking for it alone still builds those two. They
share one cache and one politeness gate, so that costs the same seven documents
either way.
"""

from chip_chat.harvest.analysis import (
    DEFAULT_API_VERSION,
    DEFAULT_MODEL_ID,
    AnalysisCache,
    AzureDocumentIntelligence,
)
from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import DocumentAnalysisError
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
from chip_chat.harvest.sources.chipotle.pdf_records import PdfDataset
from chip_chat.harvest.sources.chipotle.policy import (
    PolicyDocuments,
    harvest_policy,
    load_policy,
)
from chip_chat.harvest.sources.chipotle.policy_parse import parse_policy
from chip_chat.harvest.sources.chipotle.policy_records import PolicyDataset
from chip_chat.harvest.sources.chipotle.records import MenuDataset

DATASETS = ("menu", "nutrition", "policy", "pdf")
"""The four datasets, in dependency order. ``pdf`` reads what the others land."""

ENDPOINT_VARIABLE = "CHIP_CHAT_DOCUMENT_INTELLIGENCE_ENDPOINT"
"""Where the Document Intelligence endpoint is read from when no flag gives
one. It is a Terraform output — a hostname, not a credential."""

Dataset = MenuDataset | NutritionDataset | PolicyDataset | PdfDataset


class DatasetBuilder:
    """Builds datasets, and lends each one what the next needs."""

    def __init__(
        self,
        harvester: Harvester | None,
        analyzer: AzureDocumentIntelligence | None,
        blobs: BlobStore,
        *,
        restaurants: list[str] | None = None,
        home_url: str = HOME_URL,
        store_count: int = DEFAULT_STORE_COUNT,
        offline: bool = False,
        refresh: bool = False,
    ) -> None:
        """Initialise the builder.

        Args:
            harvester: The framework instance doing the fetching, or ``None``
                to build every dataset out of the cache and make no requests.
            analyzer: Document Intelligence, or ``None`` when there is nowhere
                to send a PDF.
            blobs: The landing zone.
            restaurants: Restaurants to price the catalogue at. Defaults to
                :data:`~chip_chat.harvest.sources.chipotle.menu.DEFAULT_RESTAURANT_IDS`.
            home_url: The page the API configuration is read from.
            store_count: How many stores the policy dataset reads.
            offline: Whether this run is forbidden to fetch. Distinct from
                ``harvester is None`` only in what it makes the PDF dataset do
                about a sheet it cannot read.
            refresh: Ask the source again for every document rather than
                trusting the cache. What issue #38's weekly re-harvest passes.
        """
        self._harvester = harvester
        self._analyzer = analyzer
        self._blobs = blobs
        self._cache = DocumentCache(blobs)
        self._restaurants = list(restaurants or DEFAULT_RESTAURANT_IDS)
        self._home_url = home_url
        self._store_count = store_count
        self._offline = offline
        self._refresh = refresh
        self._menu: tuple[MenuDocuments, MenuDataset] | None = None
        self._nutrition: tuple[NutritionDocuments, NutritionDataset] | None = None
        self._policy: tuple[PolicyDocuments, PolicyDataset] | None = None

    @property
    def restaurants(self) -> list[str]:
        """The restaurants this run prices the catalogue at."""
        return list(self._restaurants)

    def dataset(self, name: str) -> Dataset:
        """Return one dataset by name, building whatever it depends on.

        Args:
            name: One of :data:`DATASETS`.

        Returns:
            The dataset.

        Raises:
            KeyError: If ``name`` is not one of the four.
        """
        if name not in DATASETS:
            raise KeyError(f"unknown dataset {name!r}; expected one of {DATASETS}")
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
            documents = (
                load_menu(self._cache, self._restaurants, home_url=self._home_url)
                if self._harvester is None
                else harvest_menu(
                    self._harvester,
                    self._restaurants,
                    home_url=self._home_url,
                    refresh=self._refresh,
                )
            )
            self._menu = (documents, parse_menu(documents))
        return self._menu

    def nutrition(self) -> tuple[NutritionDocuments, NutritionDataset]:
        """Return the nutrition documents and the dataset parsed from them."""
        if self._nutrition is None:
            documents = (
                load_nutrition(self._cache, self._restaurants, home_url=self._home_url)
                if self._harvester is None
                else harvest_nutrition(
                    self._harvester,
                    self._restaurants,
                    home_url=self._home_url,
                    refresh=self._refresh,
                )
            )
            self._nutrition = (documents, parse_nutrition(documents))
        return self._nutrition

    def policy(self) -> tuple[PolicyDocuments, PolicyDataset]:
        """Return the policy documents and the dataset parsed from them."""
        if self._policy is None:
            documents = (
                load_policy(
                    self._cache,
                    home_url=self._home_url,
                    store_count=self._store_count,
                )
                if self._harvester is None
                else harvest_policy(
                    self._harvester,
                    home_url=self._home_url,
                    store_count=self._store_count,
                    refresh=self._refresh,
                )
            )
            self._policy = (documents, parse_policy(documents))
        return self._policy

    def pdf(self) -> PdfDataset:
        """Find the PDFs among everything harvested, read them, and check them.

        The search covers the menu and nutrition documents always, and the
        policy documents when this run built them too — searching for a
        nutrition sheet is not a reason to fetch four thousand store pages.

        Returns:
            The dataset.

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
            else harvest_pdfs(self._harvester, searched, refresh=self._refresh)
        )
        if self._analyzer is None:
            if found.pdfs and not self._offline:
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
