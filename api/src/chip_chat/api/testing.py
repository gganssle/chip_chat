"""Test doubles for the two things in this package that fail silently.

These ship with the package rather than living in ``api/tests`` because the
service built on top of them needs them too, and because in both cases the
acceptance criteria are stated in terms of a double.

THE SPEND CAP. Issue #30, in as many words:

    A test that drives the counter past the ceiling and asserts NO model call is
    attempted -- assert on a mock that would record the call, not on the
    response text.

:class:`RecordingModel` is that mock. Asserting on the response text proves only
that the copy is right; asserting that this object recorded nothing proves that
no tokens were bought, which is the property that keeps the invoice small.

THE CONNECTION POOL. Issue #44, and it needs the same idea more sharply. A pool
that leaks and a pool that holds produce *identical* output: every query
succeeds, every answer is well formed, and one of them is about somebody else.
So the only evidence a concurrency test can catch a bleed is a pool that
definitely bleeds and a test that watches it get caught. :class:`NaivePool` is
that pool -- the design RFC-001 section 05 warns about, built on purpose.
:class:`ForgetfulSession` is the narrower fixture: a connection whose clear
silently does not take effect, which is exactly what separates a pool that
*clears on return* from one that *checks on checkout*.

:class:`FakeAccount` implements #43's two row access policies in Python, because
that is what makes a bleed observable in CI at a thousand checkouts a second. It
is a model of those policies and **not a test of them** --
``snowflake/tests/test_row_access_policies.py`` and ``make snowflake-verify`` are
what test the policies, and no number produced against this fake is evidence
about the account.
:class:`RecordingWriteBackend` is the same idea for the second launch gate. The
acceptance criteria of issue #63 are about writes that must *not* happen and
retries that must not double-write, and both are properties of what reached the
database rather than of what came back. So the double implements the one
behaviour of ``sql/12_procedures.sql`` those criteria turn on -- a retry key is
claimed once and replayed thereafter -- and counts the writes separately from
the calls.
"""

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from chip_chat.api.ops import OpsUnavailableError
from chip_chat.api.pool import (
    DEFAULT_POOL_SIZE,
    SessionConnection,
    VisitorSessions,
)

__all__ = [
    "EPOCH",
    "ClosedSessionError",
    "FakeAccount",
    "FakeClock",
    "FakeSnowflakeSession",
    "ForgetfulSession",
    "ModelCall",
    "NaiveConnection",
    "NaivePool",
    "OrderRow",
    "ProcedureCall",
    "RecordingModel",
    "RecordingWriteBackend",
    "UnknownStatementError",
]

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
"""An arbitrary but fixed instant, so a day boundary is something a test can aim at."""


class FakeClock:
    """A clock the test drives, with the two hands the spend cap reads."""

    __slots__ = ("_lock", "_monotonic", "_now")

    def __init__(self, start: datetime = EPOCH) -> None:
        """Initialise the clock.

        Args:
            start: The wall-clock instant :meth:`now` starts from. Must be
                timezone-aware; the daily ceiling converts it into its reset
                zone and a naive value would silently mean local time.

        Raises:
            ValueError: If ``start`` is naive.
        """
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._lock = threading.Lock()
        self._monotonic = 0.0
        self._now = start

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move both hands forward.

        Args:
            seconds: How far forward to move.
        """
        with self._lock:
            self._monotonic += seconds
            self._now += timedelta(seconds=seconds)

    def set_now(self, moment: datetime) -> None:
        """Jump wall-clock time to ``moment``, leaving the monotonic hand alone.

        For the day-boundary tests, where what matters is the calendar and not
        how many seconds elapsed.

        Args:
            moment: The new wall-clock instant. Must be timezone-aware.

        Raises:
            ValueError: If ``moment`` is naive.
        """
        if moment.tzinfo is None:
            raise ValueError("set_now needs a timezone-aware moment")
        with self._lock:
            self._now = moment


class ModelCall:
    """One invocation a :class:`RecordingModel` was asked to make.

    Attributes:
        prompt: What the caller sent.
        session_id: The conversation it belonged to, where the caller says.
    """

    __slots__ = ("prompt", "session_id")

    def __init__(self, prompt: str, session_id: str | None = None) -> None:
        self.prompt = prompt
        self.session_id = session_id

    def __repr__(self) -> str:
        return f"ModelCall(prompt={self.prompt!r}, session_id={self.session_id!r})"


class RecordingModel:
    """A stand-in for the agent that records every call and buys nothing.

    The point of the object is the *absence* of entries in :attr:`calls`. A
    guard test asserts ``model.calls == []`` after driving the ceiling past its
    limit; if the guard ever regresses into something asynchronous, that
    assertion fails even though the visitor-facing copy would still look right.
    """

    __slots__ = ("_lock", "calls", "completion_tokens", "prompt_tokens", "reply")

    def __init__(
        self,
        reply: str = "sure thing",
        *,
        prompt_tokens: int = 1_000,
        completion_tokens: int = 200,
    ) -> None:
        """Initialise the double.

        Args:
            reply: What :meth:`complete` returns.
            prompt_tokens: Input tokens each call claims to have cost.
            completion_tokens: Output tokens each call claims to have cost.
        """
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls: list[ModelCall] = []
        self._lock = threading.Lock()

    def complete(self, prompt: str, session_id: str | None = None) -> str:
        """Record the call and return the canned reply.

        Args:
            prompt: What the caller would have sent to the model.
            session_id: The conversation, where the caller tracks one.

        Returns:
            :attr:`reply`.
        """
        with self._lock:
            self.calls.append(ModelCall(prompt, session_id))
        return self.reply

    @property
    def call_count(self) -> int:
        """How many times a model would have been invoked."""
        with self._lock:
            return len(self.calls)

    @property
    def tokens_billed(self) -> int:
        """What every recorded call would have cost, in tokens."""
        return self.call_count * (self.prompt_tokens + self.completion_tokens)

    @property
    def prompts(self) -> Sequence[str]:
        """The prompts of every recorded call, in order."""
        with self._lock:
            return [call.prompt for call in self.calls]


# ---------------------------------------------------------------------------
# The pool doubles. See the module docstring: these exist to be caught.
# ---------------------------------------------------------------------------


class UnknownStatementError(RuntimeError):
    """The fake account was asked something it does not model.

    Deliberately fatal rather than a shrug. A fake that answered anything
    plausibly would let a test drift into measuring the fake, and the vocabulary
    below is small precisely so that a test which needs a new statement has to
    add one here and think about what #43's policies would do with it.
    """


class ClosedSessionError(RuntimeError):
    """A statement was run on a session the pool destroyed.

    Raised rather than tolerated because "the pool kept using a connection it
    threw away" is a bug this fake is well placed to catch.
    """


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One row of ``ACCOUNTS.ORDERS``, reduced to the two columns that matter.

    Attributes:
        demo_id: The column both row access policies compare against.
        order_id: The value a leak is observed *as*. It is the canary: a reply
            carrying an order id belonging to another ``demo_id`` is the
            disclosure, stated in data rather than in prose.
    """

    demo_id: str
    order_id: str


class FakeAccount:
    """The shared state behind every :class:`FakeSnowflakeSession`.

    Two tables and #43's two policies, in Python:

    ``orders``
        ``visitor_isolation``. Default deny -- an unset ``DEMO_ID`` returns
        **zero** rows, never all of them, which is the difference between a bug
        and a breach and is the single most important line in this class.

    ``persona_fixtures``
        ``entry_roster``. Open while nothing is bound, narrowed to one row once
        something is. The one inversion in ``sql/10_policies.sql``.

    The maintenance escape (``CHIP_CHAT_ADMIN`` plus ``ALL_VISITORS``) is not
    modelled: nothing that serves a conversation can reach it, and a fake that
    offered it would be offering a door the thing under test does not have.
    """

    __slots__ = ("_fixtures", "_latency", "_lock", "_orders", "opened")

    def __init__(
        self,
        orders: Sequence[OrderRow] = (),
        *,
        fixtures: Sequence[Sequence[object]] = (),
        latency_seconds: float = 0.0,
    ) -> None:
        """Initialise the account.

        Args:
            orders: Every row of ``orders``, across every visitor.
            fixtures: Every row of ``persona_fixtures``, each a sequence of
                columns in the order
                :data:`~chip_chat.api.visitors.ROSTER_COLUMNS` names them. Only
                the entry roster reads these, and it reads them on a connection
                that has bound nobody -- which is the whole reason
                ``entry_roster`` is the one inverted policy in
                ``sql/10_policies.sql``.
            latency_seconds: Slept inside each statement. Not realism -- it is
                the knob that widens the window a bleed lives in, so a
                concurrency test interleaves for real rather than by luck.
        """
        self._orders = tuple(orders)
        self._fixtures = tuple(tuple(row) for row in fixtures)
        self._latency = latency_seconds
        self._lock = threading.Lock()
        self.opened = 0
        """How many sessions have been opened against this account."""

    @property
    def visitors(self) -> tuple[str, ...]:
        """Every ``demo_id`` with an order, in order."""
        return tuple(sorted({row.demo_id for row in self._orders}))

    def connect(self) -> "FakeSnowflakeSession":
        """Open a session. The factory a pool is built with."""
        with self._lock:
            self.opened += 1
        return FakeSnowflakeSession(self)

    def connect_forgetfully(self) -> "ForgetfulSession":
        """Open a session whose clear silently does nothing. See the class."""
        with self._lock:
            self.opened += 1
        return ForgetfulSession(self)

    def orders_visible_to(self, demo_id: str | None) -> list[OrderRow]:
        """``visitor_isolation``, applied.

        Args:
            demo_id: What ``GETVARIABLE('DEMO_ID')`` returns, or ``None``.

        Returns:
            The visitor's rows, or nothing at all when nobody is bound.
        """
        if demo_id is None:
            return []
        return [row for row in self._orders if row.demo_id == demo_id]

    def fixture_rows_visible_to(self, demo_id: str | None) -> list[Sequence[object]]:
        """``entry_roster``, applied to the whole row rather than to the key.

        Args:
            demo_id: What ``GETVARIABLE('DEMO_ID')`` returns, or ``None``.

        Returns:
            Every fixture while nothing is bound; otherwise just the bound
            visitor's. The first column is ``demo_id``, per
            :data:`~chip_chat.api.visitors.ROSTER_COLUMNS`.
        """
        if demo_id is None:
            return list(self._fixtures)
        return [row for row in self._fixtures if row and row[0] == demo_id]

    def roster_visible_to(self, demo_id: str | None) -> list[str]:
        """``entry_roster``, applied.

        Args:
            demo_id: What ``GETVARIABLE('DEMO_ID')`` returns, or ``None``.

        Returns:
            Every visitor while nothing is bound; otherwise just the bound one.
        """
        if demo_id is None:
            return list(self.visitors)
        return [visitor for visitor in self.visitors if visitor == demo_id]

    def pause(self) -> None:
        """Sleep whatever ``latency_seconds`` was, if anything."""
        if self._latency:
            time.sleep(self._latency)


class FakeSnowflakeSession:
    """One Snowflake session: a variable bag, two tables, and nothing else.

    Handed to a thread by a pool, and used by that one thread at a time -- which
    is why nothing in here is locked. If two threads ever share one, that is the
    bug under test and an unlocked dictionary is the least of it.

    Attributes:
        closed: Whether the pool destroyed this session.
        statements: Every statement run on it, for a test that wants to assert
            on the *order* of the four round trips rather than on their effect.
    """

    __slots__ = ("_account", "_variables", "closed", "statements")

    def __init__(self, account: FakeAccount) -> None:
        self._account = account
        self._variables: dict[str, str] = {}
        self.closed = False
        self.statements: list[str] = []

    @property
    def identity(self) -> str | None:
        """What ``DEMO_ID`` currently holds, read without going through SQL.

        The test's window into the state a visitor cannot see. Asserting on this
        after a release is how ``test_pool.py`` says "the connection went back
        clean" without trusting the same code path that was supposed to clean it.
        """
        return self._variables.get("DEMO_ID")

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run one of the five statements this fake knows.

        Args:
            sql: The statement.
            parameters: Bind values.

        Returns:
            Rows, each a sequence of columns.

        Raises:
            ClosedSessionError: If the session was destroyed.
            UnknownStatementError: If ``sql`` is not modelled.
        """
        if self.closed:
            raise ClosedSessionError(f"session is closed; refusing {sql!r}")
        self.statements.append(sql)
        self._account.pause()
        statement = " ".join(sql.split())

        if statement == "SET DEMO_ID = ?":
            self._set_identity(str(parameters[0]))
            return []
        if statement == "UNSET DEMO_ID":
            self._unset_identity()
            return []
        if statement == "SELECT GETVARIABLE('DEMO_ID')":
            return [(self.identity,)]
        if statement == "SELECT order_id FROM orders":
            return [
                (row.order_id,) for row in self._account.orders_visible_to(self.identity)
            ]
        if statement == "SELECT demo_id FROM persona_fixtures":
            return [
                (visitor,) for visitor in self._account.roster_visible_to(self.identity)
            ]
        if statement.startswith("SELECT ") and " FROM persona_fixtures" in statement:
            # The entry roster's own query. Modelled by prefix rather than
            # verbatim because `chip_chat.api.visitors` spells its column list
            # from one tuple, and a fake that had to be edited every time a
            # column was added would be a fake nobody kept current.
            return self._account.fixture_rows_visible_to(self.identity)
        raise UnknownStatementError(
            f"the fake account does not model {sql!r}; add it here and say what "
            "#43's policies do with it"
        )

    def close(self) -> None:
        """Destroy the session. Idempotent, as a driver's close should be."""
        self.closed = True

    def _set_identity(self, demo_id: str) -> None:
        """Bind the variable. Overridden by nothing; the *unset* is the seam."""
        self._variables["DEMO_ID"] = demo_id

    def _unset_identity(self) -> None:
        """Clear the variable."""
        self._variables.pop("DEMO_ID", None)


class ForgetfulSession(FakeSnowflakeSession):
    """A session whose ``UNSET`` succeeds and does nothing.

    This is the fixture the whole argument of :mod:`chip_chat.api.pool` turns on.
    A pool that trusts its release path cannot tell this session from a healthy
    one: the statement runs, no exception is raised, and the connection goes back
    into the pool still carrying a visitor. Only a checkout that *reads the
    variable back before binding* notices, which is why
    :class:`~chip_chat.api.pool.VisitorPool` does that and why
    ``test_pool.py`` points this at it.

    Failing loudly instead would prove nothing: an exception on release is the
    easy case, and :class:`~chip_chat.api.pool.VisitorPool` already destroys the
    connection when one is raised.
    """

    __slots__ = ()

    def _unset_identity(self) -> None:
        """Do nothing at all, and say it worked."""


class NaiveConnection:
    """What :class:`NaivePool` hands out. A connection and a hope.

    Attributes:
        demo_id: Whoever this request *believes* it is bound to. Whether the
            connection agrees is the question the concurrency test asks.
    """

    __slots__ = ("_session", "demo_id")

    def __init__(self, session: SessionConnection, demo_id: str) -> None:
        self._session = session
        self.demo_id = demo_id

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run ``sql``. Works forever, including after the block ends."""
        return self._session.execute(sql, parameters)


class NaivePool:
    """A pool that does exactly what #44's title says, and leaks anyway.

    Read the title of the ticket: *sets demo_id on checkout and clears it on
    return*. This class does both, faithfully, every time, in a ``finally``. It
    is still a cross-visitor disclosure waiting for two people to arrive at once,
    and that is the whole argument for :mod:`chip_chat.api.pool` being shaped the
    way it is.

    **The defect.** It keeps ``size`` connections and hands them out round-robin,
    with no record of which are in use. Nothing stops two requests holding the
    same connection at the same moment -- and when they do, the second one's
    ``SET`` lands between the first one's ``SET`` and the first one's
    ``SELECT``. Every row access policy in #43 then filters that ``SELECT``
    correctly, for the wrong visitor. There is no exception, no warning, and no
    malformed reply: one visitor is simply handed another's orders.

    "Just keep N connections and rotate" is not a straw man. It is the first
    thing anybody writes, it reviews cleanly, and *every sequential test of it
    passes* -- which is the sentence RFC-001 section 05 is making and the reason
    ``api/tests/test_pool_concurrency.py`` runs its assertions against this class
    before it runs them against the real one. A detector nobody has watched
    detect anything is not evidence.

    **What the sound pool does differently**, and it is not the clearing: a
    semaphore bounds handouts to the slots that exist, so a connection is held by
    one request at a time; the idle list is popped under a lock, so two checkouts
    cannot take the same entry; and the checkout reads ``DEMO_ID`` back before
    binding, so a connection carrying anybody is destroyed rather than reused.

    **Do not build a service on this.** It is here for the reason
    ``chip_chat.eval.adversarial.testing.BleedingTarget`` is.
    """

    __slots__ = ("_connect", "_connections", "_lock", "_next", "_size")

    def __init__(
        self,
        connect: Callable[[], SessionConnection],
        *,
        sessions: VisitorSessions | None = None,
        size: int = DEFAULT_POOL_SIZE,
    ) -> None:
        """Assemble the broken pool.

        Args:
            connect: Opens a connection.
            sessions: Accepted and ignored, and optional because the naive
                design has no use for one -- it takes the identity from its
                caller, which is the other half of what RFC-001 section 05
                rejects. The argument exists so the two pools read alike at
                their call sites.
            size: How many connections to rotate between.
        """
        del sessions
        self._connect = connect
        self._size = size
        self._lock = threading.Lock()
        self._connections: list[SessionConnection] = []
        self._next = 0

    @contextmanager
    def for_session(self, demo_id: str) -> Iterator[NaiveConnection]:
        """Set on checkout, clear on return, and hope nobody else is holding it.

        Takes the ``demo_id`` directly, because a pool that does not own the
        binding has no reason to resolve one -- the identity is whatever the
        caller says it is.

        Args:
            demo_id: Who this request claims to be.

        Yields:
            A :class:`NaiveConnection`, possibly one somebody else is also using.
        """
        session = self._rotate()
        try:
            session.execute("SET DEMO_ID = ?", (demo_id,))
            yield NaiveConnection(session, demo_id)
        finally:
            # Faithfully, on every path, including the one that raised. It has
            # never been the thing that made this safe.
            session.execute("UNSET DEMO_ID")

    def _rotate(self) -> SessionConnection:
        """Hand out the next connection, in use or not."""
        with self._lock:
            if len(self._connections) < self._size:
                session = self._connect()
                self._connections.append(session)
                return session
            session = self._connections[self._next % self._size]
            self._next += 1
            return session


class ProcedureCall:
    """One stored procedure call a :class:`RecordingWriteBackend` was asked for.

    Attributes:
        procedure: The fully qualified name, as the ops API assembled it.
        demo_id: The visitor bound on the session it was called through. On a
            real connection this is a session variable rather than an argument,
            and it is recorded here because "whose row did that write" is the
            question RFC-001 section 05 exists to answer.
        arguments: Positional, in declaration order, retry key first.
    """

    __slots__ = ("arguments", "demo_id", "procedure")

    def __init__(self, procedure: str, demo_id: str, arguments: Sequence[object]) -> None:
        self.procedure = procedure
        self.demo_id = demo_id
        self.arguments = tuple(arguments)

    @property
    def retry_key(self) -> str:
        """The idempotency key, which every procedure takes first."""
        return str(self.arguments[0])

    @property
    def name(self) -> str:
        """The unqualified procedure name."""
        return self.procedure.rsplit(".", 1)[-1]

    def __repr__(self) -> str:
        return f"ProcedureCall({self.name!r}, {self.demo_id!r}, {self.arguments!r})"


class RecordingWriteBackend:
    """A stand-in for the Functions app's Snowflake connection.

    Implements exactly as much of ``sql/12_procedures.sql`` as the ops API's
    acceptance criteria depend on:

    * a retry key is claimed once, and a second call carrying it returns the
      stored receipt with ``replayed`` true rather than writing again;
    * a key spent by a different action is ``RETRY_KEY_SPENT_ON_ANOTHER_ACTION``;
    * a rejection is a returned object with ``ok`` false, never an exception.

    The distinction that matters is :attr:`calls` versus :attr:`writes`. A test
    asserting "one write" asserts on the second: a call that replayed a receipt
    is a call the database answered and not a row anybody was charged for.
    """

    __slots__ = (
        "_available",
        "_commit_then_fail",
        "_lock",
        "_rejection",
        "_spent",
        "_unavailable_calls",
        "calls",
        "writes",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = True
        self._unavailable_calls = 0
        self._commit_then_fail = 0
        self._rejection: Mapping[str, Any] | None = None
        self._spent: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self.calls: list[ProcedureCall] = []
        self.writes: list[ProcedureCall] = []

    # --- what the ops API sees -------------------------------------------

    def session(self, demo_id: str) -> "RecordingWriteBackend._Session":
        """Acquire a session, or refuse the way an unreachable service does."""
        with self._lock:
            if not self._available:
                raise OpsUnavailableError("the recording backend is down")
        return RecordingWriteBackend._Session(self, demo_id)

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available."""
        with self._lock:
            return self._available

    # --- what a test drives ----------------------------------------------

    def take_down(self) -> None:
        """Make the write path unreachable, as taking the Functions app down does."""
        with self._lock:
            self._available = False

    def bring_up(self) -> None:
        """Reverse :meth:`take_down`."""
        with self._lock:
            self._available = True

    def fail_calls(self, times: int = 1) -> None:
        """Fail the next ``times`` calls before anything is written."""
        with self._lock:
            self._unavailable_calls = times

    def commit_then_fail(self, times: int = 1) -> None:
        """Write, then lose the connection -- the case the retry key exists for.

        The procedure commits and spends its key; the caller is told nothing.
        A retry carrying the same key must find the receipt rather than write a
        second order, which is the whole of acceptance criterion three.
        """
        with self._lock:
            self._commit_then_fail = times

    def reject_next(self, code: str, detail: str = "refused by the catalogue") -> None:
        """Make the next call return a typed rejection rather than a receipt."""
        with self._lock:
            self._rejection = {"ok": False, "rejection": code, "detail": detail}

    def receipts(self) -> tuple[Mapping[str, Any], ...]:
        """Every receipt written, in order. One per actual write."""
        with self._lock:
            return tuple(receipt for _, receipt in self._spent.values())

    # --- the procedure itself, in miniature -------------------------------

    def _call(
        self, demo_id: str, procedure_name: str, arguments: Sequence[object]
    ) -> Mapping[str, Any]:
        call = ProcedureCall(procedure_name, demo_id, arguments)
        with self._lock:
            self.calls.append(call)
            if self._unavailable_calls > 0:
                self._unavailable_calls -= 1
                raise OpsUnavailableError("the recording backend dropped the call")

            key = (demo_id, call.retry_key)
            spent = self._spent.get(key)
            if spent is not None:
                action, receipt = spent
                if action != call.name:
                    return {
                        "ok": False,
                        "rejection": "RETRY_KEY_SPENT_ON_ANOTHER_ACTION",
                        "detail": f"this retry key was already spent by {action}",
                    }
                return {**receipt, "replayed": True}

            if self._rejection is not None:
                rejection = dict(self._rejection)
                self._rejection = None
                return rejection

            self.writes.append(call)
            receipt = {
                "ok": True,
                "action": call.name.upper(),
                "subject_id": f"{call.name}-{len(self.writes)}",
                "arguments": list(call.arguments[1:]),
                "simulation": "Simulated. Nothing was cooked, charged or sent.",
            }
            self._spent[key] = (call.name, receipt)
            if self._commit_then_fail > 0:
                self._commit_then_fail -= 1
                raise OpsUnavailableError(
                    "the recording backend committed and then lost the connection"
                )
            return dict(receipt)

    class _Session:
        """One bound session. Takes no identifier: the visitor is already bound."""

        __slots__ = ("_backend", "_demo_id")

        def __init__(self, backend: "RecordingWriteBackend", demo_id: str) -> None:
            self._backend = backend
            self._demo_id = demo_id

        def call(
            self, procedure_name: str, arguments: Sequence[object]
        ) -> Mapping[str, Any]:
            """Call one procedure through this session."""
            return self._backend._call(self._demo_id, procedure_name, arguments)
