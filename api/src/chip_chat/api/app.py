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
import os
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from queue import Empty, Queue
from time import monotonic
from typing import Any, Final

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
from chip_chat.agent.envelope import ClaimClass, ProseStream, ResponseEnvelope
from chip_chat.agent.health import probe
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import PROMPT_VERSION, Conversation
from chip_chat.agent.tools import offered_tools
from chip_chat.api.confirmations import ConfirmationLedger
from chip_chat.api.connect import (
    KeyPairJwt,
    PrivateKey,
    SnowflakeSettings,
    snowflake_connect,
)
from chip_chat.api.drafts import DraftStore
from chip_chat.api.grants import OPS_KEY_VARIABLE, GrantSigner
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import (
    CachedKillSwitch,
    EnvironmentKillSwitch,
    FileKillSwitch,
    any_of,
)
from chip_chat.api.limits import SpendLimits
from chip_chat.api.menu import build_catalog
from chip_chat.api.opsclient import OpsClient
from chip_chat.api.orderdesk import OpsDesk, RewardLookup
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
from chip_chat.catalog import MenuCatalog
from chip_chat.otel import (
    TelemetryConfig,
    TokenUsage,
    ToolName,
    chat_turn,
    configure_tracing,
    render_response,
    shutdown_tracing,
)
from chip_chat.search import SearchError
from chip_chat.search.client import (
    SEARCH_SCOPE,
    EntraToken,
    HttpSearchService,
    endpoint_from_env,
)
from chip_chat.search.client import pooled_client as search_pooled_client
from chip_chat.search.lane import KnowledgeLane
from chip_chat.search.retrieve import Retriever
from chip_chat.search.schema import ALIAS as DEFAULT_SEARCH_ALIAS
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
    AzureVisionModel,
    ImageModerator,
    MealDescriber,
    MealMatcher,
    PhotoIntake,
    PhotoLane,
    SlotRules,
    UploadRejectedError,
    Vocabulary,
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
    "build_action_lane",
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

_OBJECT_MEDIA_TYPE = "application/json"
"""One JSON object, sent incrementally. See :func:`_object`."""

_HEARTBEAT_SECONDS = 10.0

_DELTA_POLL_SECONDS = 0.05
"""How often the streamed shape looks for another fragment of the answer.

Nothing to do with :data:`_HEARTBEAT_SECONDS`, which answers a different
question -- how long ingress will tolerate silence -- and is three orders of
magnitude larger. Sharing one number between them is the bug this constant was
added to fix: fragments were being released on the heartbeat's schedule, so a
streamed answer arrived in ten-second batches and the first token was no earlier
than the last. Fifty milliseconds is below the threshold at which text appearing
reads as continuous, and the cost of a poll that finds nothing is one timed wait
on a queue.
"""
"""How long a turn may go silent before its response says it is still there.

Well inside Container Apps ingress' sixty-second idle timeout, and far enough
inside it that a slow network between the two does not eat the margin."""

_OBJECT_HEARTBEAT = b" "
"""What the object shape sends while its turn is still running.

A space, and the reason it is a space rather than a frame is RFC 8259 §2:
whitespace is insignificant *before* a JSON value, so a document preceded by any
number of them decodes to exactly the object it would have decoded to on its own.
``json.loads``, ``response.json()``, ``jq`` and every other parser this route has
ever been read by are unaffected, which is what lets the object shape be held
open without becoming a different protocol that its callers would have to be
taught. See :func:`_object`."""

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

    citations: list[dict[str, str]] = Field(default_factory=list)
    """The sources the app draws under :attr:`reply`, per decision D9.

    A field on the response rather than a sentence inside ``reply``, and that is
    the whole of the decision rather than a formatting preference: a source the
    model wrote as prose is a source the model could have invented, so what
    crosses from the model is a set of ids and every field here comes off a
    passage ``retriever.search`` actually returned --
    :func:`chip_chat.agent.envelope.render` is where an id that was not
    retrieved is dropped instead of resolved.

    Four keys per citation: ``id``, ``label``, ``source_url`` and
    ``harvested_at``. Empty on every turn that made no food or policy claim, and
    on every turn the model wrote no envelope for.
    """

    claim_class: str = ClaimClass.NONE.value
    """What kind of claim :attr:`reply` makes: ``food``, ``policy``,
    ``allergen``, ``account`` or ``none``.

    The widget needs it because D9 draws allergen answers differently -- source
    adjacent to the claim, harvest date visible without interaction, never
    deduplicated -- and PRD K2's uncited-claim metric needs it because *"a food
    claim with no citation"* is a rule over these two fields rather than a
    judgement about a sentence.
    """


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
        self,
        session_id: str,
        *,
        tools: tuple[ToolName, ...] | None = None,
        lanes: Lanes = NO_LANES,
    ) -> Conversation:
        """Return the conversation for ``session_id``, creating it if new.

        Args:
            session_id: The conversation to fetch.
            tools: The tools this deployment has registered, which a new
                conversation's runtime context names. Read from
                :func:`~chip_chat.agent.tools.offered_tools` at the call site,
                because which lanes are answerable is a property of the
                assembled service and not of this store.
            lanes: What is wired, for the two paragraphs of
                :func:`~chip_chat.agent.loop.runtime_context` whose truth depends
                on it. Passed for the same reason ``tools`` is and read at the
                same call site -- and it is a separate argument because the tool
                list cannot answer the question: ``get_points_balance`` is
                offered whether or not the account lane exists behind it.
        """
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                if len(self._conversations) >= self._max:
                    # Insertion-ordered, so this is the least recently started.
                    self._conversations.pop(next(iter(self._conversations)))
                conversation = (
                    Conversation(session_id=session_id, lanes=lanes)
                    if tools is None
                    else Conversation(session_id=session_id, tools=tools, lanes=lanes)
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


WITHHELD_TOOLS: Final[frozenset[ToolName]] = frozenset({ToolName.GET_RECOMMENDATIONS})
"""Tools this deployment does not offer the model even where their lane is wired.

One name, and it is here rather than in :mod:`chip_chat.agent.lanes` because it
is a fact about *this deployment's data* rather than about how a lane works.
``get_recommendations`` reads ``CHIP_CHAT.MARTS.recommendations``, and that
table does not exist on the account:
:data:`chip_chat.snowflake.reads.RECOMMENDATIONS_MART` spells the name once and,
in the same docstring, the reason nothing publishes it -- RFC-001 §04 fixes four
serving marts and this would be a fifth, so creating it is a schema decision
(bead ``cc-afo5``) and not something a tool ticket may take on the side.

Measured on the deployed app on 27 August 2026, every call came back::

    {'declined': 'PERSONALIZATION_LANE_UNAVAILABLE',
     'reason': "ProgrammingError: 002003 (42S02): SQL compilation error:
                Object 'CHIP_CHAT.MARTS.RECOMMENDATIONS' does not exist"}

which is the failure :mod:`chip_chat.agent.lanes` argues against in its own
words -- *a tool definition the model can see and nothing can answer is worse
than an absent one* -- and the trace made it look like a personalization outage
when the lane was up and answering ``get_usual_order`` beside it.

**Withholding the tool is not withholding the lane, and the difference is the
whole point.** ``cc-lpy4`` wiring personalization is what moved
``get_usual_order`` off the hardcoded fixture, which was half of
``docs/public-demo.md`` §9; that stays. What goes is one name on the tool list.

The day ``cc-afo5`` lands and the mart is published, this frozenset goes back to
empty and nothing else changes. Until then the withdrawal is reported by
:meth:`chip_chat.agent.lanes.Lanes.withdrawn` on the start-up log and by
``GET /healthz/lanes``, so it is a state somebody can read rather than a silence.
"""


def build_lanes(pool: VisitorPool | None) -> Lanes:
    """Assemble every lane this deployment can build, and withhold the rest.

    Four of the five, and the fifth -- *action* -- is assembled separately by
    :func:`build_action_lane` because it hangs off the ops API rather than off a
    backing store. The two that were the whole of ``cc-lpy4`` are here, and so
    are the two that used to be somebody else's ticket:
    :func:`build_knowledge_lane` (``cc-e1sr``) and :func:`build_photo_lane`
    (``cc-mpd``). Issue #106 is what closed them: with neither wired,
    ``search_menu_knowledge`` answered out of
    :data:`chip_chat.agent.hardcoded.MENU`, which is three items, and no amount
    of loading Snowflake would have changed a word of it.

    **The four preconditions are independent, and that is the point.** Knowledge
    needs a search endpoint, photo needs a catalogue and a vision deployment,
    and account and personalization need a Snowflake pool. A deployment missing
    any one of them gets the other three rather than :data:`NO_LANES` -- which
    is why the pool check below returns a populated :class:`Lanes` instead of
    the constant it used to.

    **A pool is the precondition for two of them.** Both lanes take
    :meth:`~chip_chat.api.pool.VisitorPool.for_session` and nothing else --
    :data:`chip_chat.snowflake.reads.SessionCheckout` is that method's shape,
    which ``api/tests/test_read_lane_seam.py`` has been holding since before
    either end had a caller. So there is no configuration here beyond the
    connection: a deployment that can check a connection out can answer
    ``get_points_balance`` and ``get_usual_order`` from this visitor's own rows,
    and one that cannot has ``pool is None`` and gets neither of them.

    **The account lane needs one thing the pool does not give it**, which is a
    REST host for Cortex Analyst, and one thing the container cannot give it,
    which is the ``snow`` CLI that :class:`~chip_chat.snowflake.cortex.CliJwt`
    shells out to. :class:`~chip_chat.api.connect.KeyPairJwt` is the
    :class:`~chip_chat.snowflake.cortex.TokenSource` that module anticipated,
    signing with the same key the connection authenticates with.

    Args:
        pool: The connection pool, or ``None`` on a deployment with no
            Snowflake credential.

    **And one tool is withheld from a lane that is wired.** Every other absence
    here is a lane that could not be built; ``get_recommendations`` is a lane
    that was built and a table that was never published, so it is withdrawn by
    name through :attr:`chip_chat.agent.lanes.Lanes.withheld` rather than by
    taking the personalization lane away from ``get_usual_order`` beside it.
    :data:`WITHHELD_TOOLS` carries the argument and the measurement.

    Returns:
        The lanes this deployment can actually answer with. Every field is
        independently ``None``, and a ``None`` is an honest state rather than a
        hole: the tool it backs is either withdrawn from the model's list or
        left on the fixture that says in its own result what it is reading.
    """
    knowledge = build_knowledge_lane()
    photo = build_photo_lane()
    settings = None if pool is None else SnowflakeSettings.from_env()
    if pool is None or settings is None:
        return Lanes(knowledge=knowledge, photo=photo, withheld=WITHHELD_TOOLS)
    key = PrivateKey()
    # Built once per process and held for its life, for the reason
    # `chip_chat.search.client.pooled_client` measures: a client per turn is a
    # TLS handshake per turn. Neither call touches the network here.
    transport = HttpAnalystTransport(
        _analyst_host(settings), pooled_client(), KeyPairJwt(settings, key)
    )
    return Lanes(
        knowledge=knowledge,
        account=AccountLane(pool.for_session, transport),
        personalization=PersonalizationLane(pool.for_session),
        photo=photo,
        withheld=WITHHELD_TOOLS,
    )


SEARCH_ALIAS_VARIABLE: Final = "AZURE_SEARCH_INDEX_ALIAS"
"""Which alias the knowledge lane queries. ``infra/terraform/compute.tf`` sets it.

An *alias*, never an index name, and the variable is named for what it holds so
that nobody is tempted to point it at one. RFC-001 section 08 rebuilds the index
under a name that says which corpus release it carries and swaps the alias in a
single write; an application that learned an index name would pin itself to one
release and stop moving the week after somebody rebuilt it.
"""


def build_knowledge_lane() -> KnowledgeLane | None:
    """Assemble hybrid retrieval over the live corpus alias, or decline to.

    One :class:`~chip_chat.search.retrieve.Retriever` per process, holding one
    pooled ``httpx.Client`` for the life of the process. That is the whole of
    the latency argument :class:`~chip_chat.search.retrieve.Retriever` records
    against measurements taken from this container: 11.2 ms on a warm pooled
    connection against 84.3 ms on a fresh one. A retriever per turn would be a
    TLS handshake per turn and nothing further down recovers it.

    Nothing here touches the network. :class:`~chip_chat.search.client.EntraToken`
    defers its credential chain to the first call, so a process that never
    searches never resolves an identity, and the start-up cost of this function
    is a URL parse.

    Returns:
        The lane, or ``None`` where ``AZURE_SEARCH_ENDPOINT`` is unset. ``None``
        is the week-one slice: ``search_menu_knowledge`` falls back to
        :data:`chip_chat.agent.hardcoded.MENU` and says in its own result that
        it is reading a three-item menu, which is honest and was, until #106,
        what the deployment actually served.
    """
    try:
        endpoint = endpoint_from_env()
    except SearchError:
        _log.warning(
            "no knowledge lane: AZURE_SEARCH_ENDPOINT is unset, so "
            "search_menu_knowledge will answer from the hardcoded menu"
        )
        return None
    alias = os.environ.get(SEARCH_ALIAS_VARIABLE, "").strip() or DEFAULT_SEARCH_ALIAS
    service = HttpSearchService(
        endpoint, search_pooled_client(), EntraToken(SEARCH_SCOPE)
    )
    _log.info("knowledge lane wired against %s alias %r", endpoint, alias)
    return KnowledgeLane(Retriever(service, alias=alias))


def build_photo_lane() -> PhotoLane | None:
    """Assemble stage 4 and stage 5 over the published catalogue, or decline to.

    Three things have to be there and any one of them missing is a decline: the
    catalogue :func:`~chip_chat.api.menu.build_catalog` reads from blob storage,
    the generated vocabulary named by ``CHIP_CHAT_VISION_VOCABULARY``, and a
    vision deployment. The uploads container is a fourth, and it is the same
    store :func:`build_photos` writes to -- the describer reads the photograph
    back itself rather than being handed bytes, so that the image reaches
    exactly one place.

    **The two stages are built from one catalogue on purpose.** The vocabulary
    carries the content version it was generated from and
    :meth:`~chip_chat.vision.matcher.MealMatcher.resolve` checks it, so a
    vocabulary shipped in the image and a catalogue published to blob storage
    that came from different builds raise :class:`CatalogueDriftError` on the
    first photograph rather than resolving a term that has moved. That is a loud
    failure and it is the intended one: the alternative is a matcher quietly
    resolving last month's salsa.

    Returns:
        The lane, or ``None`` where any part is unconfigured. ``None`` withdraws
        ``match_meal_from_photo`` from the tool list entirely -- the model is
        never offered a tool nothing can answer -- and the upload route says so
        in a sentence.
    """
    catalog = build_catalog()
    if catalog is None:
        _log.warning(
            "no photo lane: no published catalogue in blob storage, so "
            "match_meal_from_photo is not offered to the model"
        )
        return None
    try:
        vocabulary = Vocabulary.from_env()
        model = AzureVisionModel.from_env()
        images = AzureBlobStore.from_env()
    except Exception:
        _log.warning(
            "no photo lane: the generated vocabulary, the vision deployment or "
            "the uploads container is not configured, so match_meal_from_photo "
            "is not offered to the model",
            exc_info=True,
        )
        return None
    _log.info(
        "photo lane wired against catalogue %s and vocabulary %s",
        catalog.content_version()[:12],
        (vocabulary.content_version or "unversioned")[:12],
    )
    return PhotoLane(
        MealDescriber(model, images=images, vocabulary=vocabulary),
        MealMatcher(catalog, rules=SlotRules.from_env()),
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


OPS_URL_VARIABLE: Final = "CHIP_CHAT_OPS_URL"
"""Where the deployed ops API answers. ``infra/terraform/compute.tf`` sets it.

Unset is the week-one slice and an honest state rather than a hole: the app runs
:class:`~chip_chat.agent.orders.OrderDesk`, ``GET /healthz/lanes`` reports the
action lane ``not_wired`` with the sentence that says drafts are proposed and
nothing is written, and the three write tools that need a real account are not
offered to the model at all.
"""

OPS_FUNCTION_KEY_VARIABLE: Final = "CHIP_CHAT_OPS_FUNCTION_KEY"
"""The platform key for the Functions host, which runs at ``AuthLevel.FUNCTION``.

Separate from :data:`~chip_chat.api.grants.OPS_KEY_VARIABLE`, which is the
*application's* secret. :class:`~chip_chat.api.opsclient.OpsClient` explains why
both are sent and why neither makes the other redundant.
"""


def build_action_lane(
    visitors: VisitorDesk,
    *,
    catalog: MenuCatalog | None = None,
    rewards: RewardLookup | None = None,
) -> OpsDesk | None:
    """Assemble the action lane over the deployed ops API, or decline to.

    The last of the five lanes to be wired, and the reason it was last is
    recorded at length in ``docs/ops-api.md``: the ops API has been deployed,
    credentialled and refusing correctly for some time, and the chat app did not
    call it because a draft minted in this process is invisible to a service in
    another one. ``docs/decisions/confirmation-grants.md`` is how that was
    resolved without giving this tier a write credential and without inventing a
    shared store; :mod:`chip_chat.api.grants` is the mechanism.

    **Three settings and a catalogue, and every one of them is a precondition
    rather than an option.** Without a URL there is nothing to call. Without the
    ops key there is no secret to authenticate with *and* no key to sign a
    confirmation with, which are the same secret by construction. Without a
    catalogue there is nothing to price a draft against, and a draft priced
    against nothing would be a card whose total the ops API's own procedure
    would reject -- so declining here is the same refusal, made where it is
    legible.

    The function key is deliberately *not* a precondition. A host at
    ``AuthLevel.ANONYMOUS`` needs none, the ops key is the check that is made in
    this repository's own code, and a deployment that forgot the platform key
    gets a 401 from Azure with a name on it rather than an app that would not
    start.

    Args:
        visitors: The session-to-visitor binding. The action lane's only source
            of a ``demo_id``.
        catalog: The published catalogue. ``None`` reads it through
            :func:`chip_chat.api.menu.build_catalog`, which is #66's production
            loader -- the same one the Functions host uses, so the two tiers
            price a draft identically or neither does.
        rewards: How a redemption card learns what a reward is called and costs.
            See :data:`~chip_chat.api.orderdesk.RewardLookup`.

    Returns:
        The desk, or ``None`` where this deployment has no ops API.
    """
    base_url = os.environ.get(OPS_URL_VARIABLE, "").strip()
    ops_key = os.environ.get(OPS_KEY_VARIABLE, "").strip()
    if not base_url or not ops_key:
        _log.info(
            "no action lane: %s and %s must both be set for the chat app to "
            "reach the ops API",
            OPS_URL_VARIABLE,
            OPS_KEY_VARIABLE,
        )
        return None
    if ops_key.startswith("@Microsoft.KeyVault"):
        # The trap `docs/ops-api.md` records from the other tier, caught on this
        # one. An unresolved Key Vault reference is not an error: the setting
        # arrives holding the literal string, every `hmac.compare_digest` fails
        # against it, and every write is refused with `OPS_KEY_INVALID` from a
        # service that looks entirely healthy. Refusing to build the lane says
        # so once, at start-up, in the log a deployment check reads.
        _log.error(
            "%s holds an unresolved Key Vault reference; the action lane is "
            "not wired, because every write it made would be refused",
            OPS_KEY_VARIABLE,
        )
        return None
    resolved_catalog = build_catalog() if catalog is None else catalog
    if resolved_catalog is None:
        _log.error(
            "no action lane: %s is set but no published catalogue could be "
            "read, and a draft that is not priced is not a card",
            OPS_URL_VARIABLE,
        )
        return None
    client = OpsClient(
        base_url,
        ops_key,
        os.environ.get(OPS_FUNCTION_KEY_VARIABLE, "").strip(),
    )
    _log.info("action lane wired against %s", client.base_url)
    return OpsDesk(
        DraftStore(resolved_catalog),
        ConfirmationLedger(),
        client,
        GrantSigner(ops_key),
        visitors,
        rewards=rewards,
    )


def _reward_lookup(lanes: Lanes) -> RewardLookup | None:
    """How a redemption card learns a reward's published name and point cost.

    Off the account lane's own points read, which is the query
    ``get_points_balance`` already makes: it returns the published reward
    catalogue with each row marked affordable or not. Reading it here rather
    than opening a second connection is the argument
    :mod:`chip_chat.agent.lanes` makes about every backing service -- a lane
    that built its own client would be a second place a credential is resolved.

    Returns:
        The lookup, or ``None`` on a deployment with no account lane, which
        leaves a redemption card quoting no cost at all. That is honest and it
        is not free: ``sql/12_procedures.sql`` reads a null
        ``QUOTED_POINT_COST`` as *skip the check*, so such a deployment loses
        the ``REWARD_COST_CHANGED`` protection and gains nothing invented in its
        place.
    """
    account = lanes.account
    if account is None:
        return None

    def lookup(session_id: str, reward_id: str) -> tuple[str, int] | None:
        try:
            found = account.points_balance(session_id=session_id)
        except Exception:  # pragma: no cover - a lane declines rather than raises
            _log.warning("the account lane could not price a reward", exc_info=True)
            return None
        balance = found.balance
        if balance is None:
            # The lane declined. A card that quoted a cost read from nothing
            # would be the plausible number PRD A4 forbids, so it quotes none.
            return None
        for reward in balance.rewards:
            if reward.reward_id == reward_id:
                return reward.name, reward.point_cost
        return None

    return lookup


def _ordering_available(service: Service) -> bool | None:
    """What ``GET /healthz/lanes`` should report for the action lane.

    ``None`` where this deployment has no ops API, which
    :func:`chip_chat.agent.health.probe` renders as ``not_wired`` rather than
    down -- a deployment that never had a write path is working as configured,
    and reporting it red would train whoever reads the surface to ignore red.

    Asked through the desk rather than through the client so that the health
    surface and a confirmation card get their answer from the same object and
    the same fifteen-second memo. A health route that probed independently could
    say *up* in the same second a card said *unavailable*.
    """
    desk = service.gate.desk
    if not desk.offers_every_write():
        return None
    return desk.available()


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

            All four of the backing lanes are wired there now: #106 closed
            the two that were open, so *knowledge* holds one
            :class:`~chip_chat.search.retrieve.Retriever` against the live
            alias and *photo* holds the describer and the matcher over the
            published catalogue.

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
    if withdrawn := resolved_lanes.withdrawn():
        # Beside the wiring line rather than folded into it, because they answer
        # two different questions and the second one is the one nobody thinks to
        # ask. "personalization: true" is the lane; this is the name the model
        # will never be shown despite it. See `WITHHELD_TOOLS`.
        _log.info(
            "tools withheld from the model on this deployment: %s",
            [tool.value for tool in withdrawn],
        )
    # Built here rather than on first use, unlike the photo intake, and it is
    # worth being precise about the difference because `Service.photos` records
    # a genuinely expensive lesson: building Azure SDK clients on the start-up
    # path cost thirty-five seconds and a restart loop.
    #
    # This *does* touch Azure — `build_catalog` resolves the app's identity and
    # reads nine blobs — so it is on the same path for the same kind of work,
    # and eager anyway, because the alternative is worse. The tool list the
    # model is offered depends on which desk this is, and a conversation is
    # opened with that list written into its runtime context; a desk that
    # appeared on the second turn would make the first turn's registration a lie
    # and raise `ToolRegistrationError` on the third. There is no lazy version of
    # this that is also honest.
    #
    # What makes it affordable is that the read is nine blobs and the identity
    # is already resolved: measured on `ca-chip-chat-web--0000035`, against a
    # ten-item catalogue, the process went from import to "Uvicorn running" in
    # three seconds. #106 published the real one -- 192 items and 1,385
    # modifiers, 1.3 MB across the same nine blobs -- and `build_catalog`
    # memoises it, so the photo lane below reads the same object rather than a
    # second copy. If a future catalogue makes that untrue, the fix is to hand
    # `build_service` a catalogue rather than to defer the desk.
    #
    # What it deliberately does not do is talk to the *ops API*.
    # `OpsClient.available` is asked when a card is composed, and a start-up
    # path that waited on another service would be a liveness probe waiting
    # behind it.
    desk = build_action_lane(visitors, rewards=_reward_lookup(resolved_lanes))
    limits = SpendLimits.from_env()
    return Service(
        gate=SpendGate(
            SpendGuard(limits, kill_switch=default_kill_switch()),
            lambda: AzureChatModel(FoundryConfig.from_env()),
            desk=desk,
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
        #
        # Both of these go through the threadpool for the reason the chat route
        # does. `admit` can fall through to a roster read and `probe` is a read
        # per wired lane, so this handler is several seconds of blocking
        # Snowflake work on a deployment running one uvicorn worker -- and the
        # route it would block is `/healthz`, which is the one the platform uses
        # to decide whether the process is alive. An operator's diagnostic must
        # not be able to look like a dead container.
        await run_in_threadpool(resolved.visitors.admit, session_id)
        report = await run_in_threadpool(
            partial(
                probe,
                resolved.gate.lanes,
                session_id=session_id,
                ordering_available=_ordering_available(resolved),
            )
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
            # Through the threadpool: assignment is a pool checkout and, on an
            # expired roster TTL, a Snowflake read taken inside a lock.
            payload = _entry_reply(
                await run_in_threadpool(
                    partial(
                        resolved.visitors.admit,
                        session_id,
                        display_name=body.name,
                    )
                ),
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

        **Both shapes are sent incrementally, and for the object shape that is
        the whole of chip-901's fix.** Container Apps ingress closes a response
        that has sent nothing for sixty seconds; a fifth of this app's turns take
        longer than that. The streamed shape survived it from the day it was
        written because a ``waiting`` frame keeps the response busy, and the
        object shape did not, so every caller that was not the browser -- the
        write-gate red team, ``curl``, anything holding this route as an API --
        lost one turn in five to ``RemoteDisconnected``. It now goes out the same
        way: whitespace while the turn runs, then the object. See
        :func:`_held_open`.
        """
        session_id = _session_id(request)
        source_address = _source_address(request)
        # A visitor who never posted the name gate still gets an account. The
        # cold start is the product risk and an unbound conversation is the
        # empty-account failure wearing a different hat, so the assignment is
        # here as well as on the entry route rather than only on the polite path.
        # The rest of this handler is scrupulous about staying off the event
        # loop and this call was the one exception, which is the whole of
        # chip-sv6. `admit` looks like a dictionary lookup and mostly is; on an
        # expired `DEFAULT_ROSTER_TTL_SECONDS` it is a Snowflake read inside a
        # `threading.Lock`, bounded by the driver's 15s login and 30s network
        # timeouts. Thirty seconds is not enough to trip the liveness probe on
        # its own, which is why this was a quiet defect rather than a restart
        # loop -- but it is thirty seconds in which `/healthz` cannot answer.
        admitted = await run_in_threadpool(resolved.visitors.admit, session_id)
        conversation = resolved.sessions.get(
            session_id,
            tools=offered_tools(resolved.gate.lanes, resolved.gate.desk),
            lanes=resolved.gate.lanes,
        )
        message = _with_photo(resolved, session_id, body)
        # Off the event loop, and this is not a nicety. A turn is a model call
        # and several seconds of blocking work; run in the handler it would hold
        # the only loop this process has, and the first thing that stops being
        # answered is `/healthz` -- so Container Apps concludes the container is
        # dead and restarts it *in the middle of the visitor's turn*. That is not
        # a hypothetical: it is what the first deployment of this route did to
        # every conversation. Both branches below hand the work to a
        # `StreamingResponse` over a synchronous generator, which Starlette
        # iterates on a worker thread, so neither shape can block the loop.
        streaming = _wants_stream(request)
        shape = _stream if streaming else _object
        response: Response = StreamingResponse(
            shape(resolved, conversation, body, message, source_address, admitted),
            media_type=_STREAM_MEDIA_TYPE if streaming else _OBJECT_MEDIA_TYPE,
        )
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
    socket until it has admitted. Ahead of even that is the question of whether
    a photograph can be acted on at all on this deployment -- refusing there
    costs the socket and nothing else, which is the point.

    Args:
        service: The assembled service.
        request: The request, whose body is the image.
        session_id: The conversation, resolved from the cookie.

    Returns:
        The reference the next chat request carries, or the refusal.
    """
    # Before anything is read from the socket, and before the upload ceiling is
    # even consulted: can this deployment *do* anything with a photograph?
    #
    # Two different things have to be wired and only one of them was checked
    # here. `service.photos` is the intake -- Content Safety and the blob
    # container -- and `Lanes.photo` is the half that looks at the picture and
    # resolves a meal. The deployment that prompted `chip-cfi` had the first and
    # not the second, so a visitor's photograph was moderated, written to blob
    # storage with a retention obligation, and charged
    # `DEFAULT_UPLOAD_TOKEN_CHARGE` against their session -- and *then* the turn
    # came back saying photo matching was not available. They paid for the
    # storage of an image nothing would ever look at, and the budget they paid
    # it out of is the one that ends conversations early.
    #
    # An absent lane is a smaller surface, never a broken one: that is
    # `chip_chat.agent.lanes`' rule for the model's tool list, and this is the
    # same rule applied to the route. Refuse first, cheaply, and say so.
    intake = service.photos if service.photos is not None else build_photos()
    if intake is None:
        return PhotoReply(
            reply=(
                "Photographs are not wired up on this deployment, so I cannot "
                "look at one just now -- tell me what you want instead."
            )
        )
    if service.gate.lanes.photo is None:
        return PhotoReply(
            reply=(
                "I cannot look at photographs on this deployment just now -- "
                "tell me what you are after and I will help from the menu."
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
        session_id,
        tools=offered_tools(service.gate.lanes, service.gate.desk),
        lanes=service.gate.lanes,
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
    streamed = False
    for held in _held_open(
        service,
        conversation,
        body,
        message,
        source_address,
        admitted,
        idle=_frame({"type": "waiting"}),
        stream_text=True,
    ):
        if isinstance(held, bytes):
            yield held
        elif isinstance(held, _Delta):
            streamed = True
            yield _frame({"type": "text", "text": held.text})
        else:
            # `prose=not streamed` is the whole of the handover. Where the
            # provider streamed the answer the browser already has every word of
            # it, and re-sending the finished reply would print it twice.
            yield from _frames(held, prose=not streamed)


def _object(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    message: str,
    source_address: str,
    admitted: VisitorSession | None,
) -> Iterator[bytes]:
    """Yield one turn as one JSON object, preceded by however much whitespace.

    The shape a test, a ``curl`` and the adversarial write-gate harness get, and
    -- until chip-901 -- the shape that could not survive a slow turn. It is one
    :class:`ChatReply` serialised exactly as :class:`~fastapi.responses.JSONResponse`
    would have serialised it, with :data:`_OBJECT_HEARTBEAT` written every
    :data:`_HEARTBEAT_SECONDS` while the turn runs so that ingress never sees the
    response go idle.

    Two things are given up for that and both are worth saying out loud. The
    response is chunked rather than carrying a ``Content-Length``, because the
    length is not known when the headers go out; nothing in this tree reads that
    header, and a caller that did was already reading it off a response that
    could be truncated at sixty seconds. And a failure that occurs *after* the
    first heartbeat cannot become a 4xx or 5xx status, because the status line
    has already been sent -- which costs nothing here, since :func:`_run_turn`
    answers every failure with a 200 and a reply the visitor reads rather than
    with a status code.
    """
    for held in _held_open(
        service,
        conversation,
        body,
        message,
        source_address,
        admitted,
        idle=_OBJECT_HEARTBEAT,
    ):
        if isinstance(held, bytes):
            yield held
        elif isinstance(held, _Delta):
            # Unreachable while `stream_text` is left at its default, and here
            # anyway: this shape is one JSON object, so half an answer has
            # nowhere to go. Dropping it is correct rather than lossy -- the
            # same words arrive in full on the `ChatReply` below.
            continue
        else:
            # The arguments Starlette's own `JSONResponse` renders with, spelled
            # out rather than inherited, so that the bytes on the wire did not
            # change when the response stopped being a `JSONResponse`. A reply
            # with an accent in it is still one UTF-8 document, not an escape.
            yield json.dumps(
                held.model_dump(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Delta:
    """One fragment of the answer, on its way from the provider to the browser.

    A wrapper rather than a bare ``str`` because :func:`_held_open` already
    yields two other things -- heartbeat ``bytes`` and the finished
    :class:`ChatReply` -- and a third channel that was simply "a string" would
    be told apart from them by an ``isinstance`` that happened to work. This
    one says what it is.
    """

    text: str


def _held_open(
    service: Service,
    conversation: Conversation,
    body: ChatRequest,
    message: str,
    source_address: str,
    admitted: VisitorSession | None,
    *,
    idle: bytes,
    stream_text: bool = False,
) -> Iterator[bytes | ChatReply | _Delta]:
    """Run one turn on a worker thread, yielding ``idle`` until it finishes.

    The heartbeat, and it is not cosmetic. Container Apps ingress closes a
    response that has sent nothing for sixty seconds, and a turn against a
    rate-limited reasoning deployment routinely takes longer than that: the
    unheld shape of this route dies at exactly 60.19 s with the answer already
    written, which is the worst possible failure -- the visitor is charged for a
    turn they never see. Measured on the deployment on 28 August 2026, a fifth of
    turns were over the line: eight of thirty-four, p95 72.8 s, longest 95.2 s,
    and every one of the eight is a turn the container's access log never
    recorded a response for while ``/healthz`` went on answering beside it. See
    docs/deployment.md §3.12 and §3.13.

    Yields ``idle`` -- bytes -- for each heartbeat, and finally the
    :class:`ChatReply` itself, leaving the caller to decide how a reply is
    rendered in its own shape. That split is what lets the frames and the object
    share one timer rather than two implementations of it.

    **Fragments of the answer come out the same way the heartbeat does**, as
    :class:`_Delta` values interleaved with the ``idle`` bytes, and the caller
    decides whether they mean anything. That is what keeps one timer serving
    both shapes: the streamed shape turns a delta into a ``text`` frame, and the
    object shape -- which has no way to render half an answer -- drops it and
    waits for the whole :class:`ChatReply`, exactly as it did before there was
    anything to drop.

    Args:
        idle: What to write while the turn is still running. A ``waiting`` frame
            for the streamed shape, a space for the object shape.
        stream_text: Whether to ask the turn for fragments at all. ``False``
            leaves the model on its unstreamed path, which is what every caller
            that cannot use a fragment should want.
    """
    # Where the provider's fragments cross the thread boundary. The turn runs on
    # the worker and this generator is iterated on another; a queue is the whole
    # of the synchronisation, and it is unbounded because a full queue would
    # block the model call to wait for a browser, which has the dependency
    # exactly backwards.
    deltas: Queue[str] = Queue()
    # The provider writes the model's *raw* output, which on every food answer
    # ends with the D9 envelope as a line of JSON. Forwarding that verbatim
    # would put `{"claim_class":"food","citations":[...]}` in front of the
    # visitor -- bead `chip-2ky`, reopened by the back door. `ProseStream` holds
    # back anything that might be envelope, and `_frames` sends the parsed reply
    # afterwards as the authority on what was actually said.
    prose_filter = ProseStream()

    def _forward(fragment: str) -> None:
        shown = prose_filter.feed(fragment)
        if shown:
            deltas.put(shown)

    on_text = _forward if stream_text else None

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            _run_turn,
            service,
            conversation,
            body,
            message,
            source_address,
            admitted,
            on_text,
        )
        # **The wait is on the queue, not on the future**, and the difference is
        # the whole value of streaming. Waiting on the future and draining only
        # when it timed out meant fragments left the process in
        # `_HEARTBEAT_SECONDS` batches: measured against the deployment, the
        # first token reached the browser at 24.43 s on a turn that finished at
        # 24.47 s, which is to say the visitor waited exactly as long as before
        # and then got the whole answer at once. Polling the queue on a short
        # interval and keeping the heartbeat on its own clock is what makes the
        # first token early rather than merely differently packaged.
        last_written = monotonic()
        while True:
            # Checked before the wait rather than after it, so a turn that has
            # already finished leaves immediately. The object shape reaches this
            # with an always-empty queue, and waiting a poll interval it did not
            # need would put `_DELTA_POLL_SECONDS` on the end of every one of its
            # turns -- and a whole heartbeat, when the poll was the heartbeat.
            if pending.done() and deltas.empty():
                break
            try:
                yield _Delta(deltas.get(timeout=_DELTA_POLL_SECONDS))
            except Empty:
                pass
            else:
                # A fragment is traffic. Ingress does not need a heartbeat on
                # top of an answer that is actively arriving.
                last_written = monotonic()
                continue
            if monotonic() - last_written >= _HEARTBEAT_SECONDS:
                yield idle
                last_written = monotonic()
        # The turn is finished, but the queue can still hold the tail of the
        # answer -- the last fragments and the future completing race, and the
        # future usually wins. Draining before the payload is what stops the
        # last few words of every streamed answer from being dropped.
        while True:
            try:
                yield _Delta(deltas.get_nowait())
            except Empty:
                break
        yield pending.result()
        return


def _frames(payload: ChatReply, *, prose: bool = True) -> Iterator[bytes]:
    """Take one reply apart into the frames the widget renders.

    The sources frame comes after the prose and before the card, which is the
    order they are read in: D9's trailing source line belongs under the answer
    it supports, and a confirmation card belongs under everything. It is sent
    even when the list is empty, so the widget can clear a source line rather
    than having to remember whether one is showing.

    Args:
        payload: The finished reply.
        prose: Whether to write the answer text. ``False`` when the provider
            already streamed it word by word and the browser has all of it --
            every other frame here still has to be sent, because the sources,
            the card and the end frame are not things a model streams. A stop
            state and a failure both arrive with ``prose=True``, since neither
            streamed anything: they are the app speaking, not the model.
    """
    if prose:
        for chunk in _chunks(payload.reply):
            yield _frame({"type": "text", "text": chunk})
    else:
        # What the visitor has on screen is raw model output minus whatever the
        # filter held back. What they should have is `payload.reply`, which is
        # the parsed prose: envelope removed, fallback substituted where the
        # model wrote nothing usable. Those are the same string on a well-formed
        # turn and different on every interesting one, so the answer is settled
        # here rather than hoped for -- the widget replaces what it painted with
        # this, and a stream can never leave a visitor reading something the
        # turn did not conclude.
        yield _frame({"type": "text_final", "text": payload.reply})
    yield _frame(
        {
            "type": "sources",
            "citations": payload.citations,
            "claim_class": payload.claim_class,
        }
    )
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
    on_text: Callable[[str], None] | None = None,
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
        on_text: Called with each fragment of the answer as the provider writes
            it, for the streamed shape. ``None`` for the object shape, which
            has nowhere to put a fragment and wants the finished reply.
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
                on_text=on_text,
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
        _render(turn, result.reply, envelope=result.envelope)
        return ChatReply(
            reply=result.reply,
            card=dict(result.card) if result.card is not None else None,
            receipt=result.receipt,
            citations=[citation.as_dict() for citation in result.citations],
            claim_class=result.claim_class.value,
        )


def _render(turn: Any, message: str, *, envelope: ResponseEnvelope | None = None) -> None:
    """Close the turn the same way however it ended.

    ``render.response`` is emitted for a stop state and for a failure as well as
    for an answer, because "what did the visitor actually see" is the question
    the span exists to answer and those are all things a visitor saw.

    **What is recorded about the citations goes under ``metadata``, and that is
    deliberate.** RFC-001's span vocabulary has no citation attribute and this
    is not the change that should invent one: ``otel/schema.py`` is executable
    and the twenty-five span names and their attribute namespaces are what every
    dashboard and eval is built on, so a new key here would be a schema decision
    taken in a bug fix. :meth:`chip_chat.otel.spans._Recorder.set_metadata` is
    the sanctioned escape hatch for exactly this, and what it buys is the
    question a trace could not answer before: *which sources was this answer
    drawn from, and did the model name one that was never retrieved*. The second
    is :attr:`~chip_chat.agent.envelope.ResponseEnvelope.dropped_citation_ids`,
    which issue #75 counts and which is a violation rather than a nuisance.

    Args:
        turn: The ``chat.turn`` recorder.
        message: What the visitor was shown.
        envelope: D9's envelope, where a model wrote one. ``None`` for a stop
            state and for a failure, which are the app speaking and cite
            nothing.
    """
    with render_response() as recorder:
        recorder.record_output(message)
        if envelope is not None:
            recorder.set_metadata(
                claim_class=envelope.claim_class.value,
                citation_ids=[citation.id for citation in envelope.citations],
                dropped_citation_ids=list(envelope.dropped_citation_ids),
                uncited_claim=envelope.uncited_claim,
            )
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
