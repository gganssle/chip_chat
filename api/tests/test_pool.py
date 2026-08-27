"""The half of #44 a future contributor feels when they change the wrong line.

These tests fail when the *invariant* breaks rather than when output changes.
Deleting the readback before the bind, adding a ``get_connection`` accessor,
letting the release path file a connection it could not clear, or making a
handle outlive its block each fail a test here while leaving every happy path
green -- which is the only kind of test that protects a property nobody can see
in a passing demo.

``test_pool_concurrency.py`` is the other half, and it is the one the ticket is
about. This file is what makes that one's assumptions explicit.
"""

import inspect
import logging

import pytest

from chip_chat.api.pool import (
    IdentityBindingError,
    InMemoryVisitorSessions,
    PoolClosedError,
    ReleasedConnectionError,
    StaleConnectionError,
    UnboundConnection,
    UnboundSessionError,
    VisitorConnection,
    VisitorPool,
)
from chip_chat.api.testing import (
    FakeAccount,
    FakeSnowflakeSession,
    ForgetfulSession,
    OrderRow,
)

ALICE = "demo-0001"
BOB = "demo-0002"

_ORDERS = (
    OrderRow(demo_id=ALICE, order_id="ord-0000001"),
    OrderRow(demo_id=ALICE, order_id="ord-0000002"),
    OrderRow(demo_id=BOB, order_id="ord-0000003"),
)


@pytest.fixture
def account() -> FakeAccount:
    """Two visitors with orders, and #43's policies over them."""
    return FakeAccount(_ORDERS)


@pytest.fixture
def sessions() -> InMemoryVisitorSessions:
    """One session per visitor. Anything else resolves to nobody."""
    return InMemoryVisitorSessions({"alice-session": ALICE, "bob-session": BOB})


@pytest.fixture
def pool(account: FakeAccount, sessions: InMemoryVisitorSessions) -> VisitorPool:
    """A pool of one, so every checkout reuses the connection just given back.

    One slot is the harshest sequential configuration there is: every request
    after the first is handed the connection the previous request released,
    which is exactly the hand-off RFC-001 section 05 warns about.
    """
    return VisitorPool(account.connect, sessions=sessions, size=1)


# ---------------------------------------------------------------------------
# The identity is the server's, and a request cannot supply one
# ---------------------------------------------------------------------------


def test_a_checkout_binds_the_visitor_the_session_store_resolved(
    pool: VisitorPool,
) -> None:
    with pool.for_session("alice-session") as connection:
        assert connection.demo_id == ALICE
        assert connection.execute("SELECT GETVARIABLE('DEMO_ID')") == [(ALICE,)]


def test_no_public_method_of_the_pool_accepts_a_demo_id() -> None:
    """The absence RFC-001 section 05 calls the enforcement mechanism.

    Asserted as a property of the signatures rather than as a behaviour, because
    a behaviour can be reinstated by the next person who finds it convenient to
    pass an identity in. Here there is nowhere to pass it -- and the set of
    public methods is pinned too, so a ``get_connection`` added later fails this
    rather than quietly becoming the fifth way to get one.
    """
    public = {
        name
        for name in dir(VisitorPool)
        if not name.startswith("_") and callable(getattr(VisitorPool, name, None))
    }
    assert public == {"close", "for_session", "unbound"}
    for name in sorted(public):
        parameters = inspect.signature(getattr(VisitorPool, name)).parameters
        assert "demo_id" not in parameters, name
        assert "visitor" not in parameters, name


def test_a_session_with_nothing_bound_gets_no_connection_at_all(
    pool: VisitorPool, account: FakeAccount
) -> None:
    """Fail closed. The alternative is a connection scoped to whoever was last.

    Note what is asserted second: no connection was even opened. A refusal that
    still took a slot would mean the identity was checked after the resource was
    committed rather than before, and would give an attacker a way to starve the
    pool with sessions that bind to nobody.
    """
    with pytest.raises(UnboundSessionError), pool.for_session("nobody-session"):
        pytest.fail("a session with nothing bound must not yield a connection")
    assert account.opened == 0


def test_a_session_store_holding_something_that_is_not_a_demo_id_is_refused(
    account: FakeAccount,
) -> None:
    """A corrupted store is a refuse-to-serve event, not a value to pass along."""
    sessions = InMemoryVisitorSessions({"broken": "demo-0001'; DROP TABLE orders --"})
    pool = VisitorPool(account.connect, sessions=sessions, size=1)
    with pytest.raises(UnboundSessionError), pool.for_session("broken"):
        pytest.fail("a malformed demo_id must never reach Snowflake")


def test_a_session_released_from_the_store_stops_being_servable(
    pool: VisitorPool, sessions: InMemoryVisitorSessions
) -> None:
    sessions.release("alice-session")
    with pytest.raises(UnboundSessionError), pool.for_session("alice-session"):
        pytest.fail("an unbound session must not be served")


# ---------------------------------------------------------------------------
# The row access policies, as the pool makes them true
# ---------------------------------------------------------------------------


def test_a_visitor_reads_their_own_rows_and_only_their_own(pool: VisitorPool) -> None:
    with pool.for_session("alice-session") as connection:
        assert connection.execute("SELECT order_id FROM orders") == [
            ("ord-0000001",),
            ("ord-0000002",),
        ]
    with pool.for_session("bob-session") as connection:
        assert connection.execute("SELECT order_id FROM orders") == [("ord-0000003",)]


def test_the_connection_bob_gets_is_the_one_alice_just_gave_back(
    pool: VisitorPool, account: FakeAccount
) -> None:
    """The hand-off itself, made explicit.

    A pool of one guarantees the reuse rather than hoping for it, so this is the
    disclosure RFC-001 section 05 describes, asked for directly and answered with
    Bob's single row.
    """
    with pool.for_session("alice-session") as connection:
        assert connection.execute("SELECT order_id FROM orders")
    with pool.for_session("bob-session") as connection:
        assert connection.execute("SELECT order_id FROM orders") == [("ord-0000003",)]
    assert account.opened == 1


# ---------------------------------------------------------------------------
# The clear, including on the path nobody exercises by hand
# ---------------------------------------------------------------------------


def test_the_variable_is_gone_from_the_connection_once_the_block_ends(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    """Asserted on the session's own state rather than through the pool.

    Reading it back with another checkout would mean trusting the same path that
    was supposed to have cleared it.
    """
    opened: list[FakeSnowflakeSession] = []

    def connect() -> FakeSnowflakeSession:
        session = account.connect()
        opened.append(session)
        return session

    pool = VisitorPool(connect, sessions=sessions, size=1)
    with pool.for_session("alice-session"):
        assert opened[0].identity == ALICE
    assert opened[0].identity is None


def test_an_exception_mid_request_provably_leaves_a_clean_connection(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    """The third acceptance criterion, and the path a leaked identity hides on.

    The clear runs from a ``finally``, so a handler that raises clears exactly as
    one that returns does. The proof is not that the next visitor's read comes
    back correct -- a rebind would make that true anyway -- it is that the
    connection sitting in the pool *between* the two requests is carrying nobody.
    """
    opened: list[FakeSnowflakeSession] = []

    def connect() -> FakeSnowflakeSession:
        session = account.connect()
        opened.append(session)
        return session

    pool = VisitorPool(connect, sessions=sessions, size=1)

    def a_handler_that_falls_over() -> None:
        with pool.for_session("alice-session") as connection:
            connection.execute("SELECT order_id FROM orders")
            raise ZeroDivisionError("the handler fell over mid-request")

    with pytest.raises(ZeroDivisionError):
        a_handler_that_falls_over()

    assert opened[0].identity is None
    assert opened[0].closed is False
    with pool.for_session("bob-session") as connection:
        assert connection.execute("SELECT order_id FROM orders") == [("ord-0000003",)]
    assert account.opened == 1


def test_a_handle_stops_working_when_its_block_ends(pool: VisitorPool) -> None:
    """Without this the context manager would be a convention, not a lifetime.

    A stashed handle is the one route left to a query on a connection that now
    belongs to somebody else, and it is a plausible thing for a helper to do by
    accident.
    """
    with pool.for_session("alice-session") as connection:
        stashed = connection
    with pytest.raises(ReleasedConnectionError):
        stashed.execute("SELECT order_id FROM orders")


# ---------------------------------------------------------------------------
# The checkout is what makes it binding. That claim, tested.
# ---------------------------------------------------------------------------


def test_a_connection_whose_clear_silently_failed_is_destroyed_not_handed_out(
    sessions: InMemoryVisitorSessions, caplog: pytest.LogCaptureFixture
) -> None:
    """The centre of this module, and the reason the readback is on the checkout.

    :class:`ForgetfulSession` models the failure a return-path design cannot see:
    ``UNSET`` runs, reports success, and changes nothing. A pool that trusted its
    release path would file that connection and hand Bob a session still bound to
    Alice -- and #43's policies would then scope his queries to her rows,
    correctly and disastrously.

    Three assertions, in order: Bob sees his own row; the pool destroyed a
    connection rather than binding over it; and it said so at ERROR, naming the
    visitor it found. A counter nobody reads is not the same as being loud.
    """
    account = FakeAccount(_ORDERS)
    pool = VisitorPool(account.connect_forgetfully, sessions=sessions, size=1)

    with pool.for_session("alice-session") as connection:
        assert connection.execute("SELECT order_id FROM orders")

    with (
        caplog.at_level(logging.ERROR, logger="chip_chat.api.pool"),
        pool.for_session("bob-session") as connection,
    ):
        assert connection.execute("SELECT order_id FROM orders") == [("ord-0000003",)]

    assert pool.stats.stale_discarded == 1
    assert account.opened == 2, "the destroyed connection was replaced, not mourned"
    assert any(
        ALICE in record.getMessage() and record.levelno == logging.ERROR
        for record in caplog.records
    ), "a stale checkout must name the visitor it found, at ERROR"


def test_the_forgetful_session_would_leak_through_a_pool_that_only_cleared() -> None:
    """The control on the control: the fixture really is broken.

    If a :class:`ForgetfulSession` were clean by accident, the test above would
    also pass against a pool with no checkout verification at all, and would be
    measuring nothing. So: run the clear the way a release-path design would, and
    watch the variable still standing afterwards.
    """
    account = FakeAccount(_ORDERS)
    session = account.connect_forgetfully()
    session.execute("SET DEMO_ID = ?", (ALICE,))
    session.execute("UNSET DEMO_ID")
    assert isinstance(session, ForgetfulSession)
    assert session.identity == ALICE
    assert session.execute("SELECT GETVARIABLE('DEMO_ID')") == [(ALICE,)]


def test_a_pool_whose_connections_never_come_back_clean_refuses_to_serve(
    sessions: InMemoryVisitorSessions,
) -> None:
    """When replacing does not help, the answer is no answer.

    Every connection this factory produces is already carrying somebody, which
    is the pathological case: the pool cannot heal by opening another. It gives
    up loudly instead of spinning -- and, the part that matters, it gives up
    rather than binding over one and serving it.
    """
    account = FakeAccount(_ORDERS)

    def connect() -> FakeSnowflakeSession:
        session = account.connect()
        session.execute("SET DEMO_ID = ?", (BOB,))
        return session

    pool = VisitorPool(connect, sessions=sessions, size=1)
    with pytest.raises(StaleConnectionError), pool.for_session("alice-session"):
        pytest.fail("a connection carrying somebody else must not be served")


def test_a_variable_that_will_not_read_back_refuses_the_request(
    sessions: InMemoryVisitorSessions,
) -> None:
    """The ticket's fourth scope bullet: assert on checkout, before any query runs.

    A ``SET`` that succeeds without taking effect is not hypothetical -- a driver
    that reconnected underneath does exactly this. Under #43's default deny the
    result is an empty account rather than a breach, which is the safe direction
    and still a demo that does not work, so it is refused rather than served.
    """

    class DeafSession(FakeSnowflakeSession):
        """Accepts the ``SET`` and leaves the variable unset."""

        def _set_identity(self, demo_id: str) -> None:
            return

    account = FakeAccount(_ORDERS)
    pool = VisitorPool(lambda: DeafSession(account), sessions=sessions, size=1)

    with pytest.raises(IdentityBindingError), pool.for_session("alice-session"):
        pytest.fail("a connection that did not take the bind must not serve")
    assert pool.stats.bind_failures == 1


def test_a_failed_bind_gives_its_slot_back(sessions: InMemoryVisitorSessions) -> None:
    """A pool of one that leaked a slot per failure would deadlock on the second.

    Invisible until the pool is under load, which is when it matters and when
    nobody is reading a stack trace.
    """
    failures = {"remaining": 2}

    class SometimesDeafSession(FakeSnowflakeSession):
        def _set_identity(self, demo_id: str) -> None:
            if failures["remaining"] > 0:
                failures["remaining"] -= 1
                return
            super()._set_identity(demo_id)

    account = FakeAccount(_ORDERS)
    pool = VisitorPool(lambda: SometimesDeafSession(account), sessions=sessions, size=1)

    for _ in range(2):
        with pytest.raises(IdentityBindingError), pool.for_session("alice-session"):
            pytest.fail("this bind was supposed to fail")
    with pool.for_session("alice-session") as connection:
        assert connection.execute("SELECT order_id FROM orders")


# ---------------------------------------------------------------------------
# The entry roster, which is the one deliberately unbound path
# ---------------------------------------------------------------------------


def test_the_entry_lane_reads_the_roster_on_a_connection_bound_to_nobody(
    pool: VisitorPool,
) -> None:
    """#43's ``entry_roster``: the read that happens before there is a visitor."""
    with pool.unbound() as roster:
        assert roster.execute("SELECT GETVARIABLE('DEMO_ID')") == [(None,)]
        assert roster.execute("SELECT demo_id FROM persona_fixtures") == [
            (ALICE,),
            (BOB,),
        ]


def test_an_unbound_connection_reads_nothing_from_a_visitor_scoped_table(
    pool: VisitorPool,
) -> None:
    """Why :meth:`VisitorPool.unbound` is not a loophole.

    ``visitor_isolation`` is default deny, so misusing the entry path for an
    ordinary query returns zero rows. That is a bug which shows up as an empty
    account, never a breach which shows up as somebody else's lunch.
    """
    with pool.unbound() as roster:
        assert roster.execute("SELECT order_id FROM orders") == []


def test_the_entry_lane_never_inherits_the_previous_visitor(pool: VisitorPool) -> None:
    """The leak :class:`~chip_chat.api.testing.NaivePool` has and this one does not.

    A checkout with nothing to set is the path that inherits, because it is the
    one path that does not overwrite what was there. Here the *checkout*
    guarantees the connection is clean, so having nothing to set is safe.
    """
    with pool.for_session("alice-session") as connection:
        assert connection.execute("SELECT order_id FROM orders")
    with pool.unbound() as roster:
        assert roster.execute("SELECT GETVARIABLE('DEMO_ID')") == [(None,)]
        assert roster.execute("SELECT order_id FROM orders") == []


def test_an_unbound_handle_also_dies_with_its_block(pool: VisitorPool) -> None:
    with pool.unbound() as roster:
        stashed = roster
    with pytest.raises(ReleasedConnectionError):
        stashed.execute("SELECT demo_id FROM persona_fixtures")


def test_the_two_handles_are_different_types(pool: VisitorPool) -> None:
    """So a function that needs a bound visitor can say so in its signature."""
    with pool.for_session("alice-session") as bound:
        assert isinstance(bound, VisitorConnection)
        assert not isinstance(bound, UnboundConnection)
    with pool.unbound() as unbound:
        assert isinstance(unbound, UnboundConnection)
        assert not isinstance(unbound, VisitorConnection)


# ---------------------------------------------------------------------------
# Lifecycle and counters
# ---------------------------------------------------------------------------


def test_closing_the_pool_closes_its_idle_connections(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    opened: list[FakeSnowflakeSession] = []

    def connect() -> FakeSnowflakeSession:
        session = account.connect()
        opened.append(session)
        return session

    pool = VisitorPool(connect, sessions=sessions, size=1)
    with pool.for_session("alice-session"):
        pass
    pool.close()
    assert opened[0].closed is True
    with pytest.raises(PoolClosedError), pool.for_session("alice-session"):
        pytest.fail("a closed pool has no connections to give")


def test_a_pool_needs_at_least_one_connection(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        VisitorPool(account.connect, sessions=sessions, size=0)


def test_the_counters_say_what_happened(pool: VisitorPool) -> None:
    with pool.for_session("alice-session"):
        pass
    with pool.unbound():
        pass
    stats = pool.stats
    assert stats.checkouts == 1
    assert stats.unbound_checkouts == 1
    assert stats.opened == 1
    assert stats.stale_discarded == 0
    assert stats.dirty_discarded == 0
    assert stats.bind_failures == 0


def test_a_connection_that_raises_on_clear_is_destroyed_rather_than_filed(
    sessions: InMemoryVisitorSessions,
) -> None:
    """The loud version of a failed clear. The pool shrinks; nothing leaks."""

    class UnclearableSession(FakeSnowflakeSession):
        def _unset_identity(self) -> None:
            raise RuntimeError("the network went away between statements")

    account = FakeAccount(_ORDERS)
    opened: list[UnclearableSession] = []

    def connect() -> UnclearableSession:
        session = UnclearableSession(account)
        opened.append(session)
        return session

    pool = VisitorPool(connect, sessions=sessions, size=1)
    with pool.for_session("alice-session"):
        pass
    assert pool.stats.dirty_discarded == 1
    assert opened[0].closed is True

    with pool.for_session("bob-session") as connection:
        assert connection.execute("SELECT order_id FROM orders") == [("ord-0000003",)]
    assert len(opened) == 2, "the pool healed by opening a replacement"
