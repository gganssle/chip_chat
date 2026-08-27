"""Hold the live index to #48's third and fourth acceptance criteria.

    #48.3  an alias swap is atomic from the application's point of view --
           verified by querying continuously across a swap
    #48.4  a deliberately failed build leaves the live alias pointing at the
           previous good index

Both are claims about *time*, and the issue asks for them to be verified rather
than argued. So this runs against the real service and the real embedding
deployment: it builds one index, swaps to it, then builds a second while a
second thread queries the alias about fifty times a second, and finally runs a
build that fails on purpose and asks the alias what it is serving.

``search/tests/test_build.py`` asserts the same two properties in CI against a
fake service, and the two are not redundant. The fake proves the *build* is
correct against a model of the service. This proves the model is right — that
the service really does keep answering across an alias write, and really does
reject a bad document per-document rather than failing the batch.

**What "atomic" is being claimed.** Not that the swap is instantaneous: Azure AI
Search documents alias changes as taking up to ten seconds to propagate, and
this run measures that window rather than pretending it is zero. The claim is
the one the application cares about — that during the window every query
succeeds, and every response comes entirely from one index or entirely from the
other. There is no moment at which the corpus is half-updated, which is the
property RFC-001 §08 asks for and the one a partial harvest would otherwise
break. The two builds are deliberately given a **different document count** so
that a response cannot be ambiguous about which index answered it.

This costs a few hundred embedding tokens and about a minute. It is not in
``make ci`` and could not be: it needs an Azure credential, a live service and a
model deployment, and a gate that needs a credential is not a gate.
"""

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chip_chat.search import build as build_module
from chip_chat.search import schema
from chip_chat.search.chunks import CHUNK_ID
from chip_chat.search.client import SearchService
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.embedding import Embedder, EmbeddingDeployment
from chip_chat.search.errors import SearchError

__all__ = [
    "FailedBuildResult",
    "Observation",
    "SwapResult",
    "SwapWatch",
    "check_failed_build",
    "check_swap",
    "check_vectorization",
]


@dataclass(frozen=True, slots=True)
class Observation:
    """One query issued against the alias while something else was happening.

    Attributes:
        at: Monotonic time the response came back.
        count: How many documents the alias reported, or ``None`` on failure.
        error: What went wrong, if anything.
    """

    at: float
    count: int | None
    error: str | None = None


class SwapWatch:
    """Queries an alias continuously on a thread, and remembers every answer."""

    def __init__(
        self, service: SearchService, alias: str, interval: float = 0.02
    ) -> None:
        """Initialise the watch.

        Args:
            service: The search service.
            alias: The alias to query. Never an index name — the whole point is
                that the application only ever knows this name.
            interval: Seconds between queries.
        """
        self._service = service
        self._alias = alias
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.observations: list[Observation] = []

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                response = self._service.search(
                    self._alias, {"search": "*", "count": True, "top": 0}
                )
                self.observations.append(
                    Observation(time.monotonic(), int(response["@odata.count"]))
                )
            except Exception as error:
                self.observations.append(Observation(time.monotonic(), None, str(error)))
            time.sleep(self._interval)

    def __enter__(self) -> "SwapWatch":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


@dataclass
class SwapResult:
    """What querying across a swap saw.

    Attributes:
        before: The document count the alias served before the swap.
        after: The count it served afterwards.
        queries: How many queries were issued across the window.
        failures: Queries that did not return a result.
        strays: Counts that were neither ``before`` nor ``after`` — a response
            assembled from two corpora, which is the thing that must not happen.
        window: Seconds between the swap call returning and the first response
            from the new index.
    """

    before: int
    after: int
    queries: int
    failures: tuple[Observation, ...] = ()
    strays: tuple[Observation, ...] = ()
    window: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """Whether the swap was atomic from the application's point of view."""
        return not self.failures and not self.strays and self.before != self.after

    def render(self) -> str:
        """Return the lines ``make search-verify`` prints."""
        verdict = "atomic" if self.passed else "NOT ATOMIC"
        lines = [
            f"#48.3  alias swap: {verdict}",
            f"  documents    {self.before} -> {self.after}",
            f"  queries      {self.queries} across the swap",
            f"  failures     {len(self.failures)}",
            f"  half-updated {len(self.strays)}",
            f"  propagation  {self.window:.2f}s from the alias write to the "
            f"first response from the new index",
        ]
        lines.extend(f"  note         {note}" for note in self.notes)
        for observation in (*self.failures[:3], *self.strays[:3]):
            lines.append(f"  saw          {observation}")
        return "\n".join(lines)


def check_swap(
    service: SearchService,
    first: ChunkSet,
    second: ChunkSet,
    deployment: EmbeddingDeployment,
    embedder: Embedder,
    *,
    alias: str = schema.ALIAS,
    vectorizer_key: str | None,
    settle: float = 20.0,
) -> SwapResult:
    """Query the alias continuously while a second index is swapped in.

    Args:
        service: The search service.
        first: The corpus to serve before the swap.
        second: The corpus to serve after it. Must differ in **size** from
            ``first``, so that every response says unambiguously which index
            answered it.
        deployment: The embedding deployment.
        embedder: What turns chunk text into vectors.
        alias: The alias under test.
        vectorizer_key: Passed to the builds.
        settle: Seconds to keep querying after the swap, to see the propagation
            through. Twice the ten seconds the service documents.

    Returns:
        What the watch saw.

    Raises:
        ValueError: If the two corpora are the same size, which would make
            every observation ambiguous and the check vacuous.
        chip_chat.search.errors.SearchError: If either build fails. A build
            that fails here has not swapped, so the alias is unchanged and the
            run can be repeated.
    """
    if len(first) == len(second):
        raise ValueError(
            f"both corpora hold {len(first)} chunks, so a response cannot say "
            f"which index answered it and the check would pass without "
            f"observing anything"
        )
    build_module.build(
        service,
        first,
        deployment,
        embedder,
        alias=alias,
        vectorizer_key=vectorizer_key,
    )
    # Let the first alias write propagate before measuring the second, so the
    # window this reports is the swap's and not the setup's.
    time.sleep(settle / 2)

    with SwapWatch(service, alias) as watch:
        time.sleep(1.0)
        report = build_module.build(
            service,
            second,
            deployment,
            embedder,
            alias=alias,
            vectorizer_key=vectorizer_key,
        )
        swapped_at = time.monotonic()
        time.sleep(settle)

    observations = tuple(watch.observations)
    before, after = len(first), len(second)
    arrived = [
        observation
        for observation in observations
        if observation.count == after and observation.at >= swapped_at
    ]
    notes: list[str] = [f"index {report.index} replaced {report.previous}"]
    if not arrived:
        notes.append(
            f"the new corpus never arrived within {settle:.0f}s, which is "
            f"longer than the ten seconds the service documents"
        )
    return SwapResult(
        before=before,
        after=after,
        queries=len(observations),
        failures=tuple(o for o in observations if o.error is not None),
        strays=tuple(
            o for o in observations if o.error is None and o.count not in (before, after)
        ),
        window=0.0 if not arrived else arrived[0].at - swapped_at,
        notes=tuple(notes),
    )


@dataclass
class FailedBuildResult:
    """What a deliberately failed build left behind.

    Attributes:
        alias: The alias under test.
        served_before: What it pointed at before the failed build.
        served_after: What it points at now.
        answered: How many documents the alias still serves.
        failure: The error the build raised.
        remains: The failed build's index, if it is still there to be read.
    """

    alias: str
    served_before: str | None
    served_after: str | None
    answered: int
    failure: str
    remains: str | None

    @property
    def passed(self) -> bool:
        """Whether the live corpus was untouched by the failure."""
        return (
            self.served_before is not None
            and self.served_after == self.served_before
            and self.answered > 0
        )

    def render(self) -> str:
        """Return the lines ``make search-verify`` prints."""
        verdict = "held" if self.passed else "DID NOT HOLD"
        return "\n".join(
            [
                f"#48.4  failed build: the live alias {verdict}",
                f"  {self.alias} -> {self.served_after}",
                f"  before       {self.served_before}",
                f"  serving      {self.answered} documents",
                f"  failure      {self.failure[:200]}",
                f"  remains      {self.remains or '(deleted with its build)'}",
            ]
        )


def check_failed_build(
    service: SearchService,
    chunk_set: ChunkSet,
    deployment: EmbeddingDeployment,
    embedder: Embedder,
    *,
    alias: str = schema.ALIAS,
    vectorizer_key: str | None,
) -> FailedBuildResult:
    """Run a build that cannot finish, and ask the alias what it is serving.

    The failure is injected at the **document** rather than at the client: the
    last chunk is given an empty ``chunk_id``, which is a key the service
    rejects and this package does not. Measured on 2026-08-27, the service
    fails the whole *request* it appears in — ``400 InvalidName, actions : 30:
    Document key cannot be missing or empty`` — rather than reporting it per
    document, so the documents in earlier requests are in and the ones beside it
    are not. That is why ``make search-verify`` lowers the service client's
    upload batch: at the default of 1000 this corpus is a single request, the
    index ends up empty, and "the load died halfway" is not what was
    demonstrated.

    Args:
        service: The search service.
        chunk_set: A corpus to fail a build with. Must hold at least two chunks.
        deployment: The embedding deployment.
        embedder: What turns chunk text into vectors.
        alias: The alias under test.
        vectorizer_key: Passed to the build.

    Returns:
        What the alias was serving afterwards.

    Raises:
        ValueError: If the corpus is too small to be partially loaded, or if
            the alias is not already serving something — "the previous good
            index" has to exist for this criterion to mean anything.
    """
    if len(chunk_set) < 2:
        raise ValueError("a partial load needs at least two chunks")
    served_before = service.alias_target(alias)
    if served_before is None:
        raise ValueError(
            f"{alias} is not serving anything, so there is no previous good "
            f"index for a failed build to leave in place"
        )

    poisoned = ChunkSet(
        run_id=chunk_set.run_id,
        rows=tuple(
            {**row, CHUNK_ID: ""} if position == len(chunk_set.rows) - 1 else row
            for position, row in enumerate(chunk_set.rows)
        ),
        origin=f"{chunk_set.origin} (one key emptied on purpose)",
    )
    remains: str | None = None
    failure = "the build did not fail, which is itself the finding"
    before = set(service.index_names())
    try:
        build_module.build(
            service,
            poisoned,
            deployment,
            embedder,
            alias=alias,
            vectorizer_key=vectorizer_key,
        )
    except SearchError as error:
        failure = str(error)
        remains = next(iter(sorted(set(service.index_names()) - before)), None)

    served_after = service.alias_target(alias)
    answered = 0 if served_after is None else service.document_count(alias)
    return FailedBuildResult(
        alias=alias,
        served_before=served_before,
        served_after=served_after,
        answered=answered,
        failure=failure,
        remains=remains,
    )


def check_vectorization(
    service: SearchService, alias: str, question: str
) -> Mapping[str, Any]:
    """Ask the alias a question **as text** and see whether it vectorizes it.

    The one check that proves query-time integrated vectorization is really
    configured: the request carries no vector, so if hits come back the service
    embedded the question itself, with the deployment named on the index.

    Args:
        service: The search service.
        alias: The alias to ask.
        question: A question in words.

    Returns:
        The number of hits and the first chunk's citation, so the caller can
        see that a source came back with it.

    Raises:
        chip_chat.search.errors.SearchError: If the service refuses, which on
            this index means the vectorizer is absent or its key is stale.
    """
    response = service.search(
        alias,
        {
            "search": question,
            "top": 3,
            "vectorQueries": [
                {
                    "kind": "text",
                    "text": question,
                    "fields": schema.VECTOR_FIELD,
                    "k": 10,
                }
            ],
            "select": "chunk_id,heading,source_url,harvested_at",
        },
    )
    hits: Sequence[Mapping[str, Any]] = response.get("value", [])
    return {
        "question": question,
        "hits": len(hits),
        "first": dict(hits[0]) if hits else None,
    }
