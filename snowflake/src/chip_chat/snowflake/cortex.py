"""The Cortex Analyst call, which is the half of the account lane #45 left open.

``snowflake/README.md`` is explicit about the split:

    ``analyst.py`` is the other half and it makes no network call: it takes a
    Cortex Analyst response and returns either SQL worth running or the reason
    it will not [...] **#61 owns the HTTP and the span.**

This module is the HTTP. :mod:`chip_chat.snowflake.lane` owns the span, and
:func:`chip_chat.snowflake.analyst.decide` still owns the judgement -- which is
the division worth keeping, because the judgement is the part PRD A4 is about
and it stays testable without an account.

**One request, one question, no conversation.** Cortex Analyst accepts a message
history and this sends a single user turn every time. The visitor's conversation
lives in the agent's message list; replaying it here would mean the SQL for
*"and what about last year"* depended on which of two systems remembered the
previous question, and the one that answers a different question on a retry is
the one you cannot evaluate. RFC-001 §06 gives ``ask_account_question`` one
argument for the same reason.

**Nothing here names a visitor.** The request body carries the question and the
semantic view and nothing else -- no identifier, no filter, no ``demo_id``. The
generated SQL then runs on the bound connection, where #43's row access policies
scope it. ``sql/11_semantic_view.sql`` puts the same instruction in the model's
own words through ``AI_SQL_GENERATION``, and
:func:`~chip_chat.snowflake.analyst.reads_only_the_view` refuses a statement that
mentions the column anyway. Three layers, and the outer two exist because the
inner one is a language model.

**Authentication is a key-pair JWT, minted by the CLI.** The connection is
already described once in ``~/.snowflake/config.toml`` -- account, user, private
key -- and :mod:`chip_chat.snowflake.snow` gives the argument for not building a
second thing that knows how to authenticate. ``snow connection generate-jwt``
signs a token from that same configuration, so the credential path is the CLI's
in both directions. A deployment that cannot ship the CLI supplies a different
:class:`TokenSource`; that is what the protocol is for.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Final, Protocol

from chip_chat.snowflake import analyst, semantic, snow

__all__ = [
    "ANALYST_PATH",
    "HOST_VARIABLE",
    "TOKEN_TYPE_HEADER",
    "AnalystError",
    "AnalystTransport",
    "CliJwt",
    "HttpAnalystTransport",
    "Response",
    "TokenSource",
    "host_from_env",
    "pooled_client",
]

ANALYST_PATH: Final = "/api/v2/cortex/analyst/message"
"""The endpoint. Measured against this account on 2026-08-27 at a 3.65s median
round trip -- see ``docs/snowflake-semantic-view.md`` §4, and note that the
number is why :data:`chip_chat.snowflake.analyst.DEFAULT_TIMEOUT_SECONDS` is
fifteen seconds rather than five."""

TOKEN_TYPE_HEADER: Final = "X-Snowflake-Authorization-Token-Type"
"""Snowflake requires the *kind* of bearer token as well as the token.

``KEYPAIR_JWT`` for what :class:`CliJwt` mints. Omitting it is a 401 whose body
does not say which of the two headers was the problem, which is a confusing way
to spend an afternoon.
"""

HOST_VARIABLE: Final = "SNOWFLAKE_HOST"
"""Where the account's REST host comes from.

``SNOWFLAKE_``-prefixed rather than ``CHIP_CHAT_``-prefixed for the reason
``.env.example`` gives about names: the CLI's own configuration already carries
the account identifier, and a second spelling of one value is a second thing to
keep in step. Set it to ``<account_identifier>.snowflakecomputing.com``.
"""


class AnalystError(RuntimeError):
    """Cortex Analyst could not be reached, or answered something unreadable.

    Never raised past :mod:`chip_chat.snowflake.lane`: RFC-001 §10 gives the
    account lane a blast radius of one row, and
    :func:`~chip_chat.snowflake.analyst.decide` already knows what to do with a
    call that did not produce a response -- ``None`` in, ``Path.UNAVAILABLE``
    out. This type exists so the lane can tell *the service did not answer*
    apart from *the service answered and the answer was refused*, which are the
    same refusal to a visitor and two different findings to an operator.
    """


class Response(Protocol):
    """The slice of an HTTP response this module reads.

    A protocol so the ``httpx.Client`` stays injected and its exception types
    stay unnameable here -- the same arrangement, and the same reason, as
    :class:`chip_chat.search.client.HttpSearchService`.
    """

    @property
    def status_code(self) -> int:
        """The HTTP status."""
        ...

    @property
    def text(self) -> str:
        """The body as text, for an error message."""
        ...

    def json(self) -> Any:
        """The body decoded as JSON."""
        ...


class TokenSource(Protocol):
    """Returns a bearer token for the Snowflake REST API."""

    def token(self) -> str:
        """Return a currently valid bearer token."""
        ...

    @property
    def token_type(self) -> str:
        """What :data:`TOKEN_TYPE_HEADER` should say about it."""
        ...


class AnalystTransport(Protocol):
    """One question to Cortex Analyst, and how long it took.

    The seam :class:`~chip_chat.snowflake.lane.AccountLane` holds. A protocol
    rather than the class below because the lane's whole failure behaviour is
    what PRD A4 is about, and behaviour under failure is what a live service is
    worst at demonstrating on demand.
    """

    def ask(self, question: str) -> tuple[Mapping[str, Any] | None, float]:
        """Ask ``question`` and return the decoded body and the elapsed seconds.

        Returns ``(None, elapsed)`` where the call failed, because that is
        exactly what :func:`chip_chat.snowflake.analyst.decide` takes for a
        response that did not arrive.
        """
        ...


class CliJwt:
    """A :class:`TokenSource` that shells out to ``snow connection generate-jwt``.

    The same argument :mod:`chip_chat.snowflake.snow` makes for shelling out at
    all: the account, the user and the private key are described once in
    ``~/.snowflake/config.toml``, and a second code path that knew how to sign a
    JWT would be a second thing to fix when the key rotates.

    Tokens are cached for ``lifetime_seconds`` rather than minted per call,
    because a subprocess per turn on a lane already spending three seconds of
    inference is a cost with nothing to show for it.
    """

    __slots__ = ("_expires_at", "_lifetime", "_token")

    def __init__(self, lifetime_seconds: float = 240.0) -> None:
        """Initialise the token source.

        Args:
            lifetime_seconds: How long a minted token is reused for. Well under
                Snowflake's own hour, because a token this process is still
                holding when it expires is a 401 in the middle of a turn.
        """
        self._lifetime = lifetime_seconds
        self._token = ""
        self._expires_at = 0.0

    @property
    def token_type(self) -> str:
        """``KEYPAIR_JWT``, which is what the CLI mints."""
        return "KEYPAIR_JWT"

    def token(self) -> str:
        """Return a bearer token, minting one if the cached one is old.

        Returns:
            The JWT.

        Raises:
            AnalystError: If the CLI is absent or refused. Named as this type
                rather than as :class:`~chip_chat.snowflake.snow.SnowError` so
                that the lane catches one exception and the account lane
                declines rather than the turn failing.
        """
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token
        try:
            snow.require_cli()
            completed = subprocess.run(
                [
                    "snow",
                    "connection",
                    "generate-jwt",
                    "--connection",
                    snow.connection_name(),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise AnalystError(f"could not run the Snowflake CLI: {error}") from error
        except snow.SnowError as error:
            raise AnalystError(str(error)) from error
        if completed.returncode != 0:
            raise AnalystError(
                "snow connection generate-jwt failed: "
                f"{(completed.stdout + completed.stderr).strip()[:600]}"
            )
        minted = completed.stdout.strip().splitlines()
        token = minted[-1].strip() if minted else ""
        if not token:
            raise AnalystError("snow connection generate-jwt printed no token")
        self._token = token
        self._expires_at = now + self._lifetime
        return token


class HttpAnalystTransport:
    """The real :class:`AnalystTransport`, over the Snowflake REST API."""

    __slots__ = ("_client", "_host", "_timeout", "_token", "_view")

    def __init__(
        self,
        host: str,
        client: Any,
        token: TokenSource,
        *,
        semantic_view: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Initialise the transport.

        Args:
            host: ``<account_identifier>.snowflakecomputing.com``, with or
                without a scheme.
            client: An ``httpx.Client``. Injected so one connection pool serves
                the process and so a test can supply a transport.
            token: Where bearer tokens come from.
            semantic_view: The view to ask against. The account lane's own by
                default, and there is no second one.
            timeout_seconds: Seconds before the request is abandoned.
                :data:`chip_chat.snowflake.analyst.DEFAULT_TIMEOUT_SECONDS`
                where omitted, so the transport gives up at the same moment the
                decision would have discarded the answer anyway.
        """
        self._host = host if "://" in host else f"https://{host}"
        self._host = self._host.rstrip("/")
        self._client = client
        self._token = token
        self._view = semantic.qualified() if semantic_view is None else semantic_view
        self._timeout = (
            analyst.DEFAULT_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )

    def ask(self, question: str) -> tuple[Mapping[str, Any] | None, float]:
        """Ask Cortex Analyst one question.

        Args:
            question: The visitor's words, unchanged. Not rewritten, not
                prefixed with an instruction, and not joined to an earlier turn
                -- see the module docstring.

        Returns:
            ``(body, elapsed_seconds)``, with ``body`` ``None`` where the call
            did not produce a readable response. Never raises: every failure is
            a response that did not arrive, which is precisely the input
            :func:`chip_chat.snowflake.analyst.decide` takes ``None`` for.
        """
        started = time.monotonic()
        try:
            response = self._post(question)
        except AnalystError:
            return None, time.monotonic() - started
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            return None, elapsed
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return None, elapsed
        return (body if isinstance(body, Mapping) else None), elapsed

    def _post(self, question: str) -> Response:
        """Make the request, or raise :class:`AnalystError` saying why not."""
        try:
            response: Response = self._client.request(
                "POST",
                f"{self._host}{ANALYST_PATH}",
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": question}],
                        }
                    ],
                    "semantic_view": self._view,
                },
                headers={
                    "Authorization": f"Bearer {self._token.token()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    TOKEN_TYPE_HEADER: self._token.token_type,
                },
                timeout=self._timeout,
            )
        except AnalystError:
            raise
        except Exception as error:
            # A refused connection, a DNS failure or a timeout is *the service
            # being unavailable*, which is a row in RFC-001 §10 with a blast
            # radius of one lane. The client is injected rather than imported,
            # so the transport's exception types are deliberately not nameable
            # here and this catch has to be broad to be the boundary at all.
            raise AnalystError(
                f"POST {ANALYST_PATH} did not reach {self._host}: "
                f"{type(error).__name__}: {error}"
            ) from error
        return response


def host_from_env(env: Mapping[str, str] | None = None) -> str:
    """Return the Snowflake REST host from the environment.

    Args:
        env: The environment. Defaults to :data:`os.environ`.

    Returns:
        The host, e.g. ``ab12345.us-east-2.aws.snowflakecomputing.com``.

    Raises:
        AnalystError: If :data:`HOST_VARIABLE` is unset. Raised rather than
            guessed at from the CLI's configuration file: an account identifier
            assembled here and wrong is a lane that fails on every turn with a
            DNS error, and there is no defensible default.
    """
    source = os.environ if env is None else env
    host = source.get(HOST_VARIABLE, "").strip()
    if not host:
        raise AnalystError(
            f"{HOST_VARIABLE} is not set, so the account lane has no host to ask. "
            "It is <account_identifier>.snowflakecomputing.com; "
            "`snow connection list` prints the account identifier."
        )
    return host


def pooled_client(timeout: float | None = None) -> Any:
    """Return an ``httpx.Client`` whose pool outlives one request.

    Built **once per process** and handed to one :class:`HttpAnalystTransport`,
    for the reason :func:`chip_chat.search.client.pooled_client` measures: a
    client per turn is a TLS handshake per turn. It matters less here than it
    does for search -- three seconds of cross-region inference dwarfs seventy
    milliseconds of handshake -- but it costs nothing to not pay it.

    Args:
        timeout: Seconds before a request is abandoned.
            :data:`chip_chat.snowflake.analyst.DEFAULT_TIMEOUT_SECONDS` where
            omitted.

    Returns:
        An ``httpx.Client``. Imported inside the function so that importing this
        module -- which ``make ci`` does -- costs nothing.
    """
    import httpx

    return httpx.Client(
        timeout=analyst.DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
    )
