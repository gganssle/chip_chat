"""Enough of ``azure.functions`` to run ``api/functions/function_app.py`` here.

``api/functions/requirements.txt`` carries the reason the real SDK is not in
this lockfile: it exists for one file, it is installed by the Functions worker
that runs that file, and putting it in the workspace would hand every developer
a dependency to satisfy a module nothing here imports. That argument is sound
and this module does not reopen it. What it does is remove the consequence --
that the host's edge could only ever be read as text.

``api/tests/test_ops_host.py`` reads it as text and asserts the claims a review
would otherwise have to remember: four routes, three preconditions, no SQL.
Those are claims about the *shape* of the file. Issue #63's acceptance criteria
are claims about its *behaviour* -- an unconfirmed draft is rejected, another
session's confirmed draft is rejected, a retry writes once, an outage answers
with the specified sentence -- and none of them can be established by grepping.
``api/tests/test_ops_routes.py`` establishes them by calling the routes, and
this is what makes that import work.

WHAT A STUB IS ALLOWED TO BE, AND WHAT IT IS NOT. A double that is more
forgiving than the thing it stands in for turns a green test into a claim about
the double. So the three behaviours the host actually depends on are
implemented the way the SDK implements them rather than the way that would be
convenient:

**Headers are case-insensitive.** ``azure.functions.HttpRequest`` wraps them in
a case-insensitive mapping, because HTTP header names are case-insensitive on
the wire and no client is obliged to send ``x-cilantro-ops-key`` in the casing
this repository writes it in. A stub with a plain ``dict`` would let a host that
looked up ``X-Cilantro-Ops-Key`` pass here and refuse every real request.

**A body that is not JSON raises ``ValueError``.** That is the contract
``_body`` is written against, and returning ``None`` instead would make the
400 path unreachable in a test and reachable only in production.

**A response body is bytes.** ``HttpResponse.get_body`` returns bytes and the
tests decode it, so a host that handed back an object the SDK cannot serialise
would be caught here rather than by a worker.

Registration is the one place this is deliberately thinner than the SDK.
:meth:`FunctionApp.route` records the route and returns the function unchanged,
so a test calls ``function_app.place_order(request)`` directly. The worker's
own dispatch -- which function name maps to which URL, and how the trigger
binding is built -- is Azure's code and not this repository's, and asserting on
it here would be asserting on a stub.
"""

import json
import sys
import types
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from enum import Enum
from types import ModuleType
from typing import Any

__all__ = [
    "AuthLevel",
    "FunctionApp",
    "HttpRequest",
    "HttpResponse",
    "install",
    "module",
]


class AuthLevel(Enum):
    """The worker's own key check, which the host layers its own on top of."""

    ANONYMOUS = "anonymous"
    FUNCTION = "function"
    ADMIN = "admin"


class _Headers(MutableMapping[str, str]):
    """A case-insensitive header mapping, as the SDK hands the host.

    Written out rather than approximated with ``dict`` for the reason the module
    docstring gives: a case-sensitive stub would accept a host that no real
    client could reach.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = {}
        for key, value in (values or {}).items():
            self._values[key.lower()] = value

    def __getitem__(self, key: str) -> str:
        return self._values[key.lower()]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key.lower()] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class HttpRequest:
    """One inbound request, in the shape the host reads it.

    Attributes:
        method: The verb. The host declares ``POST`` on every route.
        url: The full URL. Unused by the host and present because the SDK's
            constructor requires it.
        headers: Case-insensitive, as above.
    """

    __slots__ = ("_body", "headers", "method", "params", "route_params", "url")

    def __init__(
        self,
        method: str,
        url: str,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        route_params: Mapping[str, str] | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = _Headers(headers)
        self.params = dict(params or {})
        self.route_params = dict(route_params or {})
        self._body = body

    def get_body(self) -> bytes:
        """The raw body."""
        return self._body

    def get_json(self) -> Any:
        """Decode the body as JSON.

        Raises:
            ValueError: If the body is not JSON. The SDK raises this and
                :func:`function_app._body` catches exactly it, so a stub that
                returned ``None`` would leave the 400 path untested.
        """
        if not self._body:
            raise ValueError("the request body is empty")
        return json.loads(self._body.decode("utf-8"))


class HttpResponse:
    """One outbound response.

    Attributes:
        status_code: What the host answered with. Issue #63's rejection-is-a-200
            rule is asserted on this.
        mimetype: ``application/json`` from every path in the host.
    """

    __slots__ = ("_body", "headers", "mimetype", "status_code")

    def __init__(
        self,
        body: str | bytes = "",
        *,
        status_code: int = 200,
        mimetype: str = "text/plain",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status_code
        self.mimetype = mimetype
        self.headers = _Headers(headers)

    def get_body(self) -> bytes:
        """The response body, as bytes, the way the SDK returns it."""
        return self._body


class FunctionApp:
    """The decorator registry. See the module docstring on what it does not do.

    Attributes:
        routes: Route name to the function registered for it, so a test can
            assert the registration happened rather than trusting the import.
    """

    __slots__ = ("http_auth_level", "routes")

    def __init__(self, http_auth_level: AuthLevel = AuthLevel.FUNCTION) -> None:
        self.http_auth_level = http_auth_level
        self.routes: dict[str, Callable[..., Any]] = {}

    def route(
        self, route: str, methods: tuple[str, ...] = ("GET",), **_: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a route and return the function unchanged."""

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self.routes[route] = function
            return function

        return register


def module() -> ModuleType:
    """Build the ``azure.functions`` module object this package stands in for."""
    functions = types.ModuleType("azure.functions")
    for name, value in (
        ("AuthLevel", AuthLevel),
        ("FunctionApp", FunctionApp),
        ("HttpRequest", HttpRequest),
        ("HttpResponse", HttpResponse),
    ):
        setattr(functions, name, value)
    return functions


def install() -> None:
    """Put the stub on ``sys.modules`` so ``import azure.functions`` finds it.

    A no-op where the real SDK is already importable, which is the only sane
    order of preference: if somebody's environment has it, the host should be
    exercised against it rather than against this.
    """
    try:  # pragma: no cover - the real SDK is not in this lockfile
        import azure.functions  # noqa: F401

        return
    except ImportError:
        pass

    existing = sys.modules.get("azure")
    if existing is None:
        existing = types.ModuleType("azure")
        # A namespace package with nowhere to look. `azure` is a namespace in
        # the real distribution, and without a `__path__` the import machinery
        # refuses to treat `azure.functions` as living under it.
        existing.__path__ = []
        sys.modules["azure"] = existing
    functions = module()
    sys.modules["azure.functions"] = functions
    setattr(existing, "functions", functions)  # noqa: B010
