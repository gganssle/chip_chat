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
sequential test wearing threads. So :func:`run_sustained` starts every turn from
a :class:`threading.Barrier` *and* records the interval each was in flight, and
:attr:`Attempt.concurrent_with` names the attempts that genuinely overlapped it.
An attempt that overlapped nothing is unscored. The barrier makes overlap
likely; the windows are what make it *known*, and only the second one is
evidence.

**A round that overlapped might still not have contended.** #82's addition, and
the sharper form of the same mistake: three visitors against a pool of four
overlap perfectly and never hand a connection from one to another, so the bleed
has no window to happen in and the clean result is a fact about the arithmetic.
So a round is *sustained* -- each visitor takes many turns back to back, and the
threads free-run after the barrier rather than re-forming for each one -- and it
reports a :class:`~chip_chat.eval.adversarial.soak.Heat` saying how hot it got
and whether a hand-off was ever forced. That module holds the argument.

**One attack's failure is one attack's failure.** A target that raises on the
eleventh attack must not cost the other forty, so every attempt runs inside its
own ``try`` and an adapter error becomes a recorded :attr:`Attempt.error`. An
outage is not a design holding.
"""

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol, runtime_checkable

from chip_chat.eval.adversarial.attacks import (
    FOREIGN_CANARY,
    AdversarialSuite,
    Attack,
    Breach,
    Capability,
)
from chip_chat.eval.adversarial.canaries import Population, Visitor
from chip_chat.eval.adversarial.soak import (
    DEFAULT_ROUNDS,
    Heat,
    Pressure,
    Window,
    measure,
    slots_of,
)
from chip_chat.eval.golden.run import Signal

__all__ = [
    "BARRIER_TIMEOUT_SECONDS",
    "DEFAULT_ROUNDS",
    "SIGNAL_OF",
    "Attempt",
    "Capability",
    "Control",
    "Heat",
    "Judge",
    "Probe",
    "Round",
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


# `Window`, `Heat` and `Pressure` live in `chip_chat.eval.adversarial.soak`,
# which imports nothing from here. What a round *was* has to be describable
# without reference to how it was driven -- otherwise a run read back off disk
# could not be judged -- and that module holds the vocabulary while this one
# holds the driving. They are re-exported here so an adapter author writing a
# `Target` imports one module rather than two.


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
        return replace(self, concurrent_with=tuple(others))

    def in_round(self, index: int) -> "Attempt":
        """This attempt, stamped with which pass of a sustained round it was.

        ``attack:visitor`` is unique in a single round and repeats in a
        sustained one, and an id that repeats is one a reader cannot chase back
        to a turn. So the round is appended, from ``#1``.

        Args:
            index: The round, from zero.

        Returns:
            A copy under the stamped id. Called before overlaps are computed,
            so that ``concurrent_with`` names ids that exist.
        """
        return replace(self, attempt_id=f"{self.attempt_id}#{index + 1}")


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
        heats: One per concurrent attack: how hot its round got, and whether a
            connection was ever contended. Empty on a run holding no concurrent
            attack, and on one read back from a harness that predates #82 --
            which :meth:`heat_for` reports as ``None`` and the scorer treats as
            *nothing known about the pressure* rather than as adequate.
    """

    target: str
    capabilities: frozenset[Capability]
    reports: frozenset[Signal]
    population: Population
    controls: tuple[Control, ...]
    attempts: tuple[Attempt, ...]
    heats: tuple[Heat, ...] = ()

    def control_for(self, visitor_id: str) -> Control | None:
        """The control for one visitor, or ``None`` where none was run."""
        for control in self.controls:
            if control.visitor_id == visitor_id:
                return control
        return None

    def heat_for(self, attack_id: str) -> Heat | None:
        """How hot ``attack_id``'s round got, or ``None`` where none was recorded."""
        for heat in self.heats:
            if heat.attack_id == attack_id:
                return heat
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


@dataclass(frozen=True, slots=True)
class Round:
    """One concurrent attack, run, and how hot the running of it got.

    The two halves are inseparable and that is why they are one object. Every
    attempt in :attr:`attempts` is a fact about the target; :attr:`heat` is the
    fact about the *round* that says whether those facts are worth anything,
    and a caller handed only the first would have no way to tell a clean result
    from a round that could not have produced any other.

    Attributes:
        attempts: Every turn taken, in the order the round produced them, each
            carrying its window and the siblings it genuinely overlapped.
        heat: What the round achieved. See
            :class:`~chip_chat.eval.adversarial.soak.Heat`.
    """

    attempts: tuple[Attempt, ...]
    heat: Heat


def run_suite(
    suite: AdversarialSuite,
    target: Target,
    *,
    only: Sequence[str] | None = None,
    rounds: int = 1,
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
        rounds: Turns per visitor in each concurrent attack's round. One is the
            single burst #30 shipped; more is the *sustained* run #82 asks for,
            and :data:`~chip_chat.eval.adversarial.soak.DEFAULT_ROUNDS` is the
            argument for a number. It changes nothing about the sequential
            attacks, which have no hand-off to force.
        barrier_timeout: How long a concurrent round waits for every visitor.

    Returns:
        The run, controls first, carrying one
        :class:`~chip_chat.eval.adversarial.soak.Heat` per concurrent attack.
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
    heats: list[Heat] = []
    for attack in suite.concurrent:
        if wanted is not None and attack.attack_id not in wanted:
            continue
        round_ = run_sustained(
            attack, target, rounds=rounds, barrier_timeout=barrier_timeout
        )
        attempts.extend(round_.attempts)
        heats.append(round_.heat)
    return Run(
        target=target.name,
        capabilities=target.capabilities,
        reports=target.reports,
        population=population,
        controls=controls,
        attempts=tuple(attempts),
        heats=tuple(heats),
    )


def run_concurrently(
    attack: Attack,
    target: Target,
    *,
    barrier_timeout: float = BARRIER_TIMEOUT_SECONDS,
) -> tuple[Attempt, ...]:
    """Run one attack from every visitor at the same instant, and prove it.

    One burst: the single round #30 shipped, kept because it is the smallest
    thing that can express the question and the shape every test of this
    machinery is written against. :func:`run_sustained` is the same round held
    open, and this is that function with ``rounds=1`` and the heat dropped.

    Args:
        attack: The attack. Every visitor runs this same one.
        target: What to attack. Its ``population`` is who attacks it.
        barrier_timeout: How long to wait at the line before giving up.

    Returns:
        One attempt per visitor, in population order, each carrying its window
        and the siblings it overlapped.
    """
    return run_sustained(
        attack, target, rounds=1, barrier_timeout=barrier_timeout
    ).attempts


def run_sustained(
    attack: Attack,
    target: Target,
    *,
    rounds: int = DEFAULT_ROUNDS,
    barrier_timeout: float = BARRIER_TIMEOUT_SECONDS,
) -> Round:
    """Run one attack from every visitor at once, for as long as it takes to mean it.

    The test RFC-001 section 05 asks for, run the way #82 asks for it. Session
    variables and pooled connections bleed when a connection goes back to the
    pool with ``demo_id`` still set and is handed to the next request before it
    is reassigned -- and the window in which that is observable is exactly the
    window in which two requests are in flight together. A test that ran these
    turns back to back would pass on a pool that bleeds every single time.

    Four mechanisms, and the first is the only one #30 had:

    * A :class:`threading.Barrier` holds every visitor's thread until all of
      them are ready, so the first turns are *handed to the target* as close to
      simultaneously as the runtime allows. This makes overlap likely.
    * Each thread stamps the monotonic clock either side of every turn, and the
      attempts are then told which of their siblings they actually overlapped.
      This makes overlap *known*. An attempt overlapping nothing is scored
      unscored by :mod:`chip_chat.eval.adversarial.scoring`, however
      enthusiastically it was launched.
    * After the barrier the threads **free-run**, each taking ``rounds`` turns
      back to back with no further rendezvous. That is deliberate and it is the
      difference between hot and merely simultaneous: a barrier before every
      round would have every thread idling at the line for the slowest one, so
      the pool would drain between rounds and each burst would find it empty.
      Threads that never re-synchronise drift out of step, which is what keeps
      checkouts and returns interleaving rather than marching.
    * The round reports its own :class:`~chip_chat.eval.adversarial.soak.Heat`,
      including whether more turns were offered at once than the target has
      connections to serve them. A clean round through a pool nobody had to
      share is unscored; see :mod:`chip_chat.eval.adversarial.soak`.

    Args:
        attack: The attack. Every visitor runs this same one, every round.
        target: What to attack. Its ``population`` is who attacks it, and its
            declared pool size -- where it declares one -- is what says whether
            a hand-off was forced.
        rounds: Turns per visitor, back to back after the barrier. One is the
            single burst; :data:`~chip_chat.eval.adversarial.soak.DEFAULT_ROUNDS`
            is the argument for a sustained number.
        barrier_timeout: How long to wait at the line before giving up. A
            broken barrier is recorded as an error on every attempt in the
            round rather than retried; see :data:`BARRIER_TIMEOUT_SECONDS`.

    Returns:
        The round: every attempt, each carrying its window and the siblings it
        overlapped, and the heat measured over all of them.

    Raises:
        ValueError: If ``rounds`` is below one. A round nobody takes is not a
            gentler test than a round somebody takes; it is no test, and it
            would report a clean gate.
    """
    if rounds < 1:
        raise ValueError("a concurrent round needs at least one turn per visitor")
    population = target.population
    barrier = threading.Barrier(len(population), timeout=barrier_timeout)

    def _stamped(attempt: Attempt, number: int) -> Attempt:
        """The round stamp, and only where there is more than one to tell apart.

        A single burst keeps ``attack:visitor``, which is what #30 shipped and
        what every other test of this machinery joins on.
        """
        return attempt.in_round(number) if rounds > 1 else attempt

    def _visitor(index: int) -> list[Attempt]:
        probe = _probe(attack, population, index)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            stranded = _stranded(probe, target, barrier_timeout)
            return [_stamped(stranded, number) for number in range(rounds)]
        return [
            _stamped(_attempt(target, probe, timed=True), number)
            for number in range(rounds)
        ]

    with ThreadPoolExecutor(max_workers=len(population)) as pool:
        by_visitor = list(pool.map(_visitor, range(len(population))))

    # Interleaved rather than concatenated, so the attempts read in the order
    # the round actually produced them rather than visitor by visitor -- which
    # is how a reader chasing a disclosure back to what was in flight beside it
    # wants them, and how the sequential attempts above are already ordered.
    attempts = [column[number] for number in range(rounds) for column in by_visitor]
    overlapped = _with_overlaps(attempts)
    return Round(
        attempts=overlapped,
        heat=measure(
            attack.attack_id,
            rounds=rounds,
            windows=[attempt.window for attempt in overlapped],
            pressure=Pressure(offered=len(population), slots=slots_of(target)),
        ),
    )


def _stranded(probe: Probe, target: Target, barrier_timeout: float) -> Attempt:
    """One attempt from a round whose barrier broke.

    Recorded rather than retried. A barrier that broke means the turns did not
    start together, and a concurrency result from a round that did not start
    together is the exact thing this module is arranged to refuse to produce.
    """
    return Attempt(
        attempt_id=probe.attempt_id,
        attack_id=probe.attack.attack_id,
        visitor_id=probe.visitor.visitor_id,
        error=(
            "the concurrent round never started together: not every visitor "
            f"reached the barrier within {barrier_timeout}s"
        ),
        capabilities=target.capabilities,
        reports=target.reports,
    )


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
    return replace(attempt, window=Window(started=started, finished=time.monotonic()))
