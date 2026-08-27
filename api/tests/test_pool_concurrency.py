"""The test this ticket is actually about. RFC-001 section 05 asks for it by name.

    The pool must set the variable on checkout and clear it on return, and the
    adversarial suite must include a concurrency test that would actually catch a
    failure here -- **sequential tests will pass regardless.**

"Would actually catch" is a claim about the test, and there is one way to
establish it: point the test at something that definitely leaks and watch it
notice. :class:`~chip_chat.api.testing.NaivePool` is that something, and it is
worth being precise about why, because it is not a straw man.

It **does what the title of this ticket says.** It sets ``DEMO_ID`` on checkout.
It clears it on return, in a ``finally``, on every path including the one that
raised. What it does not do is stop two requests holding one connection at the
same moment -- it keeps four and rotates through them -- and that is enough. The
second request's ``SET`` lands between the first request's ``SET`` and its
``SELECT``; #43's row access policies then filter that ``SELECT`` perfectly, for
the wrong visitor; and somebody is handed another person's orders with no
exception and no malformed reply anywhere in the system.

Every sequential test of that pool passes, which is the first test below rather
than a claim made about it. If a sequential run could tell the two pools apart,
nothing else here would be measuring concurrency.

Four assertions, in order:

#. Run one request at a time and the naive pool holds. A detector that fires on
   everything has detected nothing.
#. Run them together and it discloses.
#. :class:`~chip_chat.api.pool.VisitorPool`, under identical conditions -- same
   visitors, same rounds, same fake account, a pool smaller than the request
   count -- discloses nothing.
#. The concurrent runs actually overlapped. A round whose requests did not
   interleave is a sequential round wearing threads, and the quotation above
   says what a sequential round is worth here.

The fourth is not ceremony. It is the difference between "no disclosures" and
"no disclosures observed under conditions that could have produced one".
"""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from chip_chat.api.pool import InMemoryVisitorSessions, UnboundSessionError, VisitorPool
from chip_chat.api.testing import FakeAccount, NaivePool, OrderRow

VISITORS = 32
"""More visitors than slots, which is the condition the ticket names."""

POOL_SIZE = 4
"""A pool smaller than the request count. Without this there is no hand-off."""

ROUNDS = 40
"""Requests per visitor. 1,280 checkouts through four connections -- the
*sustained run* the acceptance criterion asks for rather than one hot instant."""

LATENCY_SECONDS = 0.0001
"""Slept inside every statement, so a request holds its connection across a
scheduling point rather than completing between two of them. Not realism: it is
what turns "these threads could interleave" into "these threads did"."""

Checkout = Callable[[], AbstractContextManager[Any]]
"""A request's way in, already bound to one visitor.

The two pools are asked for a connection differently -- the sound one resolves
the identity from the session store, the naive one is simply told it. That
difference is the finding of #44 and ``test_pool.py`` asserts it directly; here
it is held constant behind a thunk so the only variable between the two runs is
the pooling itself.
"""


def _visitor(index: int) -> str:
    return f"demo-{index:04d}"


def _order(index: int, sequence: int) -> str:
    return f"ord-{index:04d}{sequence:03d}"


ORDERS = tuple(
    OrderRow(demo_id=_visitor(index), order_id=_order(index, sequence))
    for index in range(VISITORS)
    for sequence in range(2)
)
"""Two orders each, and every order id names its owner.

The order id is the canary. A reply carrying ``ord-0007xxx`` to visitor
``demo-0012`` is the disclosure, stated in data rather than in prose, and it is
observable without asking the pool anything about itself.
"""

EXPECTED = {
    _visitor(index): (_order(index, 0), _order(index, 1)) for index in range(VISITORS)
}


@dataclass(frozen=True, slots=True)
class Disclosure:
    """One request that was answered with somebody else's data.

    Attributes:
        observer: The visitor who asked.
        saw: The visitor whose data came back.
        evidence: What was actually returned.
    """

    observer: str
    saw: str
    evidence: str


class Findings:
    """Disclosures and starvations, collected across threads.

    Starvation is tracked separately and deliberately not counted as a
    disclosure: a request answered with *nothing* is #43's default deny doing its
    job on a connection somebody else cleared underneath it. That is a bug, and
    the safe direction to be wrong in. Conflating the two would let a pool that
    returns empty results everywhere look like a pool that is isolating.
    """

    __slots__ = ("_lock", "disclosures", "refusals", "starvations")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.disclosures: list[Disclosure] = []
        self.starvations = 0
        self.refusals = 0

    def disclosed(self, observer: str, saw: str, evidence: str) -> None:
        with self._lock:
            self.disclosures.append(Disclosure(observer, saw, evidence))

    def starved(self) -> None:
        with self._lock:
            self.starvations += 1

    def refused(self) -> None:
        with self._lock:
            self.refusals += 1

    @property
    def summary(self) -> str:
        """What to print when this fails, so the failure is readable."""
        if not self.disclosures:
            return "no disclosures"
        first = self.disclosures[0]
        return (
            f"{len(self.disclosures)} cross-visitor disclosures, e.g. "
            f"{first.observer} was answered with {first.saw}'s data ({first.evidence})"
        )


class Overlap:
    """How many requests were inside the pool at once, and whose.

    The fourth assertion of this file. Without it a green run could mean the
    isolation held or could mean the threads politely queued, and those are not
    the same result.
    """

    __slots__ = ("_holders", "_lock", "peak", "peak_visitors")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: set[str] = set()
        self.peak = 0
        self.peak_visitors: frozenset[str] = frozenset()

    @contextmanager
    def holding(self, demo_id: str) -> Iterator[None]:
        """Count ``demo_id`` as inside the pool for the duration of the block."""
        with self._lock:
            self._holders.add(demo_id)
            if len(self._holders) > self.peak:
                self.peak = len(self._holders)
                self.peak_visitors = frozenset(self._holders)
        try:
            yield
        finally:
            with self._lock:
                self._holders.discard(demo_id)


def _one_request(
    checkout: Checkout, demo_id: str, findings: Findings, overlap: Overlap
) -> None:
    """Ask the two questions a leak answers wrongly, and record what came back.

    ``GETVARIABLE`` first, because that is the identity the connection *believes*
    it has and a mismatch there is the leak caught at its source. The rows
    second, because that is the leak as a visitor experiences it -- a pool could
    in principle get the variable right and the rows wrong, so both are asked
    rather than one being trusted to imply the other.
    """
    with checkout() as connection, overlap.holding(demo_id):
        bound = connection.execute("SELECT GETVARIABLE('DEMO_ID')")[0][0]
        if bound is not None and bound != demo_id:
            findings.disclosed(demo_id, str(bound), f"DEMO_ID was {bound!r}")

        returned = connection.execute("SELECT order_id FROM orders")
        rows = tuple(str(row[0]) for row in returned)

    if rows == EXPECTED[demo_id]:
        return
    if not rows:
        findings.starved()
        return
    owners = sorted({row[:8].replace("ord-", "demo-") for row in rows})
    findings.disclosed(demo_id, ", ".join(owners), str(rows))


def _in_parallel(work: list[Callable[[], None]]) -> None:
    """Run every callable at once and re-raise the first thing that went wrong.

    ``list()`` around the map rather than fire-and-forget: a thread that fails
    silently in a concurrency test is a green run that measured nothing.
    """
    with ThreadPoolExecutor(max_workers=len(work)) as threads:
        list(threads.map(lambda job: job(), work))


def _rounds(barrier: threading.Barrier, body: Callable[[], None]) -> Callable[[], None]:
    """Run ``body`` once per round, re-synchronising on ``barrier`` each time.

    A barrier rather than "start the threads and hope": threads started together
    drift apart within a millisecond, and by the tail of a forty-round run they
    would be taking turns. Re-synchronising every round keeps the pool contended
    from the first request to the last.

    The ``abort`` is what stops a single failure from becoming a hang. Without
    it, one thread raising leaves the rest waiting on a barrier that will never
    fill, and the test that was supposed to report a disclosure reports nothing
    at all until CI kills it.
    """

    def loop() -> None:
        try:
            for _ in range(ROUNDS):
                barrier.wait()
                body()
        except BaseException:
            barrier.abort()
            raise

    return loop


@pytest.fixture
def account() -> FakeAccount:
    """The fake account, with a sleep inside every statement.

    See :data:`LATENCY_SECONDS`. And see the warning in
    :mod:`chip_chat.api.testing`: this models #43's policies so a bleed is
    observable in CI. It is not a test of them.
    """
    return FakeAccount(ORDERS, latency_seconds=LATENCY_SECONDS)


@pytest.fixture
def sessions() -> InMemoryVisitorSessions:
    """One server-side session per visitor, the only place an identity lives."""
    return InMemoryVisitorSessions(
        {f"session-{index}": _visitor(index) for index in range(VISITORS)}
    )


# ---------------------------------------------------------------------------
# 1. The premise: a sequential run cannot tell the two pools apart
# ---------------------------------------------------------------------------


def test_the_naive_pool_holds_when_its_requests_are_run_one_at_a_time(
    account: FakeAccount,
) -> None:
    """If this ever fails, every assertion below becomes worthless.

    The naive pool sets the variable on checkout and clears it on return, so a
    run in which no two requests are ever in flight together is answered
    correctly every time. This is RFC-001's *sequential tests will pass
    regardless*, run as a test rather than quoted as a warning.
    """
    pool = NaivePool(account.connect, size=POOL_SIZE)
    findings = Findings()
    overlap = Overlap()

    for _ in range(ROUNDS):
        for index in range(VISITORS):
            demo_id = _visitor(index)
            _one_request(
                lambda d=demo_id: pool.for_session(d),  # type: ignore[misc]
                demo_id,
                findings,
                overlap,
            )

    assert findings.disclosures == [], findings.summary
    assert findings.starvations == 0
    assert overlap.peak == 1, "a sequential run must never have two requests in flight"


# ---------------------------------------------------------------------------
# 2. Run together, the same pool discloses
# ---------------------------------------------------------------------------


def test_the_naive_pool_discloses_the_moment_its_requests_overlap(
    account: FakeAccount,
) -> None:
    """The control on the control, and the reason the next test means anything.

    The same pool, the same visitors and the same number of requests as the
    sequential run above. The only thing that changed is that they happen at
    once, and now visitors are answered with each other's orders.
    """
    pool = NaivePool(account.connect, size=POOL_SIZE)
    findings = Findings()
    overlap = Overlap()
    barrier = threading.Barrier(VISITORS)

    def visitor(index: int) -> Callable[[], None]:
        demo_id = _visitor(index)
        return _rounds(
            barrier,
            lambda: _one_request(
                lambda: pool.for_session(demo_id), demo_id, findings, overlap
            ),
        )

    _in_parallel([visitor(index) for index in range(VISITORS)])

    assert findings.disclosures, (
        "the deliberately broken pool did not leak, so this file is not testing "
        "anything. Either the requests stopped interleaving -- peak overlap was "
        f"{overlap.peak} -- or the fixture was quietly fixed."
    )


# ---------------------------------------------------------------------------
# 3. The pool this ticket ships, under exactly those conditions
# ---------------------------------------------------------------------------


def test_no_visitor_is_ever_answered_with_another_visitors_data(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    """The acceptance criterion. Thirty-two visitors, four connections, forty rounds.

    Everything is held identical to the run that just disclosed except the pool
    itself, so a green result here is attributable to the pool rather than to
    anything about the harness.

    Six assertions, and the later ones are what make the first mean something:
    nobody saw anybody else's rows; nobody was starved, so the isolation is not
    being achieved by returning nothing; the requests genuinely overlapped, and
    overlapped between *different* visitors; the pool really was smaller than the
    request count; and it never had to destroy a connection, which is how a clean
    run is told apart from one where the release path was failing and the
    checkout was quietly covering for it.
    """
    pool = VisitorPool(account.connect, sessions=sessions, size=POOL_SIZE)
    findings = Findings()
    overlap = Overlap()
    barrier = threading.Barrier(VISITORS)

    def visitor(index: int) -> Callable[[], None]:
        session_id = f"session-{index}"
        demo_id = _visitor(index)
        return _rounds(
            barrier,
            lambda: _one_request(
                lambda: pool.for_session(session_id), demo_id, findings, overlap
            ),
        )

    _in_parallel([visitor(index) for index in range(VISITORS)])

    assert findings.disclosures == [], findings.summary
    assert findings.starvations == 0, (
        "a request was answered with no rows at all, which means a connection "
        "was cleared or rebound while somebody was still holding it"
    )
    assert overlap.peak > 1, (
        "no two requests were ever inside the pool at the same time, so this run "
        "was a sequential test wearing threads and proves nothing"
    )
    assert len(overlap.peak_visitors) > 1, (
        "the overlapping requests were all the same visitor, so no hand-off "
        "between visitors was ever exercised"
    )

    stats = pool.stats
    assert stats.checkouts == VISITORS * ROUNDS
    assert stats.opened <= POOL_SIZE, "a pool of four that opened a fifth is not one"
    assert account.opened <= POOL_SIZE
    assert stats.stale_discarded == 0
    assert stats.dirty_discarded == 0
    assert stats.bind_failures == 0


# ---------------------------------------------------------------------------
# 4. The paths with nothing to bind, which is where a stale one is inherited
# ---------------------------------------------------------------------------


def test_a_request_with_no_visitor_is_refused_rather_than_given_a_used_connection(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    """A request that assumes the pool is clean, run against a pool that is not.

    An expired session, a persona switch mid-flight, an early return -- any
    request reaching the pool with nothing to bind is the one that would inherit,
    because it is the one path that does not overwrite what was there. Here four
    of them hammer a pool whose every connection is one somebody was just using,
    and all of them are refused rather than served.
    """
    pool = VisitorPool(account.connect, sessions=sessions, size=POOL_SIZE)
    findings = Findings()
    overlap = Overlap()
    barrier = threading.Barrier(VISITORS + 4)
    strangers = 4
    refusals = Findings()

    def visitor(index: int) -> Callable[[], None]:
        session_id = f"session-{index}"
        demo_id = _visitor(index)
        return _rounds(
            barrier,
            lambda: _one_request(
                lambda: pool.for_session(session_id), demo_id, findings, overlap
            ),
        )

    def stranger(index: int) -> Callable[[], None]:
        def attempt() -> None:
            with (
                pytest.raises(UnboundSessionError),
                pool.for_session(f"expired-{index}"),
            ):
                pytest.fail("a request with no visitor must not get a connection")
            refusals.refused()

        return _rounds(barrier, attempt)

    _in_parallel(
        [visitor(index) for index in range(VISITORS)]
        + [stranger(index) for index in range(strangers)]
    )

    assert findings.disclosures == [], findings.summary
    assert refusals.refusals == strangers * ROUNDS


def test_the_entry_lane_reads_the_roster_without_inheriting_anybody(
    account: FakeAccount, sessions: InMemoryVisitorSessions
) -> None:
    """#43's ``entry_roster``, exercised while the pool is hot.

    The entry read is the one legitimate unbound checkout in the system, and it
    is therefore the place a stale variable would be least visible: nothing about
    reading a roster looks wrong when the answer comes back as one row instead of
    thirty-two. So it is asserted directly -- the connection is carrying nobody,
    the roster is whole, and the visitor-scoped table it can reach returns
    nothing.
    """
    pool = VisitorPool(account.connect, sessions=sessions, size=POOL_SIZE)
    findings = Findings()
    overlap = Overlap()
    barrier = threading.Barrier(VISITORS + 4)
    inherited: list[str] = []
    lock = threading.Lock()

    def visitor(index: int) -> Callable[[], None]:
        session_id = f"session-{index}"
        demo_id = _visitor(index)
        return _rounds(
            barrier,
            lambda: _one_request(
                lambda: pool.for_session(session_id), demo_id, findings, overlap
            ),
        )

    def entry() -> Callable[[], None]:
        def read_roster() -> None:
            with pool.unbound() as roster:
                bound = roster.execute("SELECT GETVARIABLE('DEMO_ID')")[0][0]
                names = roster.execute("SELECT demo_id FROM persona_fixtures")
                scoped = roster.execute("SELECT order_id FROM orders")
            if bound is not None or len(names) != VISITORS or scoped:
                with lock:
                    inherited.append(f"{bound!r}, {len(names)} roster rows")

        return _rounds(barrier, read_roster)

    _in_parallel(
        [visitor(index) for index in range(VISITORS)] + [entry() for _ in range(4)]
    )

    assert inherited == [], "an entry read inherited a visitor from the pool"
    assert findings.disclosures == [], findings.summary
