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

``POST /api/chat``
    One visitor message. Opens ``chat.turn``, runs the budget check inside it,
    and calls the model *only* if the check allowed it.

``GET /healthz``
    Liveness. Deliberately outside the cap and outside the rate limit -- a probe
    that could be refused for spending money it never spends would take the app
    down every time the ceiling was reached.

**The one ordering that matters.** ``guard.turn`` opens before the agent is
reached and closes after it, and the model call is inside the ``if
budget.allowed`` branch. That is not a stylistic arrangement: it is the whole
reason a refusal costs nothing. ``api/tests/test_app.py`` asserts on a model
double that records calls, so a regression into an asynchronous check fails the
test even though the visitor-facing copy would still read correctly.

**What is deliberately absent.** No login, no visitor identifier in any tool
argument, and no ``session_id`` a client can choose: the cookie is minted here,
so a caller cannot mint a thousand sessions to walk around the per-session cap
without also collecting a thousand cookies -- and the per-source rate limit is
underneath that anyway.
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
from pydantic import BaseModel, Field

from chip_chat.agent import ACCOUNT, AzureChatModel, ChatModel, FoundryConfig
from chip_chat.agent.loop import Conversation, run_turn
from chip_chat.agent.orders import OrderDesk
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import (
    CachedKillSwitch,
    EnvironmentKillSwitch,
    FileKillSwitch,
    any_of,
)
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import STOP_STATE_MESSAGE
from chip_chat.otel import (
    TelemetryConfig,
    chat_turn,
    configure_tracing,
    render_response,
    shutdown_tracing,
)
from chip_chat.web import chat_page, stop_page

__all__ = [
    "ChatReply",
    "ChatRequest",
    "Service",
    "SessionStore",
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

    def get(self, session_id: str) -> Conversation:
        """Return the conversation for ``session_id``, creating it if new."""
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                if len(self._conversations) >= self._max:
                    # Insertion-ordered, so this is the least recently started.
                    self._conversations.pop(next(iter(self._conversations)))
                conversation = Conversation(session_id=session_id)
                self._conversations[session_id] = conversation
            return conversation

    def __len__(self) -> int:
        with self._lock:
            return len(self._conversations)


@dataclass(slots=True)
class Service:
    """Everything one deployment of the app holds, assembled once at start-up.

    Passed to :func:`create_app` rather than reached for through globals so that
    a test can supply a model double and a driven clock. The model is a factory
    rather than an instance because building the real one authenticates against
    Azure, and a process that cannot reach Azure should still be able to serve
    ``/healthz`` and say so.
    """

    guard: SpendGuard
    model_factory: Callable[[], ChatModel]
    desk: OrderDesk = field(default_factory=OrderDesk)
    sessions: SessionStore = field(default_factory=SessionStore)
    _model: ChatModel | None = field(default=None, repr=False)

    def model(self) -> ChatModel:
        """Return the chat model, building it on first use."""
        if self._model is None:
            self._model = self.model_factory()
        return self._model


def default_kill_switch() -> CachedKillSwitch:
    """The circuit breaker a deployment gets unless it says otherwise.

    Two sources, either of which stops the app, memoised for a few seconds so
    that "cheap enough to check on every request" and "responds within seconds"
    are both true. See ``api/README.md``'s runbook.
    """
    return CachedKillSwitch(
        any_of(EnvironmentKillSwitch(), FileKillSwitch(KILL_SWITCH_FILE))
    )


def build_service() -> Service:
    """Assemble the real service from the environment.

    Every ceiling comes from :meth:`~chip_chat.api.limits.SpendLimits.from_env`
    and every model deployment from
    :meth:`~chip_chat.agent.foundry.FoundryConfig.from_env`, so changing either
    on the Container App is a restart rather than a build.
    """
    return Service(
        guard=SpendGuard(SpendLimits.from_env(), kill_switch=default_kill_switch()),
        model_factory=lambda: AzureChatModel(FoundryConfig.from_env()),
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
        stop = resolved.guard.entry_state()
        body = stop_page(stop.message) if stop is not None else chat_page()
        response = HTMLResponse(body)
        _ensure_session(request, response)
        return response

    @application.post("/api/chat")
    async def chat(request: Request, body: ChatRequest) -> Response:
        """Run one turn: guard, then agent, then render."""
        session_id = _session_id(request)
        source_address = _source_address(request)
        conversation = resolved.sessions.get(session_id)
        payload = _run_turn(resolved, conversation, body, source_address)
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    return application


def _run_turn(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    source_address: str,
) -> ChatReply:
    """One ``chat.turn``, from the budget check to the rendered reply.

    Synchronous on purpose. The guard's whole promise is that the refusal
    happens before a model is called, in the request path; FastAPI runs a
    ``def`` handler's work in a thread pool, and this function is where that
    work is, so the ordering is a plain sequence of statements that anybody can
    read top to bottom.
    """
    session_id = conversation.session_id
    with (
        chat_turn(
            session_id=session_id,
            turn_index=conversation.next_turn_index(),
            message=body.message,
            persona_id=ACCOUNT.persona_id,
        ) as turn,
        service.guard.turn(
            session_id=session_id, source_address=source_address
        ) as budget,
    ):
        if not budget.allowed:
            message = budget.message or STOP_STATE_MESSAGE
            stop = budget.stop
            if stop is not None:
                turn.record_stopped(stop.reason.value)
            _render(turn, message)
            return ChatReply(reply=message, stopped=True)

        # Confirmation before the model runs, so that place_order finds a
        # confirmed draft rather than being told about one.
        if body.confirm_draft_id:
            service.desk.confirm(session_id, body.confirm_draft_id)

        try:
            result = run_turn(
                conversation,
                body.message,
                model=service.model(),
                desk=service.desk,
            )
        except Exception as error:
            turn.record_failure(error)
            # The tokens this turn bought before it fell over are unknown,
            # so it is charged the pessimistic reservation. Over-counting by
            # less than one turn is the safe direction to be wrong in.
            budget.record_usage(prompt_tokens=service.guard.limits.turn_token_reservation)
            message = "Something went wrong on my side just then. Try asking again."
            _render(turn, message)
            return ChatReply(reply=message)

        budget.record_usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
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
