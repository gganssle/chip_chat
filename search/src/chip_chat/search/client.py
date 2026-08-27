"""The search service, narrowed to the nine calls a rebuild makes.

:class:`SearchService` is a protocol, and that is the load-bearing decision in
this module. #48's third and fourth acceptance criteria are about *behaviour
under failure* — an alias swap that is atomic from the application's point of
view, and a deliberately failed build that leaves the live alias where it was —
and behaviour under failure is exactly what a live service is worst at
demonstrating on demand. ``search/tests/fake.py`` implements this protocol over
three dicts, so ``test_build.py`` can fail a build at the fortieth document of
sixty and assert what the alias points at afterwards, in CI, every time, for
free. ``make search-verify`` then does the same thing once against the real
service, which is a different and weaker kind of evidence: it says the model in
the fake matches the service, and the fake says the build is correct against the
model.

**Authentication is Entra and there is no key path.** ``search.tf`` sets
``local_authentication_enabled = false``, so admin keys do not exist on this
service — there is nothing to put in an environment variable and nothing to leak.
:class:`EntraToken` resolves to the app's user-assigned identity in Azure and to
``az login`` on a laptop, which is the same code path in both places. A developer
running ``make search-build`` needs the **Search Index Data Contributor** and
**Search Service Contributor** roles on the service; ``search.tf`` grants them,
and without them every call here returns 403 while the portal keeps working,
which is a confusing way to spend an afternoon.
"""

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Protocol

from chip_chat.search.errors import SearchError
from chip_chat.search.schema import API_VERSION

__all__ = [
    "KEEPALIVE_SECONDS",
    "SEARCH_SCOPE",
    "UPLOAD_BATCH_LIMIT",
    "EntraToken",
    "HttpSearchService",
    "SearchService",
    "ServiceError",
    "UploadError",
    "endpoint_from_env",
    "pooled_client",
]

SEARCH_SCOPE = "https://search.azure.com/.default"
"""Entra scope for the search data plane. Not the management-plane scope, and
not the one the control-plane calls in this module use either — index and alias
management on the data-plane endpoint is authorised by the same token."""

UPLOAD_BATCH_LIMIT = 1000
"""Documents per ``/docs/index`` request, the service's own ceiling."""


class ServiceError(SearchError):
    """The search service refused, or answered something unreadable."""


class UploadError(SearchError):
    """One or more documents in a batch were not indexed.

    Carried as its own type because a partial upload is the failure the whole
    rebuild-never-patch design exists to survive: the batch is reported
    per-document, most of it succeeded, and the build must still treat the whole
    load as failed rather than swap an alias onto a corpus with holes in it.

    Attributes:
        failures: ``(key, message)`` for each document the service rejected.
    """

    def __init__(self, failures: Sequence[tuple[str, str]]) -> None:
        self.failures = tuple(failures)
        shown = "; ".join(f"{key}: {message}" for key, message in self.failures[:5])
        more = "" if len(self.failures) <= 5 else f" (+{len(self.failures) - 5} more)"
        super().__init__(
            f"{len(self.failures)} documents were not indexed: {shown}{more}"
        )


class SearchService(Protocol):
    """Every call a rebuild makes, and no others."""

    def index_names(self) -> list[str]:
        """Return the names of every index on the service."""
        ...

    def create_index(self, definition: Mapping[str, Any]) -> None:
        """Create the index ``definition`` describes."""
        ...

    def delete_index(self, name: str) -> None:
        """Delete an index. A no-op if it is not there."""
        ...

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """Index ``documents``, raising :class:`UploadError` on any rejection."""
        ...

    def document_count(self, index: str) -> int:
        """Return how many documents an index holds."""
        ...

    def alias_target(self, alias: str) -> str | None:
        """Return the index ``alias`` points at, or ``None`` if it does not exist."""
        ...

    def set_alias(self, alias: str, index: str) -> None:
        """Point ``alias`` at ``index``, creating the alias if it is new."""
        ...

    def delete_alias(self, alias: str) -> None:
        """Delete an alias. A no-op if it is not there."""
        ...

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        """Run a query against an index or an alias and return the response."""
        ...


class TokenSource(Protocol):
    """Returns a bearer token for a data plane."""

    def token(self) -> str:
        """Return a currently valid bearer token."""
        ...


class EntraToken:
    """A :class:`TokenSource` over ``DefaultAzureCredential``.

    The credential is built on first use rather than at construction, so that
    importing this module — which ``make ci`` does — never reaches for a
    credential chain that is not there.
    """

    def __init__(self, scope: str = SEARCH_SCOPE) -> None:
        """Initialise the token source.

        Args:
            scope: The Entra scope to request.
        """
        self._scope = scope
        self._credential: Any | None = None

    def token(self) -> str:
        """Return a bearer token for the scope.

        Returns:
            The token.

        Raises:
            ServiceError: If no credential in the chain could produce one.
        """
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        try:
            return str(self._credential.get_token(self._scope).token)
        except Exception as error:
            raise ServiceError(
                f"could not get an Entra token for {self._scope}: {error}. "
                f"On a laptop this is usually `az login`."
            ) from error


class HttpSearchService:
    """The real :class:`SearchService`, over the REST data plane."""

    def __init__(
        self,
        endpoint: str,
        client: Any,
        token: TokenSource,
        batch: int = UPLOAD_BATCH_LIMIT,
    ) -> None:
        """Initialise the service client.

        Args:
            endpoint: ``https://<name>.search.windows.net``.
            client: An ``httpx.Client``. Injected so one connection pool serves
                the whole build, and so a test can supply a transport.
            token: Where bearer tokens come from.
            batch: Documents per ``/docs/index`` request. The service's own
                ceiling by default. ``make search-verify`` lowers it, because a
                corpus that fits in one request cannot demonstrate a *partial*
                load and a partial load is what #48.4 is about.
        """
        self._endpoint = endpoint.rstrip("/")
        self._client = client
        self._token = token
        self._batch = max(1, batch)

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        expected: Sequence[int] = (200, 201, 204),
    ) -> Any:
        url = f"{self._endpoint}{path}"
        joiner = "&" if "?" in path else "?"
        try:
            response = self._client.request(
                method,
                f"{url}{joiner}api-version={API_VERSION}",
                json=None if body is None else dict(body),
                headers={
                    "Authorization": f"Bearer {self._token.token()}",
                    "Content-Type": "application/json",
                },
            )
        except ServiceError:
            raise
        except Exception as error:
            # A refused connection, a DNS failure or a timeout is *the service
            # being unavailable*, which is a row in RFC-001 section 10 with a
            # blast radius of one lane -- and a lane can only decline for a
            # failure it can catch. Without this, the one failure that row is
            # actually about arrives as an httpx exception, escapes
            # `KnowledgeLane`, and takes the turn with it. The client is
            # injected rather than imported, so the transport's exception types
            # are deliberately not nameable here.
            raise ServiceError(
                f"{method} {path} did not reach {self._endpoint}: "
                f"{type(error).__name__}: {error}"
            ) from error
        if response.status_code not in expected:
            raise ServiceError(
                f"{method} {path} returned {response.status_code}: {response.text[:600]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise ServiceError(f"{method} {path} did not answer JSON: {error}") from error

    def index_names(self) -> list[str]:
        payload = self._call("GET", "/indexes?$select=name", expected=(200,))
        return [str(entry["name"]) for entry in payload.get("value", [])]

    def create_index(self, definition: Mapping[str, Any]) -> None:
        self._call("POST", "/indexes", body=definition, expected=(200, 201))

    def delete_index(self, name: str) -> None:
        self._call("DELETE", f"/indexes/{name}", expected=(204, 404))

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        for batch in _batches(documents, self._batch):
            payload = self._call(
                "POST",
                f"/indexes/{index}/docs/index",
                body={"value": list(batch)},
                # 207 is the service saying "some of these failed", which is
                # exactly the case UploadError exists for. It is a success
                # status at the HTTP layer and a failure at this one.
                expected=(200, 207),
            )
            failures = [
                (str(entry.get("key", "")), str(entry.get("errorMessage", "")))
                for entry in payload.get("value", [])
                if not entry.get("status", False)
            ]
            if failures:
                raise UploadError(failures)

    def document_count(self, index: str) -> int:
        payload = self._call("GET", f"/indexes/{index}/docs/$count", expected=(200,))
        return int(payload)

    def alias_target(self, alias: str) -> str | None:
        payload = self._call("GET", f"/aliases/{alias}", expected=(200, 404))
        if payload is None or "indexes" not in payload:
            return None
        targets = payload["indexes"]
        return str(targets[0]) if targets else None

    def set_alias(self, alias: str, index: str) -> None:
        # 201 when the alias is new, **204 when it already existed** -- the
        # service treats the second write as an update with no body rather than
        # as a replacement that returns one. Measured, because (200, 201) is
        # the obvious guess and it fails on the second build rather than the
        # first, which is the worst moment to find out.
        self._call(
            "PUT",
            f"/aliases/{alias}",
            body={"name": alias, "indexes": [index]},
            expected=(200, 201, 204),
        )

    def delete_alias(self, alias: str) -> None:
        self._call("DELETE", f"/aliases/{alias}", expected=(204, 404))

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._call(
            "POST", f"/indexes/{target}/docs/search", body=query, expected=(200,)
        )
        return dict(payload)


def _batches(
    documents: Sequence[Mapping[str, Any]], size: int
) -> Iterator[Sequence[Mapping[str, Any]]]:
    for start in range(0, len(documents), size):
        yield documents[start : start + size]


KEEPALIVE_SECONDS = 300.0
"""How long an idle connection to the search service is kept open.

httpx's own default is five seconds, which is tuned for a service under load and
is wrong for this one. Measured from ``ca-chip-chat-web`` against
``srch-chip-chat-4cy39i`` on 2026-08-26:

    hybrid query, warm pooled connection    p50 11.2 ms   (p95 11.8, n=35)
    the same query, fresh TLS connection    p50 84.3 ms   (n=12)
    semantic reranking, on top of the above     ~30 ms
    the cross-region hop, eastus2 to eastus      6.8 ms

A cold connection costs seventy milliseconds -- **seven times** the region
penalty everyone reaches for first, and more than twice what the reranker costs.
A public demo with a visitor every few minutes would pay it on nearly every
turn at a five-second expiry. Five minutes is long enough that a conversation
never pays it twice and short enough that an idle app is not holding a socket
open all night.
"""


def pooled_client(timeout: float = 30.0) -> Any:
    """Return an ``httpx.Client`` whose pool outlives a single request.

    The one call that turns the latency measurement above into a property of the
    deployment. Build this **once per process** and hand it to one
    :class:`HttpSearchService`, which one
    :class:`~chip_chat.search.retrieve.Retriever` then holds: a client per turn
    is a TLS handshake per turn, and no amount of tuning further down recovers
    it.

    Args:
        timeout: Seconds before a request is abandoned. Generous next to an
            11 ms query, because the number that matters here is when to stop
            waiting on a service that has stopped answering -- and that is the
            outage path, which declines rather than retries.

    Returns:
        An ``httpx.Client``. Imported inside the function so that importing this
        module -- which ``make ci`` does -- costs nothing.
    """
    import httpx

    return httpx.Client(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=10,
            keepalive_expiry=KEEPALIVE_SECONDS,
        ),
    )


def endpoint_from_env(env: Mapping[str, str] | None = None) -> str:
    """Return the search endpoint from the environment.

    ``AZURE_SEARCH_ENDPOINT`` rather than a ``CHIP_CHAT_``-prefixed name,
    because ``compute.tf`` already sets that one on the container app and a
    second spelling of the same value is a second thing to keep in step.

    Args:
        env: The environment. Defaults to ``os.environ``.

    Returns:
        The endpoint, without a trailing slash.

    Raises:
        ServiceError: If it is not set.
    """
    source = os.environ if env is None else env
    endpoint = source.get("AZURE_SEARCH_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise ServiceError(
            "AZURE_SEARCH_ENDPOINT is not set. `make infra-output` prints it as "
            "search_endpoint."
        )
    return endpoint
