"""How hot the round got, and whether it could have caught anything.

#30 built the concurrent attack and proved it overlaps. #82 asks a harder
question of the same machinery -- *"the concurrency test runs long enough and
hot enough to genuinely interleave, and passes"* -- and this module is what
turns those two adjectives into numbers a report can be held to.

The distinction it exists for is one line long. **Two turns overlapping is not
the same as two turns contending**, and only the second one can produce the
failure RFC-001 section 05 names. A bleed happens when a connection carrying one
visitor's ``demo_id`` is handed to another visitor's request, and a hand-off
requires somebody to have been waiting for it. Run three visitors against a pool
of four and every turn gets its own connection: the turns overlap perfectly,
nothing is ever handed from one visitor to another, and the round comes back
clean having been incapable of coming back any other way.

That is the same failure this package already refuses in three other forms -- an
attack needing a capability the target lacks, a canary nobody could read, a
"concurrent" round that ran one turn at a time -- and it gets the same treatment.
:class:`Pressure` is the precondition, :class:`Heat` is the measurement, and
:mod:`chip_chat.eval.adversarial.scoring` reads them to decide that a clean
round through an uncontended pool is **unscored** rather than held.

**Slots are declared, not inferred.** A target says how many connections it
pools through :class:`Pooled`, and a target that says nothing is claiming it does
not pool at all -- which is true of the in-process week-one slice and false of
anything with Snowflake behind it. The contract is the one
:attr:`~chip_chat.eval.adversarial.run.Target.capabilities` already sets, in the
same direction: understate the target, and the error lands on *unscored* rather
than on *held*. An adapter that pools and does not say so is the one lie this
module cannot catch, and it is named here rather than left implicit.

**Peak is what "hot" means, and it is measured rather than intended.** A soak
that launched forty turns proves nothing about how many were ever in flight
together; :attr:`Heat.peak` sweeps the intervals and reports the largest number
that genuinely were. A soak whose peak is one ran a sequential test with a thread
pool attached to it, and RFC-001 section 05 says what a sequential test is worth
here.

This module imports nothing from :mod:`chip_chat.eval.adversarial.run`, and that
is deliberate rather than incidental: what a round *was* has to be describable
without reference to how it was driven, so that a run read back off disk can
still be judged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_ROUNDS",
    "Heat",
    "Pooled",
    "Pressure",
    "Window",
    "measure",
    "slots_of",
]

DEFAULT_ROUNDS: Final = 24
"""Turns per visitor in a sustained round, unless the caller says otherwise.

Not a tuning constant -- a floor on the number of hand-offs a soak can produce.
One round of three visitors through a pool of two forces at most a couple of
hand-offs, and a couple of hand-offs is a coin toss rather than a test. Twenty
four rounds of three visitors is seventy-two checkouts through the same slots,
which is the difference between *"it did not bleed"* and *"it did not bleed
across seventy-two opportunities to"*.

Deliberately smaller than the 1,280 checkouts ``api/tests/test_pool_concurrency
.py`` drives through :class:`~chip_chat.api.pool.VisitorPool`. That test calls no
model; a turn here is a whole conversation, and a number chosen for the pool
would make the adversarial suite unrunnable against anything real.
"""


@dataclass(frozen=True, slots=True)
class Window:
    """When one attempt was in flight, on the monotonic clock.

    Monotonic and not wall clock: what is being computed is whether two
    intervals overlapped, and a wall clock that steps backwards mid-run would
    manufacture or erase an overlap. The absolute values mean nothing and are
    never printed.

    Attributes:
        started: When the turn was handed to the target.
        finished: When it came back, however it came back.
    """

    started: float
    finished: float

    @property
    def duration(self) -> float:
        """How long the turn took, in seconds."""
        return self.finished - self.started

    def overlaps(self, other: "Window") -> bool:
        """Whether these two intervals were open at the same instant.

        Args:
            other: The other window.

        Returns:
            Whether they overlap. Half-open on both ends, so two turns that
            merely touch -- one finishing exactly as the next starts -- do not
            count. That is the sequential case, and calling it concurrent is
            the mistake this whole module is built to avoid.
        """
        return self.started < other.finished and other.started < self.finished


@runtime_checkable
class Pooled(Protocol):
    """A target that hands connections back and forth, and says how many.

    Optional, and its absence is a claim rather than an omission: a target that
    does not satisfy this protocol is saying it does not pool, which is true of
    :class:`~chip_chat.eval.adversarial.slice.SliceTarget` -- an in-process loop
    with one shared order desk and no connections at all -- and false of
    anything with a database behind it.

    Declaring it is what makes a clean concurrent round *mean* something. The
    number is the denominator of :attr:`Pressure.forced_handoff`, and without it
    a round cannot say whether any connection was ever contended.
    """

    @property
    def pool_slots(self) -> int:
        """How many connections this target keeps. At least one."""
        ...


def slots_of(target: object) -> int | None:
    """How many connections ``target`` pools, or ``None`` where it does not say.

    Args:
        target: Anything the suite can be run against.

    Returns:
        The declared count, or ``None``. ``None`` is not zero and is not
        unlimited: it is *undeclared*, and the two callers of this function
        treat it as "this target does not pool", which is what the absence of
        :class:`Pooled` asserts.
    """
    if isinstance(target, Pooled):
        return target.pool_slots
    return None


@dataclass(frozen=True, slots=True)
class Pressure:
    """Whether a hand-off was ever forced. The precondition of the whole test.

    Attributes:
        offered: How many turns were launched at the same instant. What the
            harness controls.
        slots: How many connections the target said it pools, or ``None`` where
            it declared none. What the target controls, and what it is trusted
            about -- see the module docstring.
    """

    offered: int
    slots: int | None = None

    @property
    def forced_handoff(self) -> bool | None:
        """Whether some visitor had to wait for a connection somebody else held.

        Returns:
            ``True`` where more turns were offered than there are connections
            to serve them, so at least one connection was necessarily reused
            across visitors while the round was hot. ``False`` where the pool
            was large enough that every turn could hold its own connection
            throughout -- which makes a clean result a fact about the arithmetic
            rather than about the isolation. ``None`` where the target declared
            no pool, and there is therefore no hand-off to force.
        """
        if self.slots is None:
            return None
        return self.offered > self.slots

    @property
    def detail(self) -> str:
        """One line, for a report and for an unmeasured reason."""
        if self.slots is None:
            return f"{self.offered} turns at once; the target declares no pool"
        return (
            f"{self.offered} turns at once against {self.slots} pooled "
            f"connection{'' if self.slots == 1 else 's'}"
        )


@dataclass(frozen=True, slots=True)
class Heat:
    """What one concurrent attack's round actually achieved.

    Every field is measured after the fact. Nothing here records what the
    harness *meant* to do, because the gap between those two is the entire
    subject of this module.

    Attributes:
        attack_id: Which attack this round ran.
        rounds: How many turns each visitor took, back to back.
        attempts: How many turns ran in total.
        overlapping: How many of them were in flight beside at least one other.
        peak: The largest number of turns genuinely in flight at one instant.
            Swept from the intervals, so a soak that launched forty turns and
            served them one at a time reports ``1`` and says so.
        span: Monotonic seconds from the first turn starting to the last one
            finishing. *Long enough*, as a number.
        pressure: Whether a hand-off was forced at all. See :class:`Pressure`.
    """

    attack_id: str
    rounds: int
    attempts: int
    overlapping: int
    peak: int
    span: float
    pressure: Pressure

    @property
    def interleaved(self) -> bool:
        """Whether anything was ever in flight beside anything else."""
        return self.peak > 1

    @property
    def overlap_rate(self) -> float:
        """The fraction of turns that overlapped at least one other.

        Reported rather than gated on. The gate is per attempt and already
        exists -- :attr:`~chip_chat.eval.adversarial.run.Attempt.concurrent_with`
        being empty makes that attempt unscored -- so a rate here would be a
        second threshold over the same fact. What it is for is the reader
        deciding whether to believe a clean round: 3/72 overlapping and 71/72
        overlapping are different claims and the verdict cannot tell them apart.
        """
        if not self.attempts:
            return 0.0
        return self.overlapping / self.attempts

    @property
    def could_have_caught_a_bleed(self) -> bool:
        """Whether this round was capable of producing the failure it looks for.

        Two conditions, and both are about the round rather than the product.
        Something has to have been in flight beside something else, and some
        connection has to have been handed from one visitor to another. A round
        failing either is unscored by
        :mod:`chip_chat.eval.adversarial.scoring`, whatever it came back saying.

        A target that declares no pool passes the second condition vacuously,
        which is correct: there is no connection to hand over, so the bleed
        RFC-001 section 05 describes is not a failure that target can have.
        """
        return self.interleaved and self.pressure.forced_handoff is not False

    @property
    def detail(self) -> str:
        """The round in one line, for a report row and for a failure message."""
        return (
            f"{self.attempts} turns over {self.rounds} round"
            f"{'' if self.rounds == 1 else 's'}, {self.overlapping} of them "
            f"overlapping, peak {self.peak} in flight, {self.span:.2f}s; "
            f"{self.pressure.detail}"
        )


def measure(
    attack_id: str,
    *,
    rounds: int,
    windows: Sequence[Window | None],
    pressure: Pressure,
) -> Heat:
    """Read a round's heat off the intervals its turns were in flight for.

    Args:
        attack_id: Which attack ran.
        rounds: How many turns each visitor took.
        windows: One per attempt, in any order. ``None`` for an attempt that
            never ran -- a broken barrier, a target that raised before the
            clock was stamped -- and those are counted in ``attempts`` and
            excluded from every interval computation, because an attempt that
            was never in flight cannot have overlapped anything and must not
            be able to raise the peak.
        pressure: Whether a hand-off was forced. Not derivable from here: it is
            a fact about the target and about how many turns were launched
            together, neither of which the intervals record.

    Returns:
        The heat.
    """
    timed = [window for window in windows if window is not None]
    return Heat(
        attack_id=attack_id,
        rounds=rounds,
        attempts=len(windows),
        overlapping=_overlapping(timed),
        peak=_peak(timed),
        span=_span(timed),
        pressure=pressure,
    )


def _overlapping(windows: Sequence[Window]) -> int:
    """How many windows were open at the same time as at least one other."""
    return sum(
        1
        for index, window in enumerate(windows)
        if any(
            window.overlaps(other)
            for position, other in enumerate(windows)
            if position != index
        )
    )


def _peak(windows: Sequence[Window]) -> int:
    """The largest number of windows open at any one instant.

    A sweep over the interval endpoints. Closes are processed before opens at
    an identical timestamp, so two turns that merely touch -- one finishing
    exactly as the next starts -- do not read as two in flight. That is the
    same half-open rule :meth:`Window.overlaps` uses, and the two have to agree
    or a round could report a peak of two with nothing overlapping anything.
    """
    events = sorted(
        [(window.finished, -1) for window in windows]
        + [(window.started, 1) for window in windows]
    )
    peak = 0
    live = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def _span(windows: Sequence[Window]) -> float:
    """Monotonic seconds from the first start to the last finish. Zero if empty."""
    if not windows:
        return 0.0
    return max(window.finished for window in windows) - min(
        window.started for window in windows
    )
