"""The pool is the checkout the read lanes take, and nothing here proves it twice.

:data:`chip_chat.snowflake.reads.SessionCheckout` is written to be exactly
:meth:`chip_chat.api.pool.VisitorPool.for_session` -- a session id in, a
connection with one visitor already bound out. Nothing wires the two together
yet (bead ``cc-e1sr``), which is precisely why this file exists: a seam with no
caller is a seam that drifts, and the day it is wired is the wrong day to
discover that ``for_session`` grew a keyword or that ``VisitorConnection`` lost
``execute``.

Two claims, and the first is the load-bearing one.

**The pool satisfies the protocol.** Not by declaring that it does -- ``api/``
must not import a lane and ``snowflake/`` must not import the pool -- but
structurally, checked by mypy through the annotated binding below and at runtime
by actually running a lane's query through a real pool.

**And the connection it yields carries no way to name a visitor.** RFC-001 §05:
identity is applied to the session by the pool, so a lane holding one of these
has nothing to assert and nothing to get wrong.
"""

from datetime import UTC, datetime
from pathlib import Path

from chip_chat.api.pool import VisitorConnection, VisitorPool
from chip_chat.api.testing import FakeAccount, OrderRow
from chip_chat.api.visitors import VisitorSession, VisitorSessionStore
from chip_chat.snowflake import lane, reads
from chip_chat.snowflake.lane import PersonalizationLane
from chip_chat.snowflake.reads import SessionCheckout

SESSION = "sess-seam"
VISITOR = "demo-0007"
BOUND_AT = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _pool() -> VisitorPool:
    """A real pool over the fake account, with one visitor bound to one session."""
    account = FakeAccount([OrderRow(demo_id=VISITOR, order_id="ord-1")])
    sessions = VisitorSessionStore()
    sessions.bind(
        VisitorSession(
            session_id=SESSION,
            demo_id=VISITOR,
            persona_id="regular",
            label="The Regular",
            display_name=None,
            thread_id=None,
            created_at=BOUND_AT,
            last_seen=BOUND_AT,
        )
    )
    return VisitorPool(account.connect, sessions=sessions)


def test_the_pool_checkout_is_the_lanes_checkout() -> None:
    """The annotation is the assertion: mypy fails if the shapes diverge.

    A runtime check as well, because a protocol that type-checks and then hands
    back an object without ``execute`` would pass the annotation and fail the
    first turn.
    """
    pool = _pool()
    checkout: SessionCheckout = pool.for_session

    with checkout(SESSION) as connection:
        assert isinstance(connection, VisitorConnection)
        assert connection.demo_id == VISITOR


def test_a_lane_over_the_real_pool_runs_its_query_on_a_bound_connection() -> None:
    """End to end through the seam, with the fake account underneath.

    ``FakeAccount`` models #43's policies rather than a database, so what is
    demonstrated is the *plumbing*: the lane never saw a ``demo_id``, and the
    statement reached a session that had one.
    """
    pool = _pool()
    personalization = PersonalizationLane(pool.for_session)

    result = personalization.usual_order(session_id=SESSION)

    # The fake account models two tables and not the marts, so the read declines
    # -- which is the point: it declined *after* checking out a bound connection
    # rather than by failing to get one.
    assert result.declined is not None
    assert "the pool did not produce" not in result.declined
    assert pool.stats.checkouts == 1


def test_the_connection_a_lane_is_handed_cannot_be_asked_about_another_visitor() -> None:
    """There is no method on it that takes an identifier. That is the mechanism."""
    surface = {name for name in dir(VisitorConnection) if not name.startswith("_")}

    assert surface == {"demo_id", "execute", "invalidate"}


def test_the_snowflake_lanes_never_import_the_pool() -> None:
    """The direction that keeps ``snowflake/`` free of the app's state.

    :data:`~chip_chat.snowflake.reads.SessionCheckout` is a ``Callable`` alias
    rather than the pool's own type precisely so that the package running the
    query does not depend on the package binding the identity. Asserted against
    the source, because an import added in six months would otherwise be caught
    only by whoever next tried to use one package without the other.
    """
    for module in (lane, reads):
        source = Path(module.__file__ or "").read_text()
        assert "import chip_chat.api" not in source
        assert "from chip_chat.api" not in source
