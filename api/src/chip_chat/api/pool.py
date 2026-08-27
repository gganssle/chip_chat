"""The pooled connection, and the one thing that makes a row access policy true.

RFC-001 §05 names this module as the risk of the whole design, and it is worth
quoting rather than paraphrasing:

    Session variables and pooled connections are a classic combination for
    cross-tenant bleed. A connection returned to the pool with ``demo_id`` still
    set, then handed to another visitor's request before it's reassigned,
    defeats every policy above.

Defeats, and does it *quietly*. Every layer above this one keeps working: #43's
policies filter correctly, #45's procedures read ``GETVARIABLE('DEMO_ID')``
correctly, no tool signature grows a visitor argument. The system enforces the
wrong identity, confidently, and answers with somebody else's lunch. There is no
error, no refusal and nothing in a log — which is why the guarantee here has to
be structural rather than careful.

THE GUARANTEE, IN ONE SENTENCE. Nothing in this package can run SQL against
Snowflake except through an object handed out by a checkout that first proved
the connection carried no previous visitor and then proved it carries this one.

WHY THE CHECKOUT IS THE BINDING STEP AND THE RETURN IS NOT. The obvious design
is "clear it on the way back", and the obvious design is one missed ``finally``
away from a breach — worse, it fails *open*, because a clear that silently did
not take effect leaves a connection that looks returned and is not clean. So the
release path here is an optimisation: it gets the variable off the connection at
the earliest moment, and where it cannot, the connection is destroyed rather
than filed. The load-bearing check is on the other side.
:meth:`VisitorPool.for_session` reads ``GETVARIABLE('DEMO_ID')`` back off every
connection *before* binding anything, and a connection that answers with
anything other than ``NULL`` is closed, counted, logged and replaced. It is
never handed out — whatever it is carrying, and however it came to carry it.

That is the difference between a return path that remembers and a checkout that
cannot forget, and it is the shape three merged pieces of this repo already use:
``chip_chat.vision.lane`` accepts only a ``NormalizedImage`` so the wrong
ordering is unstateable, :class:`chip_chat.api.turns.FundedTurn` cannot exist for
a turn the guard refused, and ``sql/12_procedures.sql`` takes no visitor argument
at all.

FOUR STRUCTURAL FACTS, AND BETWEEN THEM THERE IS NO UNBOUND QUERY:

**1. There is no** ``get_connection()``. The pool exposes two context managers
and no accessor. It keeps the driver objects private, and so do the handles it
yields, so the only route to a cursor is an object this module constructed.

**2. The identity is not an argument.** :meth:`VisitorPool.for_session` takes a
*session id* and resolves the ``demo_id`` from the server-side
:class:`VisitorSessions` store. No public signature here accepts a ``demo_id``,
so there is no field for an injected instruction to populate even if the model
could reach this far — which it cannot, because no tool signature carries a
session id either.

**3. A session with nothing bound gets no connection at all.** An unresolved
session raises :class:`UnboundSessionError` before a slot is taken. Failing
closed here is the point: a connection handed out without binding is not a
connection that errors, it is a connection whose queries are scoped to whatever
the last visitor left behind.

**4. A handle dies when its block ends.** :class:`VisitorConnection` is
invalidated on release, so stashing one and using it later raises rather than
running against a connection that now belongs to somebody else. Without this the
context manager would be a convention; with it, the lifetime is the object's.

THE ONE DELIBERATELY UNBOUND PATH. :meth:`VisitorPool.unbound` exists because
#43's ``entry_roster`` policy requires it: ``persona_fixtures`` is the roster the
entry flow chooses a visitor's synthetic customer *from*, and it is read on a
connection that has bound nobody yet. It is safe for a reason worth stating
rather than assuming — ``visitor_isolation`` is default-deny, so an unbound
connection reads **zero** rows from all seven visitor-scoped tables. Misusing
:meth:`unbound` for an ordinary query is a bug that returns nothing, not a breach
that returns somebody. It yields a different type, :class:`UnboundConnection`, so
a function that requires a bound visitor says so in its signature.

FOUR ROUND TRIPS PER CHECKOUT CYCLE, AND WHY THEY ARE PAID. Read back before
binding, ``SET``, read back after binding, ``UNSET`` on release. The first and
third are the two a reviewer will want to delete, and they are the two the
guarantee is made of: the first is what makes a stale connection unreachable,
and the third is this ticket's *"assert on checkout that the variable reads back
as the expected value before any query runs"*. The demo serves a handful of
concurrent visitors against an X-Small warehouse; the round trips are cheaper
than the disclosure by a margin that does not need estimating.

INSTRUMENTATION, BECAUSE A STALE CHECKOUT MUST BE LOUD. :attr:`VisitorPool.stats`
counts what happened, and :data:`POOL_LOGGER` gets an ``ERROR`` line naming the
value found the moment a dirty connection turns up. There is no span: the schema
in :mod:`chip_chat.otel.schema` is closed by design and a pool checkout is not a
node of RFC-001 §09's tree, so this reports through the two channels that do not
require widening a vocabulary other repositories consume.

.. code-block:: python

    pool = VisitorPool(connect, sessions=sessions, size=4)

    with pool.for_session(session_id) as connection:
        rows = connection.execute("SELECT order_id FROM orders")

    with pool.unbound() as roster:          # entry only; #43's entry_roster
        rows = roster.execute("SELECT demo_id, display_name FROM persona_fixtures")

``api/tests/test_pool.py`` holds the structural half — those tests fail when the
*invariant* breaks rather than when output changes — and
``api/tests/test_pool_concurrency.py`` is the one this ticket is actually about.
"""

import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Protocol

from chip_chat.snowflake.schema import SESSION_VARIABLE

__all__ = [
    "DEFAULT_POOL_SIZE",
    "POOL_LOGGER",
    "IdentityBindingError",
    "InMemoryVisitorSessions",
    "PoolClosedError",
    "PoolError",
    "PoolStats",
    "ReleasedConnectionError",
    "SessionConnection",
    "StaleConnectionError",
    "UnboundConnection",
    "UnboundSessionError",
    "VisitorConnection",
    "VisitorPool",
    "VisitorSessions",
]

POOL_LOGGER: Final = logging.getLogger("chip_chat.api.pool")
"""Where a stale or unbindable connection goes.

At ``ERROR``, on the container's stdout, naming the value that was found. This
ticket asks for it in as many words -- *a pool checkout with a stale variable
should be loud, not silent* -- and the reason is that the counters in
:class:`PoolStats` are only read by somebody who already suspects something. A
log line is read by somebody who does not.
"""

DEFAULT_POOL_SIZE: Final = 4
"""Live connections. Small on purpose.

The warehouses are X-Small and suspend after sixty seconds
(``sql/01_warehouses.sql``), and this is a demo whose concurrency is a handful of
people at a stand. A larger pool would not serve anybody faster and would keep a
warehouse awake, which is the one thing the whole account is arranged to avoid.
"""

_MAX_ACQUIRE_ATTEMPTS: Final = 3
"""How many dirty connections a single checkout will destroy before giving up.

Not a retry budget for flakiness -- one dirty connection is already a serious
finding. It is a bound on the pathological case where every connection the
factory produces is unusable, so a request fails loudly instead of spinning.
"""

_DEMO_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
"""What a ``demo_id`` may look like. ``demo-0007`` is the shape ``data-gen`` mints.

The bind is a parameter rather than a formatted string, so this is not the
defence against injection -- it is the check that the *session store* has not
been corrupted. A session resolving to something that is not a demo_id is a
refuse-to-serve event, because the alternative is to send it to Snowflake and
find out.
"""

_SET_IDENTITY: Final = f"SET {SESSION_VARIABLE} = ?"
_UNSET_IDENTITY: Final = f"UNSET {SESSION_VARIABLE}"
_READ_IDENTITY: Final = f"SELECT GETVARIABLE('{SESSION_VARIABLE}')"
"""The three statements this module runs, spelled from the one shared constant.

:data:`chip_chat.snowflake.schema.SESSION_VARIABLE` is the name
``sql/10_policies.sql`` compares against and every procedure in
``sql/12_procedures.sql`` reads. A pool that set ``DEMO`` while the policies read
``DEMO_ID`` would produce a demo in which every table is empty -- which looks
like a missing load rather than like a bug -- so the string is imported and
never retyped.
"""


class SessionConnection(Protocol):
    """The narrow slice of a database connection this pool needs.

    A protocol rather than the Snowflake driver, for the reason
    :mod:`chip_chat.snowflake.snow` gives for shelling out to the CLI: the driver
    is not in this lockfile, the connection is already described once in
    ``~/.snowflake/config.toml``, and a second place that knows how to
    authenticate is a second thing to rotate a key in.

    It is also what makes ``api/tests/test_pool_concurrency.py`` runnable in CI.
    That test needs dozens of visitors interleaving through a handful of slots,
    thousands of times over, which is not a thing to ask of a trial account --
    and a pool bleed is a property of *this* code rather than of the network
    under it.

    An adapter over ``snowflake.connector`` is a handful of lines:
    ``cursor.execute(sql, parameters)`` then ``cursor.fetchall()``.
    """

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run one statement and return its rows, each a sequence of columns."""
        ...

    def close(self) -> None:
        """Close the underlying session. Called for every discarded connection."""
        ...


class VisitorSessions(Protocol):
    """The server-side session store, as this pool needs to read it.

    RFC-001 §05's trusted path in one method: identity originates in the app's
    session store and is applied to the connection. The store itself belongs to
    [#66]; this is the seam, and it is read-only on purpose. Nothing in this
    module can create or change a binding.
    """

    def demo_id_for(self, session_id: str) -> str | None:
        """Return the visitor bound to ``session_id``, or ``None`` if none is."""
        ...


class InMemoryVisitorSessions:
    """A session store held in one process, behind one lock.

    Honest for the single-replica deployment this demo runs on, and the same
    bargain :class:`chip_chat.api.ledger.BudgetLedger` and
    :class:`chip_chat.api.drafts.DraftStore` make: state behind one interface, so
    a shared store has one obvious place to land when a second replica exists.
    [#66] owns persona assignment and may replace this outright -- it satisfies
    :class:`VisitorSessions`, and nothing here depends on the class.
    """

    __slots__ = ("_bindings", "_lock")

    def __init__(self, bindings: Mapping[str, str] | None = None) -> None:
        """Initialise the store.

        Args:
            bindings: Session id to ``demo_id``, for a store built ready.
        """
        self._lock = threading.Lock()
        self._bindings: dict[str, str] = dict(bindings or {})

    def bind(self, session_id: str, demo_id: str) -> None:
        """Bind ``session_id`` to ``demo_id``.

        Server-side, and never from a request body: a binding a visitor can
        choose is a visitor who can choose somebody else's.

        Args:
            session_id: The conversation.
            demo_id: The synthetic customer it browses as.
        """
        with self._lock:
            self._bindings[session_id] = demo_id

    def release(self, session_id: str) -> None:
        """Forget ``session_id``. A later checkout for it fails closed.

        Args:
            session_id: The conversation to unbind.
        """
        with self._lock:
            self._bindings.pop(session_id, None)

    def demo_id_for(self, session_id: str) -> str | None:
        """Return the visitor bound to ``session_id``, or ``None``."""
        with self._lock:
            return self._bindings.get(session_id)


class PoolError(RuntimeError):
    """Base for everything this module refuses on."""


class UnboundSessionError(PoolError):
    """A checkout was asked for a session the store has no visitor for.

    The failing-closed case, and the one that matters. Serving this request on a
    freshly unbound connection would read zero rows under #43's policies and
    look like an empty account; serving it on a recycled one would read somebody
    else's. Neither is an answer, so there is no connection.
    """


class IdentityBindingError(PoolError):
    """The session variable did not read back as the visitor that was bound.

    Raised after ``SET`` and before any query runs. The connection is closed
    rather than filed, because a session that will not hold a variable is not a
    session to hand the next visitor.
    """


class StaleConnectionError(PoolError):
    """Every connection this checkout tried was carrying somebody.

    One dirty connection is a finding the pool absorbs -- it is destroyed and
    replaced. This is what is raised when replacing does not help, which means
    the factory itself is handing back used sessions. Refusing is the only safe
    answer left.
    """


class ReleasedConnectionError(PoolError):
    """A handle was used after its ``with`` block ended.

    Always a programming error, and specifically the one that would reintroduce
    everything this module prevents: the connection behind the handle has been
    cleared and may already be bound to another visitor.
    """


class PoolClosedError(PoolError):
    """A checkout was attempted after :meth:`VisitorPool.close`."""


@dataclass(frozen=True, slots=True)
class PoolStats:
    """What the pool has done, for an ops surface and for the tests.

    Attributes:
        checkouts: Connections bound to a visitor and handed out.
        unbound_checkouts: Deliberately unbound checkouts, for the entry roster.
        opened: Connections the factory was asked for.
        stale_discarded: Connections found carrying a session variable **at
            checkout** and destroyed rather than bound over. The number that
            matters. Anything above zero means the release path failed and the
            checkout caught it -- not a warning about the future, but a breach
            that did not happen.
        dirty_discarded: Connections destroyed on release because the clear
            raised. The pool shrinks by one; nothing leaks.
        bind_failures: Checkouts abandoned because the variable would not read
            back as the visitor that had just been set.
    """

    checkouts: int = 0
    unbound_checkouts: int = 0
    opened: int = 0
    stale_discarded: int = 0
    dirty_discarded: int = 0
    bind_failures: int = 0


class _Handle:
    """A live connection, valid for exactly the block it was given to.

    Shared by :class:`VisitorConnection` and :class:`UnboundConnection`. The
    driver object lives in a private slot the subclasses do not widen, so
    ``execute`` is the entire surface and there is nothing to hand to something
    that would keep it.
    """

    __slots__ = ("_session",)

    def __init__(self, session: SessionConnection) -> None:
        self._session: SessionConnection | None = session

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run ``sql`` on this connection.

        Args:
            sql: One statement.
            parameters: Bind values for it.

        Returns:
            The rows, each a sequence of columns.

        Raises:
            ReleasedConnectionError: If the block that owned this handle has
                ended. The connection behind it has been cleared and may belong
                to another visitor by now.
        """
        session = self._session
        if session is None:
            raise ReleasedConnectionError(
                "this connection was released when its `with` block ended; the "
                "session behind it has been cleared and may be bound to another "
                "visitor. Take a new one rather than holding this."
            )
        return session.execute(sql, parameters)

    def invalidate(self) -> None:
        """Sever the handle from its connection. Called by the pool, once.

        Public because :class:`VisitorPool` is not a subclass and calling a
        private member across the class boundary reads worse than this does.
        Calling it early is harmless: it makes the handle unusable, which is the
        state it is heading for anyway.
        """
        self._session = None


class VisitorConnection(_Handle):
    """A connection with exactly one visitor bound to it, proven twice.

    Constructed only by :meth:`VisitorPool.for_session`, after the connection was
    shown to carry no previous visitor and after ``DEMO_ID`` was read back as
    :attr:`demo_id`. Holding one *is* the proof, which is why nothing else in
    this package builds one.

    Attributes:
        demo_id: The visitor #43's policies will scope every query to. Here for
            logging and for assertions; passing it into a query would be the
            mistake this design removes the need for.
    """

    __slots__ = ("demo_id",)

    def __init__(self, session: SessionConnection, demo_id: str) -> None:
        super().__init__(session)
        self.demo_id = demo_id


class UnboundConnection(_Handle):
    """A connection with nothing bound, for the entry roster and nothing else.

    #43's ``entry_roster`` policy opens ``persona_fixtures`` to a session that
    has bound nobody, because that is the read the entry flow makes *before*
    there is a visitor to bind. Every other visitor-scoped table is default-deny,
    so this object reads zero rows from all seven of them: a misuse is an empty
    result, never another visitor's.

    A separate type rather than ``VisitorConnection`` with no ``demo_id`` so that
    a function requiring a bound visitor can say so in its signature.
    """

    __slots__ = ()


class VisitorPool:
    """Connections that cannot be handed out carrying somebody else's identity.

    One per process, and thread-safe: concurrency is the condition this class
    exists for rather than a case it tolerates.
    """

    __slots__ = (
        "_closed",
        "_connect",
        "_counts",
        "_idle",
        "_lock",
        "_log",
        "_sessions",
        "_slots",
    )

    def __init__(
        self,
        connect: Callable[[], SessionConnection],
        *,
        sessions: VisitorSessions,
        size: int = DEFAULT_POOL_SIZE,
        log: logging.Logger | None = None,
    ) -> None:
        """Assemble the pool.

        Args:
            connect: Opens a new connection. Called lazily, and again whenever
                one has to be destroyed, so a pool that discards a stale
                connection heals rather than shrinking for the life of the
                process.
            sessions: The server-side session store. Keyword-only and required:
                a pool without one could only be told identities by its callers,
                which is the design RFC-001 §05 rejects.
            size: Live connections. Bounds concurrency -- a checkout waits for a
                slot rather than opening an extra connection, which is what
                makes a pool smaller than the request count mean something.
            log: Where stale checkouts are reported. Defaults to
                :data:`POOL_LOGGER`.

        Raises:
            ValueError: If ``size`` is not positive.
        """
        if size < 1:
            raise ValueError("a pool needs at least one connection")
        self._connect = connect
        self._sessions = sessions
        self._log = log if log is not None else POOL_LOGGER
        self._slots = threading.Semaphore(size)
        self._lock = threading.Lock()
        self._idle: list[SessionConnection] = []
        self._counts: dict[str, int] = {}
        self._closed = False

    @property
    def stats(self) -> PoolStats:
        """A snapshot of the counters. See :class:`PoolStats`."""
        with self._lock:
            counts = dict(self._counts)
        return PoolStats(
            checkouts=counts.get("checkouts", 0),
            unbound_checkouts=counts.get("unbound_checkouts", 0),
            opened=counts.get("opened", 0),
            stale_discarded=counts.get("stale_discarded", 0),
            dirty_discarded=counts.get("dirty_discarded", 0),
            bind_failures=counts.get("bind_failures", 0),
        )

    @contextmanager
    def for_session(self, session_id: str) -> Iterator[VisitorConnection]:
        """Check out a connection bound to whoever ``session_id`` belongs to.

        The only way to run a visitor's query. It takes a session id rather than
        a ``demo_id`` because the identity is the server's to assert and not the
        caller's to supply: RFC-001 §05's trusted path, written as a signature.

        Args:
            session_id: The conversation, resolved from the request's session
                cookie by the app and by nothing the visitor can write to.

        Yields:
            A :class:`VisitorConnection` scoped to that visitor. It stops working
            when the block ends.

        Raises:
            UnboundSessionError: If the store has no visitor for ``session_id``,
                or has one that is not a well-formed ``demo_id``.
            IdentityBindingError: If the variable did not read back as the
                visitor that was set.
            StaleConnectionError: If every connection offered was carrying
                somebody else.
            PoolClosedError: If the pool has been closed.
        """
        demo_id = self._resolve(session_id)
        session = self._acquire()
        try:
            self._bind(session, demo_id)
        except BaseException:
            # `_bind` has already closed the connection; the slot is ours to
            # give back, and there is nothing to file.
            self._slots.release()
            raise
        handle = VisitorConnection(session, demo_id)
        self._bump("checkouts")
        try:
            yield handle
        finally:
            # A `finally`, because the exception path is the one nobody
            # exercises by hand and a leaked exception is a leaked identity.
            handle.invalidate()
            self._release(session, clear=True)

    @contextmanager
    def unbound(self) -> Iterator[UnboundConnection]:
        """Check out a connection with nothing bound, for the entry roster.

        The read #43's ``entry_roster`` policy exists for: the roster the entry
        flow chooses a visitor's synthetic customer *from*, which happens before
        there is a visitor. Every other visitor-scoped table is default-deny, so
        this yields the safest state in the system rather than a loophole in it.

        Yields:
            An :class:`UnboundConnection`, which stops working when the block
            ends.

        Raises:
            StaleConnectionError: If every connection offered was carrying
                somebody.
            PoolClosedError: If the pool has been closed.
        """
        session = self._acquire()
        handle = UnboundConnection(session)
        self._bump("unbound_checkouts")
        try:
            yield handle
        finally:
            handle.invalidate()
            # Nothing was set, so there is nothing to unset, and the round trip
            # is skipped. `UNSET` on a variable that was never defined is a
            # no-op on the live account rather than an error -- checked against
            # the trial on 2026-08-27 -- so this is a saving rather than a
            # necessity, and skipping it is also one fewer behaviour to depend
            # on. If a caller did set one through `execute`, the next checkout
            # finds it and destroys the connection; that is what the checkout
            # check is for.
            self._release(session, clear=False)

    def close(self) -> None:
        """Close every idle connection and refuse further checkouts.

        Connections currently checked out are closed by their own release, so a
        shutdown mid-request still clears the variable that request set.
        """
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for session in idle:
            _quietly_close(session)

    # ----------------------------------------------------------------- private

    def _resolve(self, session_id: str) -> str:
        """Resolve ``session_id`` to a visitor, or refuse before taking a slot.

        Raises:
            UnboundSessionError: If nothing is bound, or what is bound does not
                look like a ``demo_id``.
        """
        demo_id = self._sessions.demo_id_for(session_id)
        if demo_id is not None and _DEMO_ID_PATTERN.match(demo_id):
            return demo_id
        self._log.error(
            "refusing to serve session %s: the session store resolved %r, which "
            "is not a visitor this pool will bind",
            session_id,
            demo_id,
        )
        raise UnboundSessionError(
            f"session {session_id!r} has no visitor bound; there is no connection "
            "for a request whose identity the server cannot state"
        )

    def _acquire(self) -> SessionConnection:
        """Take a slot and return a connection proven to carry no visitor.

        This is the method the guarantee lives in. Everything it returns has just
        answered ``NULL`` to ``GETVARIABLE('DEMO_ID')`` -- idle connections and
        freshly opened ones alike, because "the factory always gives me a clean
        session" is exactly the kind of assumption this module is here to stop
        relying on. A connection that answers anything else does not come back
        from here: it is logged, counted, closed and replaced.
        """
        self._slots.acquire()
        try:
            for _ in range(_MAX_ACQUIRE_ATTEMPTS):
                session = self._checked_out()
                stale = self._identity_on(session)
                if stale is None:
                    return session
                self._log.error(
                    "pool checkout found %s still set to %r; destroying the "
                    "connection rather than binding over it. Something returned "
                    "a visitor to the pool still attached to a connection.",
                    SESSION_VARIABLE,
                    stale,
                )
                self._bump("stale_discarded")
                _quietly_close(session)
            raise StaleConnectionError(
                f"{_MAX_ACQUIRE_ATTEMPTS} connections in a row were still "
                f"carrying {SESSION_VARIABLE}; refusing to serve this request"
            )
        except BaseException:
            self._slots.release()
            raise

    def _checked_out(self) -> SessionConnection:
        """Pop an idle connection, or open one.

        Raises:
            PoolClosedError: If the pool has been closed.
        """
        with self._lock:
            if self._closed:
                raise PoolClosedError("this pool is closed")
            if self._idle:
                return self._idle.pop()
        self._bump("opened")
        return self._connect()

    def _identity_on(self, session: SessionConnection) -> str | None:
        """Read ``DEMO_ID`` back off ``session``.

        Returns:
            The value, or ``None`` when the variable is unset.

        A connection that cannot answer the question is reported as carrying
        something, so the caller destroys it. Treating "I could not check" as
        "it is fine" is the one interpretation that fails open.
        """
        try:
            rows = session.execute(_READ_IDENTITY)
        except Exception:
            self._log.exception(
                "could not read %s back off a pooled connection", SESSION_VARIABLE
            )
            return "<unreadable>"
        if not rows or not rows[0]:
            return None
        value = rows[0][0]
        return None if value is None else str(value)

    def _bind(self, session: SessionConnection, demo_id: str) -> None:
        """Set the variable and prove it took, before any query runs.

        The ticket's fourth scope bullet, and defence in depth rather than
        superstition: a ``SET`` returning success is not evidence that the
        *session* now holds it. A driver that reconnected underneath, a
        connection multiplexed by something helpful, or a statement quietly
        routed elsewhere all succeed and leave the variable unset -- which #43's
        default deny turns into an empty account rather than a breach, but an
        empty account is still a demo that does not work.

        Raises:
            IdentityBindingError: If the readback disagrees. The connection is
                closed first, so it cannot be filed back into the pool.
        """
        try:
            session.execute(_SET_IDENTITY, (demo_id,))
        except Exception as error:
            self._bump("bind_failures")
            _quietly_close(session)
            raise IdentityBindingError(
                f"could not bind {SESSION_VARIABLE} for visitor {demo_id!r}"
            ) from error
        bound = self._identity_on(session)
        if bound != demo_id:
            self._bump("bind_failures")
            self._log.error(
                "refusing to serve visitor %r: %s read back as %r after being set",
                demo_id,
                SESSION_VARIABLE,
                bound,
            )
            _quietly_close(session)
            raise IdentityBindingError(
                f"{SESSION_VARIABLE} read back as {bound!r} rather than "
                f"{demo_id!r}; this connection will not serve"
            )

    def _release(self, session: SessionConnection, *, clear: bool) -> None:
        """Clear the variable and file the connection, or destroy it.

        A connection that will not clear is closed rather than filed. The pool
        shrinks by one and the factory opens a replacement on the next checkout;
        that is the cheap failure. Filing it and hoping is the expensive one --
        and :meth:`_acquire` would catch it anyway, which is the point of doing
        the check there rather than trusting this.

        Args:
            session: The connection to give back.
            clear: Whether anything was bound to it. See :meth:`unbound`.
        """
        try:
            if clear:
                try:
                    session.execute(_UNSET_IDENTITY)
                except Exception:
                    self._bump("dirty_discarded")
                    self._log.exception(
                        "could not clear %s on release; destroying the connection",
                        SESSION_VARIABLE,
                    )
                    _quietly_close(session)
                    return
            with self._lock:
                if self._closed:
                    _quietly_close(session)
                    return
                self._idle.append(session)
        finally:
            self._slots.release()

    def _bump(self, name: str) -> None:
        """Add one to a counter, under the lock."""
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1


def _quietly_close(session: SessionConnection) -> None:
    """Close ``session``, swallowing whatever it has to say about it.

    A connection being destroyed is already the unhappy path, and a driver that
    raises on close would turn "this one is not safe to reuse" into a failed
    request. What matters is that it does not go back in the pool, and that has
    already happened by the time this is called.
    """
    try:
        session.close()
    except Exception:
        POOL_LOGGER.debug("closing a discarded connection raised", exc_info=True)
