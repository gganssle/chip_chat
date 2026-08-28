"""The request path. Everything the spend cap was written to sit inside.

``api/README.md`` said of the cap: *"The cap is a library rather than a
middleware because the shape of the request path is not settled yet."* This
module is that shape arriving, and wiring the cap into it is the point of the
file. :class:`~chip_chat.api.guard.SpendGuard` was correct, tested and had no
caller; a correct cap with no caller does not stop anybody spending anything.

Eight routes, and every one of them is on ``api/tests/test_spend_gate.py``'s
list so that a ninth has to be argued for:

``GET /``
    The entry page. Asks :meth:`~chip_chat.api.guard.SpendGuard.entry_state`
    first, and serves the stop state instead when the door is shut. Emits no
    span: there is no turn yet. The same document for every visitor -- who they
    have become arrives on the next request, because the page is a page and the
    persona is not.

``POST /api/entry``
    The name gate. One invented first name, and the visitor comes back holding a
    fully populated synthetic account -- order history, a home store, a points
    balance -- **and the sentence that says so**. The assignment is
    :mod:`chip_chat.api.visitors`' and the identity it resolves is bound to the
    session **server-side**, so the response says who the visitor has become
    without ever having been told.

``POST /api/switch``
    Become somebody else. A new session id, a released binding, a forgotten
    conversation and a different archetype, in that order. Its request model has
    no fields at all: see :class:`SwitchRequest`.

``POST /api/chat``
    One visitor message. Opens ``chat.turn``, runs the budget check inside it,
    and calls the model *only* if the check allowed it. Answers as one JSON
    object or as a stream of frames, depending on ``Accept``.

``POST /api/draft/revise``
    A card edited in place, priced again. No model, no confirmation carried
    across, and a new draft id -- so an edited card is unconfirmed by
    construction rather than by remembering to clear a flag.

``POST /api/photo``
    One photograph, under :class:`~chip_chat.api.uploads.UploadLimiter` and then
    through :class:`~chip_chat.vision.intake.PhotoIntake`. The only route that
    spends money before a turn does, which is why it carries its own ceiling.

``GET /healthz``
    Liveness. Deliberately outside the cap and outside the rate limit -- a probe
    that could be refused for spending money it never spends would take the app
    down every time the ceiling was reached.

``GET /robots.txt``
    The half of ``noindex`` that works when something fetches the page without
    executing it. Issue #70.

**What no route does.** There is no endpoint that returns the harvested corpus,
the catalogue, or the roster: the menu data is cached for the demo and not
republished as a dataset, and ``api/tests/test_public_demo.py`` asserts that as
an absence of GET routes rather than as a 404 somebody remembered to add.

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

import json
import logging
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from chip_chat.agent import ACCOUNT, AzureChatModel, FoundryConfig
from chip_chat.agent.health import probe
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import PROMPT_VERSION, Conversation
from chip_chat.agent.tools import offered_tools
from chip_chat.api.connect import (
    KeyPairJwt,
    PrivateKey,
    SnowflakeSettings,
    snowflake_connect,
)
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
from chip_chat.api.uploads import PhotoRegistry, UploadLimiter
from chip_chat.api.visitors import (
    MAX_DISPLAY_NAME_CHARS,
    PersonaRoster,
    SnowflakeRoster,
    StaticRoster,
    VisitorDesk,
    VisitorSession,
    VisitorSessionStore,
    journal_from_env,
    shipped_roster,
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
from chip_chat.snowflake.cortex import (
    HOST_VARIABLE,
    AnalystError,
    HttpAnalystTransport,
    host_from_env,
    pooled_client,
)
from chip_chat.snowflake.lane import AccountLane, PersonalizationLane
from chip_chat.vision import (
    AzureBlobStore,
    AzureImageAnalyzer,
    ImageModerator,
    PhotoIntake,
    UploadRejectedError,
)
from chip_chat.vision.intake import StoredPhoto
from chip_chat.vision.reader import read_upload_async
from chip_chat.web import (
    Persona,
    chat_page,
    opening_message,
    restart_message,
    stop_page,
    suggestions,
    unbound_opening_message,
)

__all__ = [
    "ChatReply",
    "ChatRequest",
    "EntryReply",
    "EntryRequest",
    "PhotoReply",
    "ReviseLine",
    "ReviseReply",
    "ReviseRequest",
    "Service",
    "SessionStore",
    "SwitchRequest",
    "VisitorProfile",
    "build_lanes",
    "build_photos",
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

_MAX_PHOTO_REF_CHARS = 256
"""Longest blob reference accepted on a chat request. ``container/YYYY-MM-DD/uuid.jpg``
is under seventy; the slack is for a container name nobody has chosen yet."""

_MAX_EDIT_LINES = 12
"""Lines an edited card may carry. The draft stores have their own quantity caps;
this one bounds the *request*, before anything is looked up."""

_STREAM_MEDIA_TYPE = "application/x-ndjson"
"""One JSON object per line. See :func:`_frames`."""

_HEARTBEAT_SECONDS = 10.0
"""How long a streamed turn may go silent before it says it is still there.

Well inside Container Apps ingress' sixty-second idle timeout, and far enough
inside it that a slow network between the two does not eat the margin."""

_ROBOTS = "User-agent: *\nDisallow: /\n"
"""The demo must never surface on the brand's own search terms."""

_photos_lock = threading.Lock()
_photos: "PhotoIntake | None" = None
_photos_built = False
"""The memo behind :func:`build_photos`. One process, one intake, built late."""

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

    photo: str | None = Field(default=None, max_length=_MAX_PHOTO_REF_CHARS)
    """A reference ``POST /api/photo`` returned on this session, if any.

    The photo lane's tool takes *"a reference the app has given you for a photo
    the visitor uploaded on this turn"* (``chip_chat.agent.surface``), so the
    reference has to reach the model somehow. It reaches it as a line of the
    visitor's own message, composed by :func:`_with_photo` -- which means the
    only references in play are ones this app minted, and a model that invents
    one is refused by ``BlobRef.parse`` rather than believed.

    Not an identity, and not spellable as one: a blob reference names a
    photograph, the app checks it came from this session's upload, and nothing
    about it selects a visitor.
    """


class ChatReply(BaseModel):
    """What the widget renders.

    Kept as a model even though ``POST /api/chat`` streams, because the frames
    it streams are this object taken apart: the prose, then the card. A single
    definition is what stops the streamed shape and the tested shape drifting.
    """

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


class SwitchRequest(BaseModel):
    """The persona switcher. It has no fields, and that is the design.

    Issue #69 asks that switching be one tap and that *"the old session's
    identity binding is fully released -- a switch is a new* ``demo_id`` *on a
    clean connection, not a mutation."* Both halves are decided on the server:
    the session being left is read from the cookie, the archetype to move away
    from is read from the store, and the customer arrived at is chosen by
    :meth:`chip_chat.api.visitors.VisitorDesk.switch`.

    A body with a field would be a body an attacker could put a persona in. So
    there is no field, and ``extra="forbid"`` means an added one is a 422.
    """

    model_config = ConfigDict(extra="forbid")


class ChipReply(BaseModel):
    """One suggested opening prompt, and the lane it exercises."""

    prompt: str
    lane: str


class EntryReply(BaseModel):
    """The answer to the name gate, and everything the first screen renders.

    Attributes:
        visitor: The assigned account, or ``None`` when this deployment has no
            synthetic population loaded. ``None`` is a decided state -- see
            :meth:`chip_chat.api.visitors.VisitorDesk.admit` -- and the widget
            renders the demo without an account rather than an error.
        opening: The sentence that tells the visitor who they have become,
            written by :func:`chip_chat.web.persona.opening_message` from the
            fixture's own narrative. Composed here rather than in the browser
            because the browser is never told enough to compose it.
        chips: The tappable opening prompts for this persona, spanning the
            menu, account, order and photo lanes.
        restarted: True when this reply is the far side of a persona switch, so
            the widget knows to clear the transcript before rendering it.
        stopped: True when the spend cap has the door shut. Still HTTP 200.
        message: The stop-state copy, when ``stopped``.
    """

    visitor: VisitorProfile | None = None
    opening: str = ""
    chips: list[ChipReply] = Field(default_factory=list)
    restarted: bool = False
    stopped: bool = False
    message: str | None = None


class ReviseLine(BaseModel):
    """One line of a card the visitor edited in place."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=20)
    selections: list[dict[str, str]] = Field(default_factory=list, max_length=24)
    """The modifiers still on the line after the edit, as the draft stores take
    them. Free-form strings because the two stores spell a portion differently
    and neither reads anything here it does not look up in a catalogue."""


class ReviseRequest(BaseModel):
    """A card edited in place, sent back to be priced again.

    PRD T3 and issue #68: *editable in place, items and modifiers changed
    without restarting the conversation*, and *editing produces a new priced
    draft*. It produces a new one rather than mutating the old, which is what
    makes an edited card unconfirmed again by construction --
    :meth:`chip_chat.api.drafts.DraftStore.revise` gives the argument at length.

    No model is called. An edit is a lookup and an arithmetic, so it costs
    nothing and returns in milliseconds, which is why this is its own route
    rather than a sentence typed into the conversation.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1, max_length=64)
    lines: list[ReviseLine] = Field(min_length=1, max_length=_MAX_EDIT_LINES)


class ReviseReply(BaseModel):
    """The re-priced card, or the reason the edit did not price up."""

    card: dict[str, Any] | None = None
    reply: str | None = None


class PhotoReply(BaseModel):
    """What an accepted upload hands back.

    Attributes:
        photo: The blob reference, which the next chat request carries. Not a
            URL: the bytes never come back out through this app.
        retention: What the visitor is promised about the photograph, shown
            beside it in the transcript.
        reply: Why an upload was refused, when it was. A sentence for a person,
            never a stack trace and never which ceiling it was.
    """

    photo: str | None = None
    retention: str | None = None
    reply: str | None = None


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

    def forget(self, session_id: str) -> None:
        """Drop a conversation's history.

        Called when a persona switch retires a session. The new conversation
        gets a new session id and would therefore have started empty anyway;
        this is the difference between "the old transcript is unreachable" and
        "the old transcript is gone", and issue #69 asks for the second.
        """
        with self._lock:
            self._conversations.pop(session_id, None)

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

    photos_seen: PhotoRegistry = field(default_factory=PhotoRegistry)
    """Which photographs each conversation may refer to. See
    :class:`~chip_chat.api.uploads.PhotoRegistry`."""

    uploads: UploadLimiter = field(default_factory=UploadLimiter)
    """The per-session and per-source ceilings on photographs.

    ``uploads.py`` shipped these correct, tested and with no caller, which is
    the same state ``guard.py`` was in before ``turns.py``: an upload route is
    what turns them into something that stops anybody. See
    :func:`create_app`'s ``POST /api/photo``.
    """

    photos: PhotoIntake | None = None
    """Stages 1 to 3 of the photo path, for a caller that assembles its own.

    ``None`` on every deployment, and ``None`` does **not** mean photographs are
    refused: the route falls back to :func:`build_photos`, which reads the
    environment on the first upload and memoises the result. The field is here
    so a test can install a double without one.

    **Built on first use rather than at start-up, and that is not a
    micro-optimisation.** Assembling the intake constructs two Azure SDK clients
    and a :class:`~azure.identity.DefaultAzureCredential`, and the first
    deployment of this route spent thirty-five seconds looking healthy and then
    stopped answering ``/healthz`` until Container Apps restarted it -- over and
    over. Whatever the credential chain was doing, it was doing it in a process
    whose whole job is to answer a one-second liveness probe. Nothing that talks
    to Azure belongs on the start-up path of an app that scales from zero: a
    visitor who never sends a photograph should not pay for the clients, and a
    probe should never be behind them. ``docs/deployment.md`` §3.11 is the
    write-up.
    """

    pool: VisitorPool | None = None
    """The connection pool, where one is configured.

    ``None`` on a deployment with no Snowflake credential, which used to be
    every deployment: ``pool.py``'s :class:`SessionConnection` was a protocol
    that nothing in this lockfile implemented.
    :class:`chip_chat.api.connect.ConnectorConnection` implements it now, so a
    deployment carrying an account and a private key gets a real pool and the
    two Snowflake-backed lanes over it. See :func:`build_service`.
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
        # Not `StaticRoster()`. An empty roster serves every visitor unbound,
        # and an unbound visitor is the empty account PRD §06 says loses the
        # demo. The committed export is the same twenty-eight customers
        # Snowflake holds, `demo_id`s included, and it is read *only* on the
        # no-connection path -- see `visitors.SHIPPED_ROSTER_PATH` and
        # docs/decisions/shipped-persona-roster.md.
        return VisitorDesk(shipped_roster(), store=store), None
    pool = VisitorPool(connect, sessions=store, size=pool_size)
    roster: PersonaRoster = SnowflakeRoster(pool)
    return VisitorDesk(roster, store=store), pool


def build_lanes(pool: VisitorPool | None) -> Lanes:
    """Assemble the Snowflake-backed lanes over ``pool``, or return none at all.

    Two of the five, and the two that were the whole of ``cc-lpy4``. The other
    three are somebody else's ticket and are deliberately untouched here:
    *knowledge* needs one :class:`~chip_chat.search.retrieve.Retriever` against
    the live alias (``cc-e1sr``), and *photo* needs the upload route and a
    production catalogue loader (``cc-mpd``).

    **A pool is the precondition, and it is the only one.** Both lanes take
    :meth:`~chip_chat.api.pool.VisitorPool.for_session` and nothing else --
    :data:`chip_chat.snowflake.reads.SessionCheckout` is that method's shape,
    which ``api/tests/test_read_lane_seam.py`` has been holding since before
    either end had a caller. So there is no configuration here beyond the
    connection: a deployment that can check a connection out can answer
    ``get_points_balance`` and ``get_usual_order`` from this visitor's own rows,
    and one that cannot has ``pool is None`` and gets :data:`NO_LANES`.

    **The account lane needs one thing the pool does not give it**, which is a
    REST host for Cortex Analyst, and one thing the container cannot give it,
    which is the ``snow`` CLI that :class:`~chip_chat.snowflake.cortex.CliJwt`
    shells out to. :class:`~chip_chat.api.connect.KeyPairJwt` is the
    :class:`~chip_chat.snowflake.cortex.TokenSource` that module anticipated,
    signing with the same key the connection authenticates with.

    Args:
        pool: The connection pool, or ``None`` on a deployment with no
            Snowflake credential.

    Returns:
        The lanes. :data:`NO_LANES` where there is no pool, which is the
        week-one slice and is an honest state rather than a hole.
    """
    if pool is None:
        return NO_LANES
    settings = SnowflakeSettings.from_env()
    if settings is None:  # pragma: no cover - a pool implies settings
        return NO_LANES
    key = PrivateKey()
    # Built once per process and held for its life, for the reason
    # `chip_chat.search.client.pooled_client` measures: a client per turn is a
    # TLS handshake per turn. Neither call touches the network here.
    transport = HttpAnalystTransport(
        _analyst_host(settings), pooled_client(), KeyPairJwt(settings, key)
    )
    return Lanes(
        account=AccountLane(pool.for_session, transport),
        personalization=PersonalizationLane(pool.for_session),
    )


def _analyst_host(settings: SnowflakeSettings) -> str:
    """Where ``ask_account_question`` posts, from the environment or the locator.

    :func:`~chip_chat.snowflake.cortex.host_from_env` refuses to guess and gives
    the reason: *"an account identifier assembled here and wrong is a lane that
    fails on every turn with a DNS error, and there is no defensible default."*
    That argument was about assembling one from the CLI's configuration file. It
    is not about this: ``SNOWFLAKE_ACCOUNT`` is the locator this same process is
    already opening connections with, and ``<locator>.snowflakecomputing.com``
    is that account's own REST host. Both forms were checked against the live
    account on 27 August 2026 and both answer ``200``.

    So the environment wins where it is set -- ``infra/terraform/compute.tf``
    sets it, and the organisation form is the one Snowflake's own documentation
    prints -- and the locator is the fallback rather than a refusal. The
    alternative is a deployment where one unset variable silently withdraws
    ``ask_account_question`` and puts ``get_points_balance`` back on the fixture,
    which is precisely the contradiction ``docs/public-demo.md`` §9 recorded.

    Args:
        settings: The connection this process is already using.

    Returns:
        The REST host, without a scheme.
    """
    try:
        return host_from_env()
    except AnalystError:
        derived = f"{settings.account}.snowflakecomputing.com"
        _log.info(
            "%s is unset; the account lane will post to %s, derived from %s",
            HOST_VARIABLE,
            derived,
            settings.account,
        )
        return derived


def build_service(
    lanes: Lanes | None = None,
    connect: Callable[[], SessionConnection] | None = None,
) -> Service:
    """Assemble the real service from the environment.

    Every ceiling comes from :meth:`~chip_chat.api.limits.SpendLimits.from_env`
    and every model deployment from
    :meth:`~chip_chat.agent.foundry.FoundryConfig.from_env`, so changing either
    on the Container App is a restart rather than a build. As of ``cc-lpy4`` the
    Snowflake connection is the same kind of value, which is what closed the gap
    this docstring used to describe: it said *"nothing supplies any of them
    yet"*, and the reason nothing did was that there was no connection factory to
    supply them with.

    Args:
        lanes: The backing services this deployment has. ``None`` -- the
            default -- means *read the environment*, exactly as the limits and
            the model deployment above do: :func:`build_lanes` wires the account
            and personalization lanes over whatever pool ``connect`` produced.
            Pass a value to override, which is what a test does; pass
            :data:`~chip_chat.agent.lanes.NO_LANES` to run the week-one slice on
            a machine that has credentials.

            Two of the five are still somebody else's: *knowledge* needs one
            :class:`~chip_chat.search.retrieve.Retriever` built per process
            against the live alias (``cc-e1sr``), and *photo* needs an upload
            route and a production catalogue loader (#62, ``cc-mpd``).

        connect: Opens a Snowflake connection, for the pool and the roster.
            ``None`` means :func:`~chip_chat.api.connect.snowflake_connect`
            decides from the environment, and it answers ``None`` itself on a
            deployment with no credential -- which is the shipped-roster path
            ``docs/decisions/shipped-persona-roster.md`` describes, still intact
            and still the thing that keeps a credential-less deployment from
            serving an empty account. :func:`build_visitors` keeps the older,
            literal meaning of the argument: it is the lower-level function and
            ``None`` there really is *no connection*.

    Returns:
        The assembled service.
    """
    resolved_connect = snowflake_connect() if connect is None else connect
    visitors, pool = build_visitors(resolved_connect)
    resolved_lanes = build_lanes(pool) if lanes is None else lanes
    _log.info("lanes wired on this deployment: %s", resolved_lanes.describe())
    limits = SpendLimits.from_env()
    return Service(
        gate=SpendGate(
            SpendGuard(limits, kill_switch=default_kill_switch()),
            lambda: AzureChatModel(FoundryConfig.from_env()),
            lanes=resolved_lanes,
        ),
        visitors=visitors,
        uploads=UploadLimiter(limits),
        pool=pool,
    )


def build_photos() -> PhotoIntake | None:
    """Assemble the upload path from the environment, or decline to.

    The wiring :mod:`chip_chat.vision`'s own module docstring writes out: a blob
    store for the bytes, Content Safety for stage 3, and no way to build the
    first without the second. Both read their endpoints from the environment
    Terraform sets on the Container App.

    Called on the **first upload** rather than at assembly, and memoised after.
    See :attr:`Service.photos` for what start-up construction cost, measured on
    a live deployment.

    Returns:
        The intake, or ``None`` where either half is unconfigured. ``None`` is
        the honest state of a deployment that cannot store a photograph, and the
        route says so in a sentence -- an upload button that accepts a
        photograph and then loses it would be worse than one that declines.
    """
    global _photos, _photos_built
    with _photos_lock:
        if _photos_built:
            return _photos
        _photos_built = True
        try:
            _photos = PhotoIntake(
                AzureBlobStore.from_env(),
                moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
            )
        except Exception:
            _log.warning(
                "no photo intake: the uploads container or the Content Safety "
                "endpoint is not configured, so photographs will be declined",
                exc_info=True,
            )
            _photos = None
        return _photos


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

    @application.get("/healthz/lanes")
    async def lane_health(request: Request) -> JSONResponse:
        """Which lanes are answering, and which are not wired at all.

        Issue #65 asks that per-lane health be *surfaced somewhere operable*,
        and this is that surface: :func:`chip_chat.agent.health.probe` asks each
        wired lane the cheapest question it answers and reports what came back.
        The route lives here rather than in ``agent/`` because ``agent/`` has no
        request path -- and because two of the answers are the app tier's to
        give. The probe needs a **bound** session, since the Snowflake-backed
        lanes check a connection out of #44's pool by session id and an unbound
        one would report two working lanes as down; and it needs to be told
        whether the ops API is available, because ``agent/`` does not import
        ``api/`` and that direction is load-bearing.

        Outside the spend cap and outside the rate limit, like ``/healthz`` and
        for the same reason. It calls no model: every probe is a lane's own
        cheapest read, and a lane that is not wired is not asked anything.

        Deliberately **not** a liveness probe. A lane being down is a fact an
        operator wants and not a reason to restart the container -- RFC-001 §10
        is explicit that a lane may fail and the conversation may not fail with
        it, so the platform's probe stays pointed at ``/healthz``, which answers
        for the process and nothing else.
        """
        session_id = _session_id(request)
        # Bind first. `probe` is documented as needing a session the visitor
        # store knows, and an operator curling this endpoint arrives with no
        # cookie at all -- which would otherwise report the account and
        # personalization lanes as down on a deployment where they are fine.
        resolved.visitors.admit(session_id)
        report = probe(
            resolved.gate.lanes,
            session_id=session_id,
            ordering_available=None,
        )
        response = JSONResponse(report.as_dict())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

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
            payload = _entry_reply(
                resolved.visitors.admit(session_id, display_name=body.name),
                restarted=False,
            )
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    @application.post("/api/switch")
    async def switch(request: Request, body: SwitchRequest) -> Response:
        """Become a different synthetic customer, on a conversation that restarts.

        One tap from the chat surface, and four things happen in the order that
        makes the release real rather than described:

        1. a **new** session id is minted, so the browser leaves holding a
           different cookie than the one it arrived with;
        2. :meth:`~chip_chat.api.visitors.VisitorDesk.switch` releases the old
           binding from the store *before* it makes the new one -- and the store
           is what :meth:`~chip_chat.api.pool.VisitorPool.for_session` resolves
           against, so a released session checks out nothing;
        3. the old conversation, its drafts and its photographs are dropped
           here, because "no data from the previous persona survives" is not
           satisfied by a new identity in front of an old transcript; and
        4. the archetype the visitor is leaving is excluded from the choice, so
           the switch shows them the Lapsed Regular after the Regular rather
           than a reshuffle.

        ``body`` has no fields. See :class:`SwitchRequest`.
        """
        del body
        leaving = _session_id(request)
        arriving = _new_session_id()
        if (stop := resolved.gate.entry_state()) is not None:
            payload = EntryReply(stopped=True, message=stop.message)
        else:
            payload = _entry_reply(
                resolved.visitors.switch(leaving, arriving), restarted=True
            )
        resolved.sessions.forget(leaving)
        resolved.photos_seen.release(leaving)
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, arriving, secure=_is_https(request))
        return response

    @application.post("/api/draft/revise")
    async def revise(request: Request, body: ReviseRequest) -> Response:
        """Re-price a card the visitor edited, and hand back the new one.

        The whole of "editable in place". No model is called and no confirmation
        is carried across: the returned draft is a different draft with a
        different id, unconfirmed, which is what stops a confirmation granted
        for one basket applying to another.
        """
        session_id = _session_id(request)
        payload = _revise(resolved, session_id, body)
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    @application.post("/api/photo")
    async def upload_photo(request: Request) -> Response:
        """Accept one photograph, or say why not.

        The body is the image itself rather than a multipart form: the intake
        wants a stream it can stop reading, and the ceiling that matters is on
        bytes read rather than on fields parsed.
        :meth:`~chip_chat.vision.intake.PhotoIntake.accept_stream` is what
        enforces it, and :class:`~chip_chat.api.uploads.UploadLimiter` runs
        first so that a flood is refused before a byte is read.
        """
        session_id = _session_id(request)
        payload = await _accept_photo(resolved, request, session_id)
        response = JSONResponse(payload.model_dump())
        _set_session_cookie(response, session_id, secure=_is_https(request))
        return response

    @application.post("/api/chat")
    async def chat(request: Request, body: ChatRequest) -> Response:
        """Run one turn: guard, then agent, then render.

        Answers in one of two shapes, chosen by ``Accept``. A caller that asks
        for :data:`_STREAM_MEDIA_TYPE` gets the turn as newline-delimited JSON
        frames, which is what the widget asks for and why a two-second turn
        paints rather than hangs; anything else gets one
        :class:`ChatReply` object, which is what a test, a curl and an eval
        harness want. Both are the same turn and the same fields -- see
        :func:`_frames`.
        """
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
        message = _with_photo(resolved, session_id, body)
        response: Response
        if _wants_stream(request):
            response = StreamingResponse(
                _stream(resolved, conversation, body, message, source_address, admitted),
                media_type=_STREAM_MEDIA_TYPE,
            )
        else:
            # Off the event loop, and this is not a nicety. A turn is a model
            # call and several seconds of blocking work; run in the handler it
            # would hold the only loop this process has, and the first thing
            # that stops being answered is `/healthz` -- so Container Apps
            # concludes the container is dead and restarts it *in the middle of
            # the visitor's turn*. That is not a hypothetical: it is what the
            # first deployment of this route did to every conversation.
            # `_run_turn`'s own docstring says it is synchronous on the strength
            # of FastAPI putting a `def` handler on a worker thread -- but this
            # handler is `async def`, because it has to read the request before
            # deciding which shape to answer in, so the threadpool has to be
            # asked for explicitly. The streaming branch above gets it for free:
            # Starlette iterates a synchronous generator on a worker thread.
            payload = await run_in_threadpool(
                _run_turn,
                resolved,
                conversation,
                body,
                message,
                source_address,
                admitted,
            )
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


def _entry_reply(admitted: VisitorSession | None, *, restarted: bool) -> EntryReply:
    """Render an assignment as the whole of what the first screen needs.

    The opening message and the chips are written here rather than in the
    browser for the reason the profile omits ``demo_id``: the browser is told
    who it has become and never enough to have chosen.

    Args:
        admitted: The assigned customer, or ``None`` on a deployment with no
            synthetic population loaded.
        restarted: Whether this is the far side of a persona switch, which
            changes the copy from a greeting to a restart notice.

    Returns:
        The reply, with an opening message that names the store, the balance and
        the characteristic order even when there is no persona -- in which case
        it says there is none rather than inventing one.
    """
    profile = _profile(admitted)
    if profile is None:
        return EntryReply(opening=unbound_opening_message(), restarted=restarted)
    persona = _persona(profile)
    return EntryReply(
        visitor=profile,
        opening=(restart_message if restarted else opening_message)(persona),
        chips=[
            ChipReply(prompt=chip.prompt, lane=chip.lane) for chip in suggestions(persona)
        ],
        restarted=restarted,
    )


def _persona(profile: VisitorProfile) -> Persona:
    """Project an assigned account onto the copy's view of it.

    Two narrow types rather than one wide one, on purpose: ``web/`` renders the
    sentence and must not be able to name the visitor, so what crosses is a
    value with no identity in it.
    """
    return Persona(
        persona_id=profile.persona_id,
        label=profile.label,
        display_name=profile.display_name,
        narrative=profile.narrative,
        home_store_name=profile.home_store_name,
        points_balance=profile.points_balance,
        order_count=profile.order_count or 0,
    )


def _revise(service: Service, session_id: str, body: ReviseRequest) -> ReviseReply:
    """Price an edited card again, on the desk that minted the original.

    The desk is :attr:`chip_chat.api.turns.SpendGate.desk` -- the same one the
    turn used -- because the card being edited came off it. Reaching for a
    different store here would re-price against a catalogue the conversation has
    never seen and hand back a total the model cannot explain.

    Args:
        service: The assembled service.
        session_id: The conversation, resolved from the cookie.
        body: The edited lines.

    Returns:
        The new card, or a sentence saying why the edit did not price up. A
        rejection is a normal answer here: an edit that names something not on
        the menu is a visitor's mistake, not a fault.
    """
    desk = service.gate.desk
    items = [
        {
            "item_id": line.item_id,
            "quantity": line.quantity,
            "selections": line.selections,
        }
        for line in body.lines
    ]
    try:
        draft = desk.propose(session_id, items)
    except Exception as error:  # OrderRejectedError, and anything it wraps.
        detail = getattr(error, "message", None) or str(error)
        return ReviseReply(reply=f"I could not re-price that: {detail}")
    return ReviseReply(
        card=dict(draft.as_card()),
        reply="Re-priced. Nothing is placed until you press the button.",
    )


async def _accept_photo(
    service: Service, request: Request, session_id: str
) -> PhotoReply:
    """Run the upload path for one request, and answer in a sentence either way.

    Order matters and is the same order the spend cap uses: the ceiling that an
    attacker cannot re-roll for free runs first, and nothing is read from the
    socket until it has admitted.

    Args:
        service: The assembled service.
        request: The request, whose body is the image.
        session_id: The conversation, resolved from the cookie.

    Returns:
        The reference the next chat request carries, or the refusal.
    """
    intake = service.photos if service.photos is not None else build_photos()
    if intake is None:
        return PhotoReply(
            reply=(
                "Photographs are not wired up on this deployment, so I cannot "
                "look at one just now -- tell me what you want instead."
            )
        )
    stop = service.uploads.check(
        session_id=session_id, source_address=_source_address(request)
    )
    if stop is not None:
        return PhotoReply(reply=stop.message)
    try:
        # The read is genuinely asynchronous and bounded; the four gates behind
        # it are not -- stage 3 is an HTTP call to Content Safety and the write
        # is a blob upload. Both go to a worker thread for the reason the chat
        # route gives at length: a handler that blocks this loop stops answering
        # the liveness probe, and the platform's answer to that is a restart.
        payload = await read_upload_async(
            _RequestBody(request),
            declared_length=_declared_length(request.headers.get("content-length")),
            limits=intake.limits,
        )
        stored = await run_in_threadpool(
            _moderate_and_store,
            service,
            intake,
            payload,
            session_id,
            request.headers.get("content-type"),
        )
    except UploadRejectedError as refusal:
        return PhotoReply(reply=str(refusal))
    except Exception:
        _log.exception("photo upload failed", extra={"session_id": session_id})
        return PhotoReply(
            reply="I could not take that photo in just then. Try again in a moment."
        )
    reference = str(stored.blob_ref)
    service.photos_seen.record(session_id, reference)
    return PhotoReply(photo=reference, retention=stored.retention_notice)


def _moderate_and_store(
    service: Service,
    intake: PhotoIntake,
    payload: bytes,
    session_id: str,
    declared_media_type: str | None,
) -> "StoredPhoto":
    """Run stages 1 to 3 and the write, inside the turn they belong to.

    The span schema is what makes this a function rather than a line. RFC-001
    §09 puts ``guard.content_safety`` under ``chat.turn`` -- image moderation is
    something that happens *on a turn*, before inference, and a moderation span
    hanging off the trace root is a moderation nobody can attribute to a
    conversation. :func:`chip_chat.otel.spans.content_safety` enforces that by
    raising, which is how the first deployed upload failed: ``guard.content_safety
    must be a child of chat.turn, but was opened under the trace root``.

    So the upload opens the turn it is the first half of. That is not a
    workaround: handing over a photograph *is* a visitor turn -- issue #68 asks
    that the photo appear in the transcript *"next to what Cilantro thought it
    saw"*, which is a question and an answer. The message the turn records is
    ``(photo upload)`` rather than a sentence the visitor did not type, so an
    eval reading ``chat.turn`` inputs can tell the two apart.
    """
    admitted = service.visitors.visitor(session_id)
    conversation = service.sessions.get(
        session_id, tools=offered_tools(service.gate.lanes)
    )
    with chat_turn(
        session_id=session_id,
        turn_index=conversation.next_turn_index(),
        message="(photo upload)",
        persona_id=admitted.persona_id if admitted is not None else None,
        demo_id=admitted.demo_id if admitted is not None else None,
        prompt_version=PROMPT_VERSION,
    ):
        return intake.accept(payload, declared_media_type=declared_media_type)


class _RequestBody:
    """Starlette's request body, as the awaitable ``read`` the intake wants.

    :func:`chip_chat.vision.reader.read_upload_async` reads in bounded chunks so
    that an oversized upload is refused partway through rather than after it has
    been buffered. ``Request.stream()`` is an async *iterator*, which cannot be
    asked for a size, so this adapter keeps at most one transport chunk in hand
    and hands out slices of it. Nothing here relaxes the ceiling: the budget is
    the reader's and this only feeds it.
    """

    __slots__ = ("_buffer", "_chunks", "_done")

    def __init__(self, request: Request) -> None:
        self._chunks = request.stream().__aiter__()
        self._buffer = b""
        self._done = False

    async def read(self, size: int, /) -> bytes:
        while not self._buffer and not self._done:
            try:
                self._buffer = await self._chunks.__anext__()
            except StopAsyncIteration:
                self._done = True
        taken, self._buffer = self._buffer[:size], self._buffer[size:]
        return taken


def _declared_length(raw: str | None) -> int | None:
    """Read a ``Content-Length`` header, or decide nothing was claimed."""
    if raw is None:
        return None
    try:
        declared = int(raw)
    except ValueError:
        return None
    return declared if declared >= 0 else None


def _with_photo(service: Service, session_id: str, body: ChatRequest) -> str:
    """Return the message to run, with a photo reference attached where there is one.

    The reference is checked against
    :class:`~chip_chat.api.uploads.PhotoRegistry` first: a well-formed reference
    this session did not upload names no photograph, exactly as a well-formed
    draft id belonging to somebody else names no draft. A reference that passes
    is appended to the visitor's own message, which is how the tool surface says
    it should arrive -- *"a reference the app has given you for a photo the
    visitor uploaded on this turn"*.

    Args:
        service: The assembled service.
        session_id: The conversation, resolved from the cookie.
        body: The request.

    Returns:
        The message text the turn runs.
    """
    reference = (body.photo or "").strip()
    if not reference or not service.photos_seen.holds(session_id, reference):
        return body.message
    return (
        f"{body.message}\n\n"
        f"[The visitor uploaded a photograph on this turn. Its reference is "
        f"{reference}. Pass that string to match_meal_from_photo; do not invent "
        f"another.]"
    )


def _wants_stream(request: Request) -> bool:
    """Whether this caller asked for the turn as frames rather than as an object."""
    return _STREAM_MEDIA_TYPE in request.headers.get("accept", "")


def _stream(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    message: str,
    source_address: str,
    admitted: VisitorSession | None,
) -> Iterator[bytes]:
    """Yield one turn as newline-delimited JSON frames.

    A synchronous generator, which Starlette iterates on a worker thread, so the
    blocking work inside :func:`_run_turn` happens off the event loop exactly as
    it does for a ``def`` handler. The first frame is written before the turn
    starts, which is what gives the widget a response to render into rather than
    a request that has not answered yet.

    **What is streamed and what is not, said plainly.** The frames are real: the
    header and the ``open`` frame reach the browser immediately, and the card
    arrives as its own frame the moment the turn produces one. The *tokens* are
    not: :class:`chip_chat.agent.model.ChatModel` has one method and it returns a
    finished reply, so the prose is chunked here after the turn rather than
    forwarded from the provider as it arrives. Token streaming is an ``agent/``
    change -- a second method on that protocol and a loop that can yield
    mid-step -- and this route is written so that landing it means replacing the
    body of :func:`_chunks` and nothing else.
    """
    yield _frame({"type": "open"})
    # A heartbeat, and it is not cosmetic. Container Apps ingress closes a
    # response that has sent nothing for sixty seconds, and a turn against a
    # rate-limited reasoning deployment routinely takes longer than that: the
    # JSON shape of this route dies at exactly 60.19 s with the answer already
    # written, which is the worst possible failure -- the visitor is charged for
    # a turn they never see. So the turn runs on its own thread and this
    # generator keeps the response alive while it does. See
    # docs/deployment.md 3.12.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            _run_turn, service, conversation, body, message, source_address, admitted
        )
        while True:
            try:
                payload = pending.result(timeout=_HEARTBEAT_SECONDS)
                break
            except FutureTimeout:
                yield _frame({"type": "waiting"})
    yield from _frames(payload)


def _frames(payload: ChatReply) -> Iterator[bytes]:
    """Take one reply apart into the frames the widget renders."""
    for chunk in _chunks(payload.reply):
        yield _frame({"type": "text", "text": chunk})
    if payload.card is not None:
        yield _frame({"type": "card", "card": payload.card, "receipt": payload.receipt})
    yield _frame({"type": "end", "stopped": payload.stopped})


def _chunks(reply: str, size: int = 120) -> Iterator[str]:
    """Cut a finished reply into pieces a browser can paint as they land.

    Cut on whitespace so a chunk boundary never lands inside a word, which would
    make the text visibly reflow as it arrives.
    """
    if not reply:
        return
    words = reply.split(" ")
    held: list[str] = []
    length = 0
    for word in words:
        held.append(word)
        length += len(word) + 1
        if length >= size:
            yield " ".join(held) + " "
            held, length = [], 0
    if held:
        yield " ".join(held)


def _frame(payload: Mapping[str, Any]) -> bytes:
    """Render one frame. One JSON object, one newline, no blank lines."""
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _run_turn(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    message: str,
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
        body: The request. Read for the confirmation only; the text the model
            sees is ``message``.
        message: What to run, which is the visitor's message plus whatever
            :func:`_with_photo` attached to it.
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
            message=message,
            persona_id=(
                admitted.persona_id if admitted is not None else ACCOUNT.persona_id
            ),
            demo_id=admitted.demo_id if admitted is not None else None,
            prompt_version=PROMPT_VERSION,
        ) as turn,
        service.gate.turn(
            session_id=session_id,
            source_address=source_address,
            # #79: the text goes to the gate, which moderates it before a
            # FundedTurn exists. Passing it here rather than screening in
            # this function is what stops a later route from reordering the
            # check -- there is no route to a model that skips the gate.
            message=message,
        ) as funded,
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
                message,
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
