"""The session store, persona assignment, and the restart the criteria name.

Issue #66's acceptance criteria are the structure of this module. Three of the
five are properties of the code here and are tested here:

- *Two concurrent sessions get different personas and different, correct data.*
  :func:`test_concurrent_visitors_are_never_handed_the_same_customer` runs the
  admissions on a barrier, because a desk that collides only under contention
  passes every sequential test ever written.
- *No endpoint accepts a* ``demo_id`` *from a client or from a tool result.*
  ``api/tests/test_identity_binding.py`` is that one, and it is separate because
  it is about absence rather than behaviour.
- *Session store survives a container restart, or degrades in a way that is
  decided rather than discovered.* Both halves are here: the journal restores,
  and the unset-variable case logs before it forgets.

The fourth property is the cold start, and it is the one worth being blunt
about. *An empty account is how this demo dies* -- so an unpopulated fixture is
not deprioritised, it is unassignable, and
:func:`test_an_unpopulated_fixture_is_never_assigned` fails if that ever
softens into a preference.
"""

import json
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import pytest

from chip_chat.api.pool import UnboundSessionError, VisitorPool
from chip_chat.api.testing import EPOCH, FakeAccount, FakeClock, OrderRow
from chip_chat.api.visitors import (
    JOURNAL_VARIABLE,
    ROSTER_COLUMNS,
    FileJournal,
    NoJournal,
    PersonaFixture,
    SnowflakeRoster,
    StaticRoster,
    VisitorDesk,
    VisitorSession,
    VisitorSessionStore,
    clean_display_name,
    journal_from_env,
)
from chip_chat.snowflake.schema import columns_of

PERSONAS = ("regular", "explorer", "lapsed", "group", "saver")


def fixture(
    demo_id: str,
    persona_id: str = "regular",
    *,
    rank: int = 1,
    order_count: int = 42,
    home_store: int | None = 679,
    points_balance: int | None = 1_340,
) -> PersonaFixture:
    """A populated fixture, unless a test deliberately hollows one out."""
    return PersonaFixture(
        demo_id=demo_id,
        persona_id=persona_id,
        label=persona_id.title(),
        rank=rank,
        home_store=home_store,
        home_store_name="Ballard",
        points_balance=points_balance,
        usual_item_id="CMG-1",
        order_count=order_count,
        lifetime_spend=512.40,
        narrative="Same bowl, same store, nearly every week.",
    )


def roster_row(fixture_: PersonaFixture) -> tuple[object, ...]:
    """The same fixture as a row of :data:`ROSTER_COLUMNS`, for the fake account."""
    values = {
        "demo_id": fixture_.demo_id,
        "persona_id": fixture_.persona_id,
        "label": fixture_.label,
        "rank": fixture_.rank,
        "home_store": fixture_.home_store,
        "home_store_name": fixture_.home_store_name,
        "points_balance": fixture_.points_balance,
        "usual_item_id": fixture_.usual_item_id,
        "order_count": fixture_.order_count,
        "lifetime_spend": fixture_.lifetime_spend,
        "narrative": fixture_.narrative,
    }
    return tuple(values[column] for column in ROSTER_COLUMNS)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def roster() -> StaticRoster:
    """Five archetypes, two exemplars each. Enough to collide if the desk lets it."""
    return StaticRoster(
        fixture(f"dm-{index:06d}", persona, rank=rank)
        for index, (persona, rank) in enumerate(
            [(persona, rank) for persona in PERSONAS for rank in (1, 2)], start=1
        )
    )


@pytest.fixture
def desk(roster: StaticRoster, clock: FakeClock) -> VisitorDesk:
    return VisitorDesk(roster, clock=clock, entropy=Random(7))


# ---------------------------------------------------------------------------
# The roster: where a visitor's account comes from, and what disqualifies one.
# ---------------------------------------------------------------------------


def test_the_columns_read_are_the_columns_the_ddl_declares() -> None:
    """A renamed column fails here rather than emptying the roster in production."""
    declared = columns_of("persona_fixtures")

    assert set(ROSTER_COLUMNS) <= set(declared)


def test_the_roster_is_read_on_a_connection_that_has_bound_nobody() -> None:
    """#43's ``entry_roster`` is the one inverted policy, and this is its read."""
    account = FakeAccount(fixtures=[roster_row(fixture("dm-000001"))])
    store = VisitorSessionStore()
    pool = VisitorPool(account.connect, sessions=store, size=2)

    fixtures = SnowflakeRoster(pool).fixtures()

    assert [item.demo_id for item in fixtures] == ["dm-000001"]
    assert pool.stats.unbound_checkouts == 1
    assert pool.stats.checkouts == 0


def test_an_unpopulated_fixture_is_never_assigned() -> None:
    """*An empty account is how this demo dies* -- so it is not offered at all."""
    account = FakeAccount(
        fixtures=[
            roster_row(fixture("dm-000001", order_count=0)),
            roster_row(fixture("dm-000002", home_store=None)),
            roster_row(fixture("dm-000003", points_balance=None)),
            roster_row(fixture("dm-000004")),
        ]
    )
    pool = VisitorPool(account.connect, sessions=VisitorSessionStore(), size=2)

    fixtures = SnowflakeRoster(pool).fixtures()

    assert [item.demo_id for item in fixtures] == ["dm-000004"]


def test_a_roster_read_is_cached_and_a_failed_refresh_keeps_what_it_had(
    clock: FakeClock,
) -> None:
    """A Snowflake blip should cost a stale roster, never an empty one."""
    account = FakeAccount(fixtures=[roster_row(fixture("dm-000001"))])
    pool = VisitorPool(account.connect, sessions=VisitorSessionStore(), size=2)
    snowflake = SnowflakeRoster(pool, clock=clock, ttl_seconds=60.0)

    assert len(snowflake.fixtures()) == 1
    assert len(snowflake.fixtures()) == 1
    assert pool.stats.unbound_checkouts == 1, "the second read came from the cache"

    pool.close()
    clock.advance(120.0)

    assert [item.demo_id for item in snowflake.fixtures()] == ["dm-000001"]


def test_an_empty_roster_leaves_the_visitor_unbound_rather_than_inventing_one(
    clock: FakeClock,
) -> None:
    """A deployment with no population loaded serves the demo without an account."""
    empty = VisitorDesk(StaticRoster(), clock=clock)

    assert empty.admit("session-1") is None
    assert empty.store.demo_id_for("session-1") is None


# ---------------------------------------------------------------------------
# Assignment.
# ---------------------------------------------------------------------------


def test_a_visitor_is_assigned_a_fully_populated_account(desk: VisitorDesk) -> None:
    visitor = desk.admit("session-1", display_name="Sam")

    assert visitor is not None
    assert visitor.display_name == "Sam"
    assert visitor.fixture is not None
    assert visitor.fixture.populated


def test_the_same_cookie_resumes_the_same_customer(desk: VisitorDesk) -> None:
    """#9: visitor state persists between visits, and the cookie is how."""
    first = desk.admit("session-1", display_name="Sam")
    second = desk.admit("session-1")

    assert first is not None
    assert second is not None
    assert second.demo_id == first.demo_id
    assert second.display_name == "Sam"


def test_a_new_name_renames_rather_than_reassigning(desk: VisitorDesk) -> None:
    """Renaming is the one edit the entry screen offers; it is not a switch."""
    first = desk.admit("session-1", display_name="Sam")
    renamed = desk.admit("session-1", display_name="Alex")

    assert first is not None
    assert renamed is not None
    assert renamed.display_name == "Alex"
    assert renamed.demo_id == first.demo_id


def test_sequential_visitors_get_different_archetypes(desk: VisitorDesk) -> None:
    personas = {
        visitor.persona_id
        for visitor in (desk.admit(f"session-{index}") for index in range(5))
        if visitor is not None
    }

    assert personas == set(PERSONAS)


def test_concurrent_visitors_are_never_handed_the_same_customer(
    desk: VisitorDesk,
) -> None:
    """The criterion is about *concurrent* sessions, so the test has to be one.

    A desk that read the live set and then chose without a lock would collide
    here and nowhere else -- the sequential test above passes either way.
    """
    count = 10
    barrier = threading.Barrier(count)
    assigned: list[VisitorSession] = []
    guard = threading.Lock()

    def admit(index: int) -> None:
        barrier.wait()
        visitor = desk.admit(f"session-{index}")
        assert visitor is not None
        with guard:
            assigned.append(visitor)

    threads = [threading.Thread(target=admit, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({visitor.demo_id for visitor in assigned}) == count


def test_one_session_admitted_twice_at_once_gets_one_customer(
    desk: VisitorDesk,
) -> None:
    """Two requests racing on a fresh cookie must not spend two roster slots."""
    barrier = threading.Barrier(2)
    seen: list[VisitorSession] = []
    guard = threading.Lock()

    def admit() -> None:
        barrier.wait()
        visitor = desk.admit("session-1")
        assert visitor is not None
        with guard:
            seen.append(visitor)

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({visitor.demo_id for visitor in seen}) == 1
    assert len(desk.store) == 1


def test_an_exhausted_roster_reuses_the_least_recently_active_visitor(
    clock: FakeClock,
) -> None:
    """The roster bounds concurrent distinct accounts; it does not bound visitors."""
    desk = VisitorDesk(
        StaticRoster([fixture("dm-000001"), fixture("dm-000002", "explorer")]),
        clock=clock,
        entropy=Random(3),
    )
    first = desk.admit("session-1")
    clock.advance(600.0)
    desk.admit("session-2")
    clock.advance(600.0)

    third = desk.admit("session-3")

    assert first is not None
    assert third is not None
    assert third.demo_id == first.demo_id, "the idlest holder's account is reused"


# ---------------------------------------------------------------------------
# The identity binding: the store is what the pool resolves against.
# ---------------------------------------------------------------------------


def test_the_bound_visitor_reaches_snowflake_without_passing_through_a_caller(
    clock: FakeClock,
) -> None:
    """RFC-001 §05's trusted path, end to end, in six lines."""
    mine = "dm-000001"
    theirs = "dm-000002"
    account = FakeAccount(
        orders=[OrderRow(mine, "ord-1"), OrderRow(theirs, "ord-2")],
        fixtures=[roster_row(fixture(mine)), roster_row(fixture(theirs, "explorer"))],
    )
    store = VisitorSessionStore()
    pool = VisitorPool(account.connect, sessions=store, size=2)
    desk = VisitorDesk(SnowflakeRoster(pool, clock=clock), store=store, clock=clock)

    visitor = desk.admit("session-1")
    assert visitor is not None
    with pool.for_session("session-1") as connection:
        rows = connection.execute("SELECT order_id FROM orders")

    assert connection.demo_id == visitor.demo_id
    assert [row[0] for row in rows] == [
        row.order_id for row in account.orders_visible_to(visitor.demo_id)
    ]


def test_a_session_nobody_admitted_gets_no_connection() -> None:
    """Failing closed: an unresolved session is refused before a slot is taken."""
    account = FakeAccount(orders=[OrderRow("dm-000001", "ord-1")])
    pool = VisitorPool(account.connect, sessions=VisitorSessionStore(), size=2)

    with pytest.raises(UnboundSessionError), pool.for_session("session-nobody"):
        pass  # pragma: no cover - the checkout raises before the body runs


def test_releasing_a_session_unbinds_it(desk: VisitorDesk) -> None:
    desk.admit("session-1")
    desk.store.release("session-1")

    assert desk.store.demo_id_for("session-1") is None


# ---------------------------------------------------------------------------
# Restart: the criterion that offers a choice, and both halves of it.
# ---------------------------------------------------------------------------


def test_a_journalled_binding_survives_a_restart(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "sessions.jsonl"
    store = VisitorSessionStore(FileJournal(path, clock=clock))
    desk = VisitorDesk(StaticRoster([fixture("dm-000001")]), store=store, clock=clock)
    visitor = desk.admit("session-1", display_name="Sam")

    restarted = VisitorSessionStore(FileJournal(path, clock=clock))

    assert visitor is not None
    assert restarted.demo_id_for("session-1") == visitor.demo_id
    resumed = restarted.session("session-1")
    assert resumed is not None
    assert resumed.display_name == "Sam"
    assert resumed.fixture is None, "the account is Snowflake's, not the journal's"


def test_the_thread_pointer_outlives_the_visit(tmp_path: Path, clock: FakeClock) -> None:
    """``foundry-agent-shape.md``: history is Microsoft's, the pointer is ours."""
    path = tmp_path / "sessions.jsonl"
    store = VisitorSessionStore(FileJournal(path, clock=clock))
    desk = VisitorDesk(StaticRoster([fixture("dm-000001")]), store=store, clock=clock)
    desk.admit("session-1")
    store.set_thread_id("session-1", "thread_abc")

    restarted = VisitorSessionStore(FileJournal(path, clock=clock))
    resumed = restarted.session("session-1")

    assert resumed is not None
    assert resumed.thread_id == "thread_abc"


def test_a_session_older_than_the_ageing_window_is_not_restored(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "sessions.jsonl"
    journal = FileJournal(path, clock=clock, max_age_days=30)
    journal.record(
        VisitorSession(
            session_id="session-old",
            demo_id="dm-000001",
            persona_id="regular",
            label="Regular",
            display_name=None,
            thread_id=None,
            created_at=EPOCH - timedelta(days=90),
            last_seen=EPOCH - timedelta(days=60),
        )
    )

    assert VisitorSessionStore(journal).demo_id_for("session-old") is None


def test_a_truncated_line_costs_that_line_and_not_the_deployment(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A container killed mid-append should not become a container that will not start."""
    path = tmp_path / "sessions.jsonl"
    journal = FileJournal(path, clock=clock)
    journal.record(
        VisitorSession(
            session_id="session-1",
            demo_id="dm-000001",
            persona_id="regular",
            label="Regular",
            display_name=None,
            thread_id=None,
            created_at=EPOCH,
            last_seen=EPOCH,
        )
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"session_id": "session-2", "demo_i')

    assert VisitorSessionStore(journal).demo_id_for("session-1") == "dm-000001"


def test_the_journal_is_compacted_rather_than_grown(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "sessions.jsonl"
    journal = FileJournal(path, clock=clock)
    store = VisitorSessionStore(journal)
    desk = VisitorDesk(StaticRoster([fixture("dm-000001")]), store=store, clock=clock)
    desk.admit("session-1")
    for _ in range(5):
        store.set_thread_id("session-1", f"thread_{_}")

    VisitorSessionStore(FileJournal(path, clock=clock))

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["thread_id"] == "thread_4"


def test_no_journal_says_so_rather_than_being_found_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The decided degradation. It is a warning at assembly, not a surprise later."""
    monkeypatch.delenv(JOURNAL_VARIABLE, raising=False)

    with caplog.at_level(logging.WARNING, logger="chip_chat.api.visitors"):
        journal = journal_from_env()

    assert isinstance(journal, NoJournal)
    assert not VisitorSessionStore(journal).durable
    assert JOURNAL_VARIABLE in caplog.text


def test_an_unwritable_journal_path_degrades_instead_of_refusing_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file share that failed to mount should not also take the demo down."""
    wall = tmp_path / "wall"
    wall.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(JOURNAL_VARIABLE, str(wall / "sessions.jsonl"))

    with caplog.at_level(logging.ERROR, logger="chip_chat.api.visitors"):
        journal = journal_from_env()

    assert isinstance(journal, NoJournal)
    assert "falling back" in caplog.text


def test_a_journal_write_failure_never_fails_the_visitor_in_front_of_us(
    tmp_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "gone" / "sessions.jsonl"
    store = VisitorSessionStore(FileJournal(path, clock=clock))
    desk = VisitorDesk(StaticRoster([fixture("dm-000001")]), store=store, clock=clock)

    with caplog.at_level(logging.ERROR, logger="chip_chat.api.visitors"):
        visitor = desk.admit("session-1")

    assert visitor is not None
    assert store.demo_id_for("session-1") == "dm-000001"


def test_an_active_conversation_is_not_one_append_per_message(
    tmp_path: Path, clock: FakeClock
) -> None:
    """``last_seen`` is read by a restart and by #47's ageing.

    Neither can tell a minute, and a mounted file share can tell twenty appends.
    """
    path = tmp_path / "sessions.jsonl"
    store = VisitorSessionStore(FileJournal(path, clock=clock))
    desk = VisitorDesk(StaticRoster([fixture("dm-000001")]), store=store, clock=clock)
    desk.admit("session-1")

    for _ in range(20):
        clock.advance(1.0)
        store.touch("session-1", now=clock.now())

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1

    clock.advance(300.0)
    store.touch("session-1", now=clock.now())

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# The one piece of visitor text this tier keeps.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "kept"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("Sam", "Sam"),
        ("  Sam  ", "Sam"),
        ("Sam\x00\x1b", "Sam"),
        ("Sam\nJones", "Sam Jones"),
        ("S" * 200, "S" * 40),
    ],
)
def test_a_display_name_is_bounded_before_it_is_kept(
    supplied: str | None, kept: str | None
) -> None:
    assert clean_display_name(supplied) == kept


def test_a_fixture_row_of_the_wrong_width_is_a_wiring_bug() -> None:
    """The query and this class drifting apart is a bug, not a row to skip."""
    with pytest.raises(ValueError, match="columns"):
        PersonaFixture.from_row(("dm-000001", "regular"))


def test_a_null_timestamp_column_reads_as_missing_rather_than_zero() -> None:
    """Snowflake nulls arrive as ``None``; a fixture must not turn them into data."""
    row = list(roster_row(fixture("dm-000001")))
    row[ROSTER_COLUMNS.index("home_store_name")] = None
    row[ROSTER_COLUMNS.index("narrative")] = None

    parsed = PersonaFixture.from_row(row)

    assert parsed.home_store_name is None
    assert parsed.narrative is None
    assert parsed.populated


def test_a_session_record_survives_a_round_trip_without_its_account() -> None:
    """Everything the binding is, and nothing the database is better placed to say."""
    session = VisitorSession(
        session_id="session-1",
        demo_id="dm-000001",
        persona_id="regular",
        label="Regular",
        display_name="Sam",
        thread_id="thread_abc",
        created_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        last_seen=datetime(2026, 8, 27, 9, 30, tzinfo=UTC),
        fixture=fixture("dm-000001"),
    )

    restored = VisitorSession.from_record(session.as_record())

    assert restored == replace(session, fixture=None)
