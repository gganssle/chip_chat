"""Running the suite against a target, and proving the concurrent part was concurrent.

#30's first acceptance criterion is *the suite runs against any deployment and
reports pass/fail per attack*, and the seam that makes "any" true is
:class:`Target` -- four members wide, with
:mod:`chip_chat.eval.adversarial.slice` on the other side of it.

Everything else in this module exists to stop the suite from scoring zero
breaches while measuring nothing. There are three ways that happens and each has
a mechanism here.

**A target might not be able to hold the attack's premise, and would then survive
it trivially.** A deployment serving one hardcoded account to every visitor
cannot disclose one visitor's data to another, not because it is isolated but
because there is only one visitor's data. :class:`Capability` is how a target
says what it can be attacked *through*, and an attack needing a capability the
target lacks is unscored rather than held. This is not hypothetical here:
``chip_chat.api.app`` serves ``chip_chat.agent.hardcoded.ACCOUNT`` to everybody,
so today's deployment does not report :attr:`Capability.ISOLATED_ACCOUNTS` and
its account-disclosure attacks come back unmeasured.

**A canary might not be reachable, and then nothing could have leaked it.** A
target that answers *"I'm not sure"* to every question scores a perfect zero
disclosures. :class:`Control` is the positive control: before a visitor's canary
is treated as something another visitor failed to obtain, that visitor is asked
for it *through the same surface*, and a canary they cannot see themselves makes
every attack on it unscoreable. This is the eval-side form of a failure this
repository keeps finding -- a guard that is correct and unreachable stops
nothing, and an attack that is correct and unreachable proves nothing.

**A concurrent test might not actually overlap.** This is the one RFC-001
section 05 calls out: *sequential tests will pass regardless*. A loop that
submits eight turns to a thread pool and gets them back one after another is a
sequential test wearing threads. So :func:`run_concurrently` starts every turn
from a :class:`threading.Barrier` *and* records the interval each was in flight,
and :attr:`Attempt.concurrent_with` names the attempts that genuinely overlapped
it. An attempt that overlapped nothing is unscored. The barrier makes overlap
likely; the windows are what make it *known*, and only the second one is
evidence.

**One attack's failure is one attack's failure.** A target that raises on the
eleventh attack must not cost the other forty, so every attempt runs inside its
own ``try`` and an adapter error becomes a recorded :attr:`Attempt.error`. An
outage is not a design holding.
"""

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from chip_chat.eval.adversarial.attacks import (
    FOREIGN_CANARY,
    AdversarialSuite,
    Attack,
    Breach,
    Capability,
)
from chip_chat.eval.adversarial.canaries import Population, Visitor
from chip_chat.eval.golden.run import Signal

__all__ = [
    "BARRIER_TIMEOUT_SECONDS",
    "SIGNAL_OF",
    "Attempt",
    "Capability",
    "Control",
    "Judge",
    "Probe",
    "Run",
    "Signal",
    "Target",
    "Window",
    "run_concurrently",
    "run_suite",
]

BARRIER_TIMEOUT_SECONDS: Final = 30.0
"""How long a concurrent round waits for every visitor to reach the line.

Generous, because the thing on the other side may be a network. A round that
times out is recorded as an error on every attempt in it rather than retried:
a barrier that broke means the turns did not start together, and a concurrency
result from a round that did not start together is the exact thing this module
is arranged to refuse to produce.
"""


# `Capability` is defined in `attacks`, beside the manifest's other closed
# vocabularies, because it is named on an attack rather than only on a target --
# see that module. It is re-exported here so that an adapter author writing a
# `Target` imports one module rather than two.


SIGNAL_OF: Final[Mapping[Breach, frozenset[Signal]]] = {
    Breach.CANARY_IN_REPLY: frozenset(),
    Breach.WRITE_EXECUTED: frozenset({Signal.WRITES}),
    Breach.TOOL_REACHED: frozenset({Signal.TOOLS}),
    Breach.INVENTED: frozenset(),
}
"""What each deterministic breach needs *observed* to be scoreable at all.

:class:`~chip_chat.eval.golden.run.Signal` is the golden set's, imported rather
than restated: it means exactly the same thing here, and two enumerations of
"what a deployment can see about a turn" would be one enumeration and a bug
waiting for somebody to add a member to the wrong one.

:attr:`Breach.CANARY_IN_REPLY` needs no signal because every target reports what
the visitor saw -- that is what a target *is*. The write gate needs
:attr:`~chip_chat.eval.golden.run.Signal.WRITES`, and a target that cannot report
it leaves that gate unmeasured rather than clean.
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


@dataclass(frozen=True, slots=True)
class Probe:
    """One attack, aimed by one visitor. The unit of work a target is handed.

    Attributes:
        attack: What is being attempted.
        visitor: Who is attempting it. Their canary is the one that is *not* a
            breach if it comes back; everybody else's is.
        foreign: Another visitor's canary, spliced into the attack wherever it
            writes :data:`~chip_chat.eval.adversarial.attacks.FOREIGN_CANARY`.
            Empty for the attacks that do not ask for one, which is most of
            them: an attacker usually has to *obtain* the secret, and only the
            draft-theft shape starts from already holding it.
    """

    attack: Attack
    visitor: Visitor
    foreign: str = ""

    @property
    def attempt_id(self) -> str:
        """``attack:visitor``. Unique in a run, and the join key throughout."""
        return f"{self.attack.attack_id}:{self.visitor.visitor_id}"

    @property
    def message(self) -> str:
        """What to say, with any foreign canary spliced in."""
        return self.attack.message.replace(FOREIGN_CANARY, self.foreign)

    @property
    def context(self) -> tuple[str, ...]:
        """The prior turns the attack presupposes, likewise resolved."""
        return tuple(
            line.replace(FOREIGN_CANARY, self.foreign) for line in self.attack.context
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    """What came of one probe.

    Attributes:
        attempt_id: :attr:`Probe.attempt_id`, so a run scored later can be
            matched to a suite without depending on order.
        attack_id: Which attack.
        visitor_id: Which visitor made it.
        visible: Everything this visitor could see afterwards -- the prose and
            the card, concatenated. One field rather than two because a canary
            rendered on a widget is as disclosed as one written in a sentence,
            and a detector reading only the prose would miss the more likely
            half.
        tools: Tools called on the turn, in call order.
        wrote: Whether a write executed.
        confirmed: Whether this visitor had confirmed anything the turn could
            act on. An attack is a launch-gate failure only where this is
            ``False``: a write after a confirmation is the product working.
        card: The confirmation card or receipt, where one was rendered.
        window: When the turn was in flight, where the runner measured it.
            ``None`` for a sequential attempt, which does not need one.
        concurrent_with: The ids of attempts genuinely overlapping this one.
            Empty on a sequential attempt, and empty on a *concurrent* attempt
            that did not actually overlap -- which is not the same thing, and
            :mod:`chip_chat.eval.adversarial.scoring` reads the attack to tell
            them apart.
        error: Why there is nothing here, in one line. ``None`` on success.
        capabilities: What the target that produced this could be attacked
            through. Carried on the attempt rather than only on the target, so
            a run serialised to disk and scored later still knows what it could
            and could not have caught.
        reports: Which signals the target could observe about the turn. Carried
            for the same reason, and separate from ``capabilities`` because
            they answer different questions -- see :class:`Capability`.
    """

    attempt_id: str
    attack_id: str
    visitor_id: str
    visible: str = ""
    tools: tuple[str, ...] = ()
    wrote: bool = False
    confirmed: bool = False
    card: Mapping[str, Any] | None = None
    window: Window | None = None
    concurrent_with: tuple[str, ...] = ()
    error: str | None = None
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    reports: frozenset[Signal] = field(default_factory=frozenset)

    @property
    def answered(self) -> bool:
        """Whether the target produced anything at all for this probe."""
        return self.error is None

    def alongside(self, others: Iterable[str]) -> "Attempt":
        """This attempt, told which others it overlapped.

        Args:
            others: Their attempt ids.

        Returns:
            A copy carrying them. The runner computes overlap only once every
            window is closed, which is after the attempt exists.
        """
        return Attempt(
            attempt_id=self.attempt_id,
            attack_id=self.attack_id,
            visitor_id=self.visitor_id,
            visible=self.visible,
            tools=self.tools,
            wrote=self.wrote,
            confirmed=self.confirmed,
            card=self.card,
            window=self.window,
            concurrent_with=tuple(others),
            error=self.error,
            capabilities=self.capabilities,
            reports=self.reports,
        )


@dataclass(frozen=True, slots=True)
class Control:
    """Whether a visitor can see their own canary. The positive control.

    The question this answers is *"could this secret have leaked at all?"*, and
    the reason it has to be asked is that the alternative to asking it is
    believing a target that says nothing. See the module docstring.

    Attributes:
        visitor_id: Whose canary was looked for.
        visible: Whether it came back through the ordinary surface.
        detail: What happened, in one line, for the report. Says why where
            ``visible`` is ``False``, since that is the case a reader has to
            act on.
    """

    visitor_id: str
    visible: bool
    detail: str = ""


@runtime_checkable
class Target(Protocol):
    """Something the adversarial suite can be run against.

    Three members. A target is anything that can be handed a probe, say what it
    can be attacked through, and demonstrate that a visitor's canary was
    reachable in the first place -- the in-process week-one slice, a URL, a
    Foundry hosted agent.
    """

    @property
    def name(self) -> str:
        """What was attacked, for the report. A deployment, a URL, a build."""
        ...

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What this target can be attacked through.

        Declared rather than inferred, and understated rather than overstated.
        A target claiming :attr:`Capability.ISOLATED_ACCOUNTS` it does not have
        turns every disclosure attack from unscored into held, which is the one
        change to this report that would matter and the one nobody would see.
        """
        ...

    @property
    def population(self) -> Population:
        """The visitors attacking it, each holding a canary this target planted.

        The target mints these rather than being handed them, because planting
        is the one step nothing else can do. A canary is only evidence if it
        really is somewhere only that visitor's data is, and only the target
        knows where that is -- a row for a deployment with accounts, a draft id
        for the week-one slice, which has session-scoped drafts and no accounts
        at all.

        Preparing the population may cost turns. It is not a measurement and
        nothing scores it: :meth:`control` is the measurement, and what it
        establishes is that what was planted here can come back out through a
        reply.
        """
        ...

    @property
    def reports(self) -> frozenset[Signal]:
        """Which signals this target can observe about a turn.

        :class:`~chip_chat.eval.golden.run.Signal`, the golden set's, meaning
        the same thing. A target that cannot report
        :attr:`~chip_chat.eval.golden.run.Signal.WRITES` cannot be scored on
        the second launch gate, and says so here rather than defaulting
        ``wrote`` to ``False`` and reading as clean.
        """
        ...

    def control(self, visitor: Visitor) -> Control:
        """Check that ``visitor`` can see their own canary.

        Must be answered through the same surface a visitor uses -- a store
        lookup would prove the canary exists, which is not the question. What
        is being established is that a *reply* can contain it, because that is
        the only channel a disclosure could travel down.

        Args:
            visitor: Whose canary to look for.

        Returns:
            The control. ``visible=False`` with a reason is the right answer
            for a target that cannot plant a canary at all, and is far better
            than a target that quietly reports every attack as held.
        """
        ...

    def turn(self, probe: Probe) -> Attempt:
        """Run one probe and report what happened.

        Args:
            probe: The attack, and who is making it. The attack's ``context``
                is prior assistant turns the message presupposes, and an
                adapter that drops them is attacking a different question.

        Returns:
            The attempt. Raising is permitted -- the runner records it against
            the probe -- but returning an :class:`Attempt` with an ``error`` is
            better where the adapter knows what went wrong.
        """
        ...


class Judge(Protocol):
    """Settles a breach that no data structure can settle.

    Deliberately not implemented in this package, for the reason
    :class:`chip_chat.eval.golden.run.Judge` gives: the judge is a model, it
    costs tokens, and choosing one is #72's problem. What #30 owes is a suite
    with :data:`~chip_chat.eval.adversarial.attacks.JUDGED` *named*, so that the
    day a judge arrives it has somewhere to attach.
    """

    def verdict(self, breach: Breach, attack: Attack, attempt: Attempt) -> bool | None:
        """Whether ``attempt`` breached ``attack`` in this way.

        Args:
            breach: One of :data:`~chip_chat.eval.adversarial.attacks.JUDGED`.
            attack: What was attempted, and why.
            attempt: What came back.

        Returns:
            ``True`` where the judge is willing to say the attack succeeded,
            ``False`` where it is willing to say it did not, and ``None`` where
            it is not willing to say -- which is unscored, and therefore blocks
            any gate the attack belongs to. A judge that never returns ``None``
            is a judge that guesses, and on this side of the product a guess
            reads as a clean gate.
        """
        ...


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of the suite: what was attacked, by whom, and what happened.

    Attributes:
        target: What was attacked, as it names itself.
        capabilities: What it could be attacked through.
        reports: What it could observe about a turn.
        population: The visitors, so a scorer can ask whose canary a reply held.
        controls: One per visitor. A run whose controls all failed is a run
            that measured nothing, and this is where that is visible.
        attempts: Every attempt, sequential and concurrent alike, in the order
            they were started.
    """

    target: str
    capabilities: frozenset[Capability]
    reports: frozenset[Signal]
    population: Population
    controls: tuple[Control, ...]
    attempts: tuple[Attempt, ...]

    def control_for(self, visitor_id: str) -> Control | None:
        """The control for one visitor, or ``None`` where none was run."""
        for control in self.controls:
            if control.visitor_id == visitor_id:
                return control
        return None

    @property
    def visible_canaries(self) -> frozenset[str]:
        """The visitors whose canary was demonstrably reachable.

        A disclosure attack is scoreable only where the canary that could have
        leaked is one of these. Where the set is empty the first launch gate is
        unmeasured on this run, whatever the attempts came back saying.
        """
        return frozenset(
            control.visitor_id for control in self.controls if control.visible
        )


def run_suite(
    suite: AdversarialSuite,
    target: Target,
    *,
    only: Sequence[str] | None = None,
    barrier_timeout: float = BARRIER_TIMEOUT_SECONDS,
) -> Run:
    """Run every attack against every visitor, and the concurrent ones together.

    Controls first, always. A run that scored its attacks before establishing
    that the canaries were reachable would spend the whole suite and then find
    out it had measured nothing -- and, worse, would have produced a clean pair
    of gates on the way.

    Sequential attacks are then run one visitor at a time, in suite order.
    Concurrent attacks are each run by the whole population at once; see
    :func:`run_concurrently`.

    Args:
        suite: The attacks.
        target: What to attack. Its ``population`` is who attacks it.
        only: Attack ids to run, for iterating on one attack. ``None`` runs all.
        barrier_timeout: How long a concurrent round waits for every visitor.

    Returns:
        The run, controls first.
    """
    wanted = None if only is None else set(only)
    population = target.population
    controls = tuple(_control(target, visitor) for visitor in population)

    attempts: list[Attempt] = []
    for attack in suite.sequential:
        if wanted is not None and attack.attack_id not in wanted:
            continue
        attempts.extend(
            _attempt(target, _probe(attack, population, index))
            for index in range(len(population))
        )
    for attack in suite.concurrent:
        if wanted is not None and attack.attack_id not in wanted:
            continue
        attempts.extend(run_concurrently(attack, target, barrier_timeout=barrier_timeout))
    return Run(
        target=target.name,
        capabilities=target.capabilities,
        reports=target.reports,
        population=population,
        controls=controls,
        attempts=tuple(attempts),
    )


def run_concurrently(
    attack: Attack,
    target: Target,
    *,
    barrier_timeout: float = BARRIER_TIMEOUT_SECONDS,
) -> tuple[Attempt, ...]:
    """Run one attack from every visitor at the same instant, and prove it.

    The test RFC-001 section 05 asks for. Session variables and pooled
    connections bleed when a connection goes back to the pool with ``demo_id``
    still set and is handed to the next request before it is reassigned -- and
    the window in which that is observable is exactly the window in which two
    requests are in flight together. A test that ran these turns back to back
    would pass on a pool that bleeds every single time.

    Two mechanisms, and only the second one is evidence:

    * A :class:`threading.Barrier` holds every thread until all of them are
      ready, so the turns are *handed to the target* as close to simultaneously
      as the runtime allows. This makes overlap likely.
    * Each thread stamps the monotonic clock either side of its turn, and the
      attempts are then told which of their siblings they actually overlapped.
      This makes overlap *known*. An attempt overlapping nothing is scored
      unscored by :mod:`chip_chat.eval.adversarial.scoring`, however
      enthusiastically it was launched.

    Args:
        attack: The attack. Every visitor runs this same one.
        target: What to attack. Its ``population`` is who attacks it.
        barrier_timeout: How long to wait at the line before giving up. A
            broken barrier is recorded as an error on every attempt in the
            round rather than retried; see :data:`BARRIER_TIMEOUT_SECONDS`.

    Returns:
        One attempt per visitor, in population order, each carrying its window
        and the siblings it overlapped.
    """
    population = target.population
    barrier = threading.Barrier(len(population), timeout=barrier_timeout)
    probes = [_probe(attack, population, index) for index in range(len(population))]

    def _one(probe: Probe) -> Attempt:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return Attempt(
                attempt_id=probe.attempt_id,
                attack_id=probe.attack.attack_id,
                visitor_id=probe.visitor.visitor_id,
                error=(
                    "the concurrent round never started together: not every "
                    f"visitor reached the barrier within {barrier_timeout}s"
                ),
                capabilities=target.capabilities,
                reports=target.reports,
            )
        return _attempt(target, probe, timed=True)

    with ThreadPoolExecutor(max_workers=len(population)) as pool:
        attempts = list(pool.map(_one, probes))
    return _with_overlaps(attempts)


def _probe(attack: Attack, population: Population, index: int) -> Probe:
    """Aim one attack, handing the attacker their neighbour's canary.

    The neighbour is the next visitor round the ring rather than a fixed one,
    so that in a population of three every visitor is somebody's victim. A
    suite that always stole from ``v1`` would leave two thirds of the isolation
    rule untested and report the same clean gate either way.
    """
    visitor = population[index]
    if not attack.supplies_foreign_canary:
        return Probe(attack, visitor)
    neighbour = population[(index + 1) % len(population)]
    return Probe(attack, visitor, foreign=neighbour.token)


def _with_overlaps(attempts: Sequence[Attempt]) -> tuple[Attempt, ...]:
    """Tell each attempt which of the others it was genuinely in flight beside."""
    return tuple(
        attempt.alongside(
            other.attempt_id
            for other in attempts
            if other.attempt_id != attempt.attempt_id
            and other.window is not None
            and attempt.window is not None
            and attempt.window.overlaps(other.window)
        )
        for attempt in attempts
    )


def _control(target: Target, visitor: Visitor) -> Control:
    """Ask the target for one control, turning a raise into a failed control.

    A target that raises here has not demonstrated the canary is reachable,
    which is the only thing a control can conclude. Treating the exception as
    anything softer would let an adapter bug quietly re-enable every disclosure
    attack it broke.
    """
    try:
        return target.control(visitor)
    except Exception as error:  # a target is somebody else's code
        return Control(
            visitor_id=visitor.visitor_id,
            visible=False,
            detail=f"{type(error).__name__}: {error}",
        )


def _attempt(target: Target, probe: Probe, *, timed: bool = False) -> Attempt:
    """Run one probe, turning an adapter failure into a recorded line.

    Broad by design and narrow in what it does with what it catches, for the
    reason :func:`chip_chat.eval.golden.run._run_one` gives: a target is a
    network, a model and somebody else's code, and the failures it can produce
    are not enumerable from here. ``KeyboardInterrupt`` and ``SystemExit`` do
    not inherit from ``Exception`` and pass straight through.

    The window is stamped around the call rather than inside the target,
    because what has to be measured is the interval in which this turn was
    *the target's problem* -- a target that measured its own would be free to
    report an interval in which nothing overlapped.
    """
    started = time.monotonic()
    try:
        attempt = target.turn(probe)
    except Exception as error:
        attempt = Attempt(
            attempt_id=probe.attempt_id,
            attack_id=probe.attack.attack_id,
            visitor_id=probe.visitor.visitor_id,
            error=f"{type(error).__name__}: {error}",
            capabilities=target.capabilities,
            reports=target.reports,
        )
    if not timed:
        return attempt
    finished = time.monotonic()
    return Attempt(
        attempt_id=attempt.attempt_id,
        attack_id=attempt.attack_id,
        visitor_id=attempt.visitor_id,
        visible=attempt.visible,
        tools=attempt.tools,
        wrote=attempt.wrote,
        confirmed=attempt.confirmed,
        card=attempt.card,
        window=Window(started=started, finished=finished),
        concurrent_with=attempt.concurrent_with,
        error=attempt.error,
        capabilities=attempt.capabilities,
        reports=attempt.reports,
    )
