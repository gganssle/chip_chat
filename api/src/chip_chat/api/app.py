"""The request path. Everything the spend cap was written to sit inside.

``api/README.md`` said of the cap: *"The cap is a library rather than a
middleware because the shape of the request path is not settled yet."* This
module is that shape arriving, and wiring the cap into it is the point of the
file. :class:`~chip_chat.api.guard.SpendGuard` was correct, tested and had no
caller; a correct cap with no caller does not stop anybody spending anything.

Three routes and no more:

``GET /``
    The entry page. Asks :meth:`~chip_chat.api.guard.SpendGuard.entry_state`
    first, and serves the stop state instead when the door is shut. Emits no
    span: there is no turn yet.

``POST /api/entry``
    The name gate. One invented first name, and the visitor comes back holding a
    fully populated synthetic account -- order history, a home store, a points
    balance. The assignment is :mod:`chip_chat.api.visitors`' and the identity it
    resolves is bound to the session **server-side**, so the response says who
    the visitor has become without ever having been told.

``POST /api/chat``
    One visitor message. Opens ``chat.turn``, runs the budget check inside it,
    and calls the model *only* if the check allowed it.

``GET /healthz``
    Liveness. Deliberately outside the cap and outside the rate limit -- a probe
    that could be refused for spending money it never spends would take the app
    down every time the ceiling was reached.

**The one ordering that matters, and why it is not up to this module.** A
refusal has to cost nothing, which means the check runs before the model and not
beside it. Rather than arranging the statements carefully and hoping the next
route does too, the model is not reachable from here at all:
:class:`~chip_chat.api.turns.SpendGate` holds it privately and hands it out only
inside a :class:`~chip_chat.api.turns.FundedTurn`, which cannot be constructed
for a turn the guard refused. So a second route added later cannot call a model
without passing the check first -- there is nothing else to call. Read
:mod:`chip_chat.api.turns`; ``api/tests/test_spend_gate.py`` is what fails if
somebody takes that apart.

**What is deliberately absent.** No login, no visitor identifier in any tool
argument, and no ``session_id`` a client can choose: the cookie is minted here,
so a caller cannot mint a thousand sessions to walk around the per-session cap
without also collecting a thousand cookies -- and the per-source rate limit is
underneath that anyway.

**And no** ``demo_id``, **anywhere a request can reach.** RFC-001 §05 puts the
identity in the server-side session store and applies it to the Snowflake
connection; this module resolves it from the cookie through
:class:`~chip_chat.api.visitors.VisitorDesk` and passes a *session id* onward.
Every request model here forbids unknown fields, so a body carrying ``demo_id``
is a 422 rather than a field somebody has to prove is ignored, and
``api/tests/test_identity_binding.py`` holds every model and every route
signature to :data:`~chip_chat.snowflake.procedures.IDENTITY_VOCABULARY`.
"""

import logging
import secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from chip_chat.agent import ACCOUNT, AzureChatModel, FoundryConfig
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import PROMPT_VERSION, Conversation
from chip_chat.agent.tools import offered_tools
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import (
    CachedKillSwitch,
    EnvironmentKillSwitch,
    FileKillSwitch,
    any_of,
)
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import Stop
from chip_chat.api.pool import DEFAULT_POOL_SIZE, SessionConnection, VisitorPool
from chip_chat.api.turns import SpendGate
from chip_chat.api.visitors import (
    MAX_DISPLAY_NAME_CHARS,
    PersonaRoster,
    SnowflakeRoster,
    StaticRoster,
    VisitorDesk,
    VisitorSession,
    VisitorSessionStore,
    journal_from_env,
)
from chip_chat.otel import (
    TelemetryConfig,
    TokenUsage,
    ToolName,
    chat_turn,
    configure_tracing,
    render_response,
    shutdown_tracing,
)
from chip_chat.web import chat_page, stop_page

__all__ = [
    "ChatReply",
    "ChatRequest",
    "EntryReply",
    "EntryRequest",
    "Service",
    "SessionStore",
    "VisitorProfile",
    "build_service",
    "create_app",
    "default_kill_switch",
]

SESSION_COOKIE = "cc_session"
"""Name of the cookie the session id travels in. Minted here, never accepted."""

KILL_SWITCH_FILE = "/mnt/ops/stop"
"""The no-restart half of the circuit breaker. ``api/README.md`` runbook step 2.

An unreadable path reads as *not* thrown, which is why a file that does not
exist -- the normal case -- costs nothing and blocks nothing.
"""

_MAX_MESSAGE_CHARS = 2_000
"""Longest visitor message accepted. Cheapest possible bound on prompt tokens,
applied before the model is reached and before anything is billed."""

_MAX_SESSIONS = 4_096
"""Conversations held in memory before the oldest are forgotten."""

_ROBOTS = "User-agent: *\nDisallow: /\n"
"""The demo must never surface on the brand's own search terms."""

_log = logging.getLogger("chip_chat.api")
"""Where a failed turn goes as well as onto its span.

The span is the better record and the one an eval reads. The log line exists
because a turn that fails for every visitor -- a deployment name that does not
exist, an identity without the role assignment -- looks identical from outside
to one that fails for one visitor, and the container's stdout is the first place
anybody looks.
"""


class ChatRequest(BaseModel):
    """One visitor message, and optionally a confirmation of a draft."""

    model_config = ConfigDict(extra="forbid")
    """Unknown fields are refused rather than ignored.

    The field this is really about is ``demo_id``. A body that carries one gets
    a 422, which means "no endpoint accepts a visitor identifier from a client"
    is enforced by the schema rather than by a reviewer checking that nothing
    reads it. See the module docstring.
    """

    message: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)

    confirm_draft_id: str | None = Field(default=None, max_length=64)
    """Set by pressing the confirm button, and by nothing else.

    This is where the launch gate lives. Confirmation arrives as a field on the
    *request*, so the agent cannot grant itself one -- see
    :mod:`chip_chat.agent.orders`.
    """


class ChatReply(BaseModel):
    """What the widget renders."""

    reply: str
    card: dict[str, Any] | None = None
    receipt: bool = False
    stopped: bool = False
    """True when the spend cap refused the turn. Still HTTP 200: the stop state
    is a designed state and never an error."""


class EntryRequest(BaseModel):
    """The name gate: one invented first name, and nothing else.

    ``extra="forbid"`` is doing real work here. This is the request that decides
    which synthetic customer a visitor becomes, so it is exactly the body an
    attacker would like to add a ``demo_id`` to -- and adding one is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_CHARS)
    """What the visitor would like to be called. Invented, and optional.

    Optional because the assignment does not depend on it: a visitor who submits
    an empty form still gets a fully populated account, which is the difference
    between a name gate and a login.
    """


class VisitorProfile(BaseModel):
    """Who the visitor has become, as the entry screen renders it.

    Every field is read off the roster row the app itself chose. Note the one
    that is absent: there is no ``demo_id``. The identity is the server's, the
    browser holds a cookie, and nothing in this payload could be handed back to
    an endpoint to claim an account.
    """

    display_name: str | None = None
    persona_id: str
    label: str
    home_store: int | None = None
    home_store_name: str | None = None
    points_balance: int | None = None
    order_count: int | None = None
    usual_item_id: str | None = None
    narrative: str | None = None


class EntryReply(BaseModel):
    """The answer to the name gate.

    Attributes:
        visitor: The assigned account, or ``None`` when this deployment has no
            synthetic population loaded. ``None`` is a decided state -- see
            :meth:`chip_chat.api.visitors.VisitorDesk.admit` -- and the widget
            renders the demo without an account rather than an error.
        stopped: True when the spend cap has the door shut. Still HTTP 200.
        message: The stop-state copy, when ``stopped``.
    """

    visitor: VisitorProfile | None = None
    stopped: bool = False
    message: str | None = None


class SessionStore:
    """Conversations by session id, bounded and in memory.

    Process-local, like the ledger and for the same reason: one replica, one
    store, and one obvious place for a shared one to land if a second replica
    ever exists. A restart forgets every conversation, which is issue #9's
    problem and not this one's.
    """

    __slots__ = ("_conversations", "_lock", "_max")

    def __init__(self, max_sessions: int = _MAX_SESSIONS) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._lock = threading.Lock()
        self._max = max_sessions

    def get(
        self, session_id: str, *, tools: tuple[ToolName, ...] | None = None
    ) -> Conversation:
        """Return the conversation for ``session_id``, creating it if new.

        Args:
            session_id: The conversation to fetch.
            tools: The tools this deployment has registered, which a new
                conversation's runtime context names. Read from
                :func:`~chip_chat.agent.tools.offered_tools` at the call site,
                because which lanes are answerable is a property of the
                assembled service and not of this store.
        """
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                if len(self._conversations) >= self._max:
                    # Insertion-ordered, so this is the least recently started.
                    self._conversations.pop(next(iter(self._conversations)))
                conversation = (
                    Conversation(session_id=session_id)
                    if tools is None
                    else Conversation(session_id=session_id, tools=tools)
                )
                self._conversations[session_id] = conversation
            return conversation

    def __len__(self) -> int:
        with self._lock:
            return len(self._conversations)


@dataclass(slots=True)
class Service:
    """Everything one deployment of the app holds, assembled once at start-up.

    The gate is required and positional, so there is no service -- and therefore
    no application -- assembled without a spend cap behind it. It is passed to
    :func:`create_app` rather than reached for through globals so that a test
    can supply a model double and a driven clock.

    Note what is *not* here: a model. The only object in this package that holds
    one is :class:`~chip_chat.api.turns.SpendGate`, and it does not hand it out
    except inside a funded turn.
    """

    gate: SpendGate
    sessions: SessionStore = field(default_factory=SessionStore)
    visitors: VisitorDesk = field(default_factory=lambda: VisitorDesk(StaticRoster()))
    """Where a browser becomes a synthetic customer, and the store the pool
    resolves identities against.

    Defaults to a desk with an **empty** roster, which is the honest state of a
    deployment whose synthetic population has not been loaded: every visitor is
    served unbound, exactly as they were before this field existed, and
    :data:`~chip_chat.api.visitors.VISITORS_LOGGER` has already said so. It is
    not a default that invents customers -- an invented account is the empty
    account issue #66 is written to prevent.
    """

    pool: VisitorPool | None = None
    """The connection pool, where one is configured.

    ``None`` on a deployment with no Snowflake connection factory, which is
    every deployment today: ``pool.py``'s :class:`SessionConnection` is a
    protocol and nothing in this lockfile implements it. See
    :func:`build_service`.
    """

    @property
    def guard(self) -> SpendGuard:
        """The cap the gate enforces, for tests and for an ops surface."""
        return self.gate.guard


def default_kill_switch() -> CachedKillSwitch:
    """The circuit breaker a deployment gets unless it says otherwise.

    Two sources, either of which stops the app, memoised for a few seconds so
    that "cheap enough to check on every request" and "responds within seconds"
    are both true. See ``api/README.md``'s runbook.
    """
    return CachedKillSwitch(
        any_of(EnvironmentKillSwitch(), FileKillSwitch(KILL_SWITCH_FILE))
    )


def build_visitors(
    connect: Callable[[], SessionConnection] | None = None,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
) -> tuple[VisitorDesk, VisitorPool | None]:
    """Assemble the session store, the pool that binds it, and the roster.

    The order is the whole of RFC-001 §05's trusted path, written once:

    1. the journal decides whether bindings survive a restart, and says which;
    2. the store holds them, and is the only thing that answers ``demo_id_for``;
    3. the pool is built **around** that store, so the identity it applies to a
       Snowflake session cannot have come from a caller; and
    4. the roster is read through the pool's one deliberately unbound checkout.

    Args:
        connect: Opens a Snowflake connection. ``None`` -- the default, and the
            state of every deployment today -- means no pool, an empty roster
            and every visitor served unbound.
            :class:`~chip_chat.api.pool.SessionConnection` is a protocol
            deliberately not backed by a driver in this lockfile
            (:mod:`chip_chat.snowflake.snow` gives the argument), so the
            adapter is a parameter rather than an import.
        pool_size: Live connections. See
            :data:`~chip_chat.api.pool.DEFAULT_POOL_SIZE`.

    Returns:
        The desk and the pool, the second of which is ``None`` when ``connect``
        was.
    """
    store = VisitorSessionStore(journal_from_env())
    if connect is None:
        return VisitorDesk(StaticRoster(), store=store), None
    pool = VisitorPool(connect, sessions=store, size=pool_size)
    roster: PersonaRoster = SnowflakeRoster(pool)
    return VisitorDesk(roster, store=store), pool


def build_service(
    lanes: Lanes = NO_LANES,
    connect: Callable[[], SessionConnection] | None = None,
) -> Service:
    """Assemble the real service from the environment.

    Every ceiling comes from :meth:`~chip_chat.api.limits.SpendLimits.from_env`
    and every model deployment from
    :meth:`~chip_chat.agent.foundry.FoundryConfig.from_env`, so changing either
    on the Container App is a restart rather than a build.

    Args:
        lanes: The backing services this deployment has.
            **Nothing supplies any of them yet**, so a deployment runs the
            week-one slice and does not offer ``ask_account_question``,
            ``get_recommendations`` or ``match_meal_from_photo`` -- see
            :func:`~chip_chat.agent.tools.offered_tools` for why that is the
            honest state rather than a hole. The parameter exists so the seam is
            named rather than discovered, and bead ``cc-e1sr`` is where each is
            wired:

            *knowledge* needs one
            :class:`~chip_chat.search.retrieve.Retriever` built per process
            against the live alias -- the cheapest of the three, and blocked on
            nothing structural.
            *account* and *personalization* need the pool below to actually
            have connections in it, which is ``connect`` and therefore
            ``cc-lpy4``. Handing them a pool that cannot check anything out
            would offer the model two tools that decline on every turn, which
            reads as a lane outage rather than as a deployment nobody finished.
            *photo* needs an upload route, since the tool takes a reference to a
            photograph the visitor uploaded on this turn, and a production
            catalogue loader for stage 5 -- #62 and ``cc-mpd``.

        connect: Opens a Snowflake connection, for the pool and the roster. See
            :func:`build_visitors` for why it is an argument.

    Returns:
        The assembled service.
    """
    visitors, pool = build_visitors(connect)
    return Service(
        gate=SpendGate(
            SpendGuard(SpendLimits.from_env(), kill_switch=default_kill_switch()),
            lambda: AzureChatModel(FoundryConfig.from_env()),
            lanes=lanes,
        ),
        visitors=visitors,
        pool=pool,
    )


def create_app(service: Service | None = None) -> FastAPI:
    """Build the ASGI application.

    Args:
        service: The assembled service. Defaults to :func:`build_service`,
            which reads the environment and needs Azure credentials the first
            time a model is called.

    Returns:
        The application. ``uvicorn chip_chat.api.app:app`` serves it.
    """
    resolved = service if service is not None else build_service()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        config = TelemetryConfig.from_env("api")
        if config.exports_anywhere:
            configure_tracing(config)
        try:
            yield
        finally:
            # Flushes the batch processor. A container that exits without this
            # loses whatever spans were still queued, which looks exactly like a
            # backend that stopped listening.
            shutdown_tracing()

    application = FastAPI(
        title="Chip Chat — Cilantro",
        description="The week-one slice: hardcoded data, real deployment.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def _noindex(request: Request, call_next: Any) -> Response:
        """Keep the demo out of search results even when nothing runs the meta tag."""
        response: Response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness. Never gated by the cap; see the module docstring."""
        return JSONResponse({"status": "ok"})

    @application.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return _ROBOTS

    @application.get("/", response_class=HTMLResponse)
    async def entry(request: Request) -> HTMLResponse:
        """Serve the chat page, or the stop state when the door is shut."""
        stop = resolved.gate.entry_state()
        body = stop_page(stop.message) if stop is not None else chat_page()
        response = HTMLResponse(body)
        _ensure_session(request, response)
        return response

    @application.post("/api/entry")
    async def entry_gate(request: Request, body: EntryRequest) -> Response:
        """Assign this session a synthetic customer and say who they are.

        The name is the visitor's only input, and it is not what decides the
        account: :meth:`~chip_chat.api.visitors.VisitorDesk.admit` chooses from
        the roster, binds the result to the session id resolved from the cookie,
        and hands back what it chose. A second call for the same cookie returns
        the same visitor -- issue #9 decided visitor state persists between
        visits, so a returning browser resumes rather than collects a second
        account.

        The spend cap is asked first and answered the same way the entry page
        answers it: a stop state, at 200, with no account assigned. Assigning a
        persona to a visitor who cannot have a conversation would spend a roster
        slot on nobody.
        """
        session_id = _session_id(request)
        if (stop := resolved.gate.entry_state()) is not None:
            payload = EntryReply(stopped=True, message=stop.message)
        else:
            payload = EntryReply(
                visitor=_profile(
                    resolved.visitors.admit(session_id, display_name=body.name)
                )
            )
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    @application.post("/api/chat")
    async def chat(request: Request, body: ChatRequest) -> Response:
        """Run one turn: guard, then agent, then render."""
        session_id = _session_id(request)
        source_address = _source_address(request)
        # A visitor who never posted the name gate still gets an account. The
        # cold start is the product risk and an unbound conversation is the
        # empty-account failure wearing a different hat, so the assignment is
        # here as well as on the entry route rather than only on the polite path.
        admitted = resolved.visitors.admit(session_id)
        conversation = resolved.sessions.get(
            session_id, tools=offered_tools(resolved.gate.lanes)
        )
        payload = _run_turn(resolved, conversation, body, source_address, admitted)
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    return application


def _profile(admitted: VisitorSession | None) -> VisitorProfile | None:
    """Render an assigned visitor for the entry screen, or ``None``.

    The ``demo_id`` is not copied across. It is the one field of the binding the
    browser has no use for and the one an attacker would: a payload that never
    carries it is a payload that cannot be replayed into a request claiming it.
    """
    if admitted is None:
        return None
    fixture = admitted.fixture
    return VisitorProfile(
        display_name=admitted.display_name,
        persona_id=admitted.persona_id,
        label=admitted.label,
        home_store=fixture.home_store if fixture is not None else None,
        home_store_name=fixture.home_store_name if fixture is not None else None,
        points_balance=fixture.points_balance if fixture is not None else None,
        order_count=fixture.order_count if fixture is not None else None,
        usual_item_id=fixture.usual_item_id if fixture is not None else None,
        narrative=fixture.narrative if fixture is not None else None,
    )


def _run_turn(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    source_address: str,
    admitted: VisitorSession | None = None,
) -> ChatReply:
    """One ``chat.turn``, from the budget check to the rendered reply.

    Synchronous on purpose. FastAPI runs a ``def`` handler's work in a thread
    pool, and this function is where that work is, so the ordering is a plain
    sequence of statements anybody can read top to bottom.

    Args:
        service: The assembled service.
        conversation: The visitor's history.
        body: The request.
        source_address: What the per-source rate limit counts against.
        admitted: The assigned synthetic customer, where the roster had one. It
            reaches the span and nothing else: ``demo_id`` is a correlation
            value on ``chat.turn`` -- see
            :attr:`~chip_chat.otel.attributes.ChipChatAttributes.DEMO_ID` -- and
            the *binding* travels to Snowflake through the session store, which
            is the only path RFC-001 §05 permits.
    """
    session_id = conversation.session_id
    with (
        chat_turn(
            session_id=session_id,
            turn_index=conversation.next_turn_index(),
            message=body.message,
            persona_id=(
                admitted.persona_id if admitted is not None else ACCOUNT.persona_id
            ),
            demo_id=admitted.demo_id if admitted is not None else None,
            prompt_version=PROMPT_VERSION,
        ) as turn,
        service.gate.turn(session_id=session_id, source_address=source_address) as funded,
    ):
        if isinstance(funded, Stop):
            # A Stop has no `run`. The refused branch could not call a model
            # if it tried, which is why this is an `isinstance` and not a
            # boolean somebody could invert.
            turn.record_stopped(funded.reason.value)
            _render(turn, funded.message)
            return ChatReply(reply=funded.message, stopped=True)

        try:
            result = funded.run(
                conversation,
                body.message,
                confirm_draft_id=body.confirm_draft_id,
            )
        except Exception as error:
            turn.record_failure(error)
            _log.exception("turn failed", extra={"session_id": session_id})
            funded.charge_reservation(service.guard.limits.turn_token_reservation)
            message = "Something went wrong on my side just then. Try asking again."
            _render(turn, message)
            return ChatReply(reply=message)

        # What the whole turn cost, on the root of its trace. The individual
        # `llm.completion` spans carry the OpenInference counts and are what a
        # sum reconciles against the provider; this is the same figure under
        # `chip_chat.tokens.*` so that Application Insights -- which searches
        # attributes and does not walk trace trees -- can chart cost per
        # conversation without one. See `ChipChatAttributes.TOKENS_TOTAL`.
        turn.record_token_rollup(
            TokenUsage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
        )
        _render(turn, result.reply)
        return ChatReply(
            reply=result.reply,
            card=dict(result.card) if result.card is not None else None,
            receipt=result.receipt,
        )


def _render(turn: Any, message: str) -> None:
    """Close the turn the same way however it ended.

    ``render.response`` is emitted for a stop state and for a failure as well as
    for an answer, because "what did the visitor actually see" is the question
    the span exists to answer and those are all things a visitor saw.
    """
    with render_response() as recorder:
        recorder.record_output(message)
    turn.record_output(message)


def _session_id(request: Request) -> str:
    """Return the request's session id, minting one if it carries none."""
    existing = request.cookies.get(SESSION_COOKIE)
    if existing and _plausible(existing):
        return existing
    return _new_session_id()


def _new_session_id() -> str:
    return secrets.token_urlsafe(16)


def _plausible(value: str) -> bool:
    """Cheap sanity check on a cookie value before it becomes a span attribute."""
    return 8 <= len(value) <= 64 and all(
        character.isalnum() or character in "-_" for character in value
    )


def _ensure_session(request: Request, response: Response) -> None:
    """Give the entry page a session cookie if it arrived without one."""
    _set_session_cookie(response, _session_id(request), secure=_is_https(request))


def _set_session_cookie(response: Response, session_id: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=86_400,
    )


def _is_https(request: Request) -> bool:
    """Whether the visitor's connection is encrypted, as opposed to this hop's.

    Container Apps terminates TLS at the ingress and forwards plain HTTP, so the
    scheme this process sees is ``http`` on a site that is entirely HTTPS. A
    ``Secure`` cookie decided from that would never be set in production; a
    cookie that ignored the question would not be ``Secure`` on a local run, and
    then the local run would be the one that did not match. So the forwarded
    scheme wins where there is one, and a plain local ``http://localhost`` run
    gets a cookie it can actually keep.
    """
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (forwarded or request.url.scheme).lower() == "https"


def _source_address(request: Request) -> str:
    """Resolve the client address the per-source rate limit counts against.

    Behind Container Apps ingress there is exactly one trusted proxy, and it
    *appends* to ``X-Forwarded-For``. So the last entry is the address that
    proxy saw and the earlier ones are whatever the client chose to claim --
    taking the first would let a caller re-roll its rate-limit bucket on every
    request by sending a different header, which is the opposite of what the
    limiter is for.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
        if candidates:
            return candidates[-1]
    client = request.client
    return client.host if client is not None else "unknown"
