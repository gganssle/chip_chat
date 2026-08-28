"""Which lanes were wired when a number was produced, and how to wire them.

On 27 August 2026 ``make experiment-baseline`` was re-run after two changes that
should each have moved a number: the account and personalization lanes were
wired onto the deployment (``cc-lpy4``), and the chat deployment's capacity went
from 10,000 tokens a minute to 200,000, which removed the 429s that had been
landing rows in no rate at all. The result was byte-identical to the run before
it -- 14.7% task completion, 42.9% tool selection -- and that was the correct
answer to the question the harness had actually asked, because the harness was
not measuring the deployment. Every entry point took
:data:`~chip_chat.agent.lanes.NO_LANES` by default and none of them offered a way
to pass anything else.

The consequence is sharper than a stale number.
:data:`~chip_chat.agent.lanes.CONDITIONAL_TOOLS` withholds
``ask_account_question``, ``get_recommendations`` and ``match_meal_from_photo``
from a deployment that cannot answer them, which is right -- #64's argument is
that a tool nothing can answer is worse than an absent one. But it means the
account lane's rows were scored at 0% tool selection *because the tool the row
expects did not exist in the process doing the scoring*. That is not a model
failure and it is not a lane failure. It is a harness that could not express the
configuration it was reporting on, and ``docs/launch-readiness.md`` had been
holding a launch target against it.

This module is the two halves of the fix.

**A label, so that a number says what it was produced against.**
:class:`Wiring` is the wiring of a run reduced to something a document can print
and a comparison can check. Two numbers taken under different lane wirings are
not the same measurement, and a reader must never have to infer which one they
are looking at from the size of the number. Every rendered baseline and every
recorded result carries this now, and
:mod:`chip_chat.eval.experiment.compare` refuses to draw the delta table at all
when either side does not state it -- the discipline
:mod:`chip_chat.eval.retrieval.report` already keeps for an arm whose vector half
dropped, and for the same reason: a comparison that quietly averages over a
configuration difference is worse than no comparison, because somebody acts on
it.

**A builder, so that the wired configuration can actually be run.**
:func:`wire_lanes` assembles the same lanes the deployment assembles, by calling
the same two functions the deployment calls -- :func:`chip_chat.api.connect.
snowflake_connect` and :func:`chip_chat.api.app.build_lanes`. Not a copy of
them. A second assembly path here would be a second place where a credential is
resolved and a lane is composed, and the first time the two disagreed the
harness would be scoring a deployment that does not exist. This is the same
argument :mod:`chip_chat.eval.retrieval` makes for calling the real
``Retriever``, and :mod:`chip_chat.eval.adversarial.gate2` makes for attacking
the real ops service.

**Identity is bound here exactly as the application binds it.**
:class:`~chip_chat.api.pool.VisitorPool` takes a session id and asks a store who
that session belongs to; nothing may hand it a ``demo_id``. So the harness
supplies a store -- :class:`OneVisitor` -- and the pool resolves through it, which
means the run's identity comes from the server side of the same seam a request
comes through. It is deliberately *one* visitor for the whole run: the golden
set's :data:`~chip_chat.eval.golden.slice.SLICE_PERSONA` says the set is written
for one archetype, and a run that bound a different synthetic customer per case
would be scoring the roster.

**Wiring costs money and credentials, so nothing here is on the free path.**
``make ci`` runs no target that reaches this module's builder. That is a rule in
this repository rather than an oversight -- *a gate that needs a logged-in human
is not a gate* -- so :data:`NO_WIRING` stays the default of every entry point and
a wired run is an explicit ``--lanes wired``.
"""

import argparse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from chip_chat.agent.lanes import NO_LANES, Lanes

if TYPE_CHECKING:  # pragma: no cover - imported for the annotations only
    from chip_chat.api.pool import VisitorPool
    from chip_chat.api.visitors import PersonaFixture

__all__ = [
    "LANE_CHOICES",
    "LANE_NAMES",
    "NO_WIRING",
    "UNSTATED",
    "UNWIRED",
    "WIRED",
    "LaneWiringError",
    "OneVisitor",
    "WiredLanes",
    "Wiring",
    "add_lanes_option",
    "run_lanes",
    "stated",
    "wire_lanes",
]

UNSTATED: Final = ""
"""What a result that never recorded its wiring carries.

Distinct from :attr:`Wiring.label` of :data:`NO_WIRING`, which is ``"none"`` and
is a measurement: *this run had no lanes wired*. Empty is the absence of a
measurement -- a file written before anything recorded the configuration -- and
the two must not collapse, because a comparison can subtract two runs that both
had no lanes and cannot subtract a run from one that never said.
"""

LANE_NAMES: Final = ("knowledge", "account", "personalization", "photo")
"""The four lanes, in :meth:`chip_chat.agent.lanes.Lanes.describe`'s order.

The order is that of RFC-001 §06 rather than alphabetical, so a label reads the
way the architecture table reads and two labels sort into the same shape.
"""

_POOL_SIZE: Final = 4
"""Live Snowflake connections a wired run holds.

The runs are sequential -- one case at a time, by construction, because a
conversation carried across cases would make the set order-dependent -- so one
connection would do. Four is slack for the roster read and for a checkout that
has to be discarded and reopened, at the cost of three idle sessions on a
warehouse that is already running.
"""


@dataclass(frozen=True, slots=True)
class Wiring:
    """Which of the four lanes were wired when a run was scored.

    A projection of :class:`~chip_chat.agent.lanes.Lanes` down to the only thing
    a document and a comparison need from it: not the objects, which are a
    retriever and two Snowflake lanes and a photo pipeline, but *which of them
    were there*. That is what makes two numbers comparable or not, and it is
    small enough to write into a JSON record and read back in six months.

    Attributes:
        knowledge: Hybrid retrieval over the harvested corpus was wired.
        account: Cortex Analyst and the points read were wired.
        personalization: The gold marts were wired.
        photo: Stage 4 and stage 5 were wired.
    """

    knowledge: bool = False
    account: bool = False
    personalization: bool = False
    photo: bool = False

    @classmethod
    def of(cls, lanes: Lanes) -> "Wiring":
        """Read the wiring off the lanes themselves.

        Args:
            lanes: What a run was given.

        Returns:
            The label. Built from :meth:`~chip_chat.agent.lanes.Lanes.describe`
            rather than from the fields, so a fifth lane added there arrives
            here as a ``TypeError`` at the door instead of being silently left
            out of every label.
        """
        return cls(**lanes.describe())

    @property
    def wired(self) -> tuple[str, ...]:
        """The lanes that were wired, in :data:`LANE_NAMES` order."""
        present = {
            "knowledge": self.knowledge,
            "account": self.account,
            "personalization": self.personalization,
            "photo": self.photo,
        }
        return tuple(name for name in LANE_NAMES if present[name])

    @property
    def label(self) -> str:
        """How a document and a record spell this.

        ``none`` where nothing was wired, and the lanes joined by ``+``
        otherwise: ``account+personalization``. Never empty -- see
        :data:`UNSTATED` for why the distinction matters.
        """
        return "+".join(self.wired) if self.wired else "none"

    def __str__(self) -> str:
        """The label, so an f-string in a report cannot print a dataclass repr."""
        return self.label


NO_WIRING: Final = Wiring()
"""No lane wired: the week-one slice, and the default of every entry point.

Labelled ``none``, which is a statement about a run rather than the absence of
one. :data:`UNSTATED` is the absence.
"""


def stated(label: str) -> bool:
    """Whether a recorded lane configuration says anything.

    Args:
        label: What a result carries.

    Returns:
        ``False`` for :data:`UNSTATED`, which is what a file written before
        anything recorded the wiring holds.
    """
    return label != UNSTATED


class LaneWiringError(RuntimeError):
    """The lanes a run asked for could not be assembled.

    Raised rather than falling back to :data:`~chip_chat.agent.lanes.NO_LANES`,
    which is the whole point of this module: a run that silently degrades to the
    unwired slice produces exactly the number that started all this, and it
    produces it under a document heading that says the deployment was measured.
    """


class OneVisitor:
    """The session store a wired run resolves identity through.

    :class:`~chip_chat.api.pool.VisitorPool` will not take a ``demo_id`` from a
    caller -- RFC-001 §05's trusted path is that the server states who a session
    belongs to, and the pool asks a store rather than being told. This is that
    store, holding one visitor: every session id in the run resolves to the same
    synthetic customer, which is what the golden set's one-archetype scope
    already assumes.

    Mutable, and deliberately so: the roster is read *through* the pool, on the
    one deliberately unbound checkout, so the pool has to exist before there is
    a visitor to put in it. :meth:`bind` is called once, before any case runs.
    """

    __slots__ = ("_demo_id",)

    def __init__(self, demo_id: str | None = None) -> None:
        """Initialise the store.

        Args:
            demo_id: The visitor, where one is already known.
        """
        self._demo_id = demo_id

    @property
    def demo_id(self) -> str | None:
        """Who every session in this run belongs to, or ``None`` before binding."""
        return self._demo_id

    def bind(self, demo_id: str) -> None:
        """Name the visitor this run is for.

        Args:
            demo_id: The synthetic customer, read off the roster.
        """
        self._demo_id = demo_id

    def demo_id_for(self, session_id: str) -> str | None:
        """Who ``session_id`` belongs to.

        Args:
            session_id: The case's session. Ignored: a run is one visitor.

        Returns:
            The bound visitor, or ``None`` before :meth:`bind`, which the pool
            turns into an :class:`~chip_chat.api.pool.UnboundSessionError`
            rather than into an unscoped query.
        """
        return self._demo_id


def _nothing_to_close() -> None:
    """Release nothing. An unwired run holds no connection to release."""


@dataclass(frozen=True, slots=True)
class WiredLanes:
    """Assembled lanes, the visitor they answer for, and the pool behind them.

    Also the shape of an *unwired* run -- :func:`run_lanes` yields one of these
    either way, with no visitor and :data:`~chip_chat.agent.lanes.NO_LANES`.
    That is deliberate: a caller that had to branch on which kind of run it was
    holding would be a caller that could forget to say, and saying is the whole
    of this module.

    Attributes:
        lanes: What to hand a deployment.
        visitor: The synthetic customer every session in the run is bound to,
            or ``None`` on an unwired run, which is bound to nobody and answers
            from :data:`chip_chat.agent.hardcoded.ACCOUNT`. Carried so the run
            can name it: a number produced against one account is a number
            about that account's rows.
        close: Releases the connections. Called by :func:`wire_lanes`'s context
            manager; a caller that built one by hand owes it.
    """

    lanes: Lanes = NO_LANES
    visitor: "PersonaFixture | None" = None
    close: Callable[[], None] = _nothing_to_close

    @property
    def wiring(self) -> Wiring:
        """The label for what came up."""
        return Wiring.of(self.lanes)

    @property
    def note(self) -> str:
        """One line saying what this run is measuring, for the console.

        Printed before the first model call rather than after the last, so that
        somebody who meant to run wired and forgot the flag finds out before
        they have paid for thirty-four turns.
        """
        if self.visitor is None:
            return (
                f"lanes: {self.wiring} — the hardcoded three-item menu and the "
                "account fixture answer, and the three conditional tools are "
                "not offered at all"
            )
        return (
            f"lanes: {self.wiring} — bound to {self.visitor.demo_id} "
            f"({self.visitor.persona_id}, rank {self.visitor.rank}, "
            f"{self.visitor.order_count} orders)"
        )


@contextmanager
def wire_lanes(
    persona: str,
    *,
    env: Mapping[str, str] | None = None,
    pool_size: int = _POOL_SIZE,
) -> Iterator[WiredLanes]:
    """Assemble the deployment's lanes, and bind the run to one visitor.

    The three steps are the application's own, in the application's order, and
    every one of them is a call into ``api/`` rather than a re-implementation of
    it. See the module docstring on why that is not merely tidy.

    1. :func:`chip_chat.api.connect.snowflake_connect` reads the environment and
       answers ``None`` where there is no credential -- which is a configuration
       fact rather than an error, and is why it returns an optional.
    2. :class:`~chip_chat.api.pool.VisitorPool` is built around :class:`OneVisitor`,
       and :class:`~chip_chat.api.visitors.SnowflakeRoster` reads the assignable
       customers through the pool's one unbound checkout. The rank-one fixture
       for ``persona`` is the run's visitor, and it is bound before any case
       runs.
    3. :func:`chip_chat.api.app.build_lanes` composes the account and
       personalization lanes over that pool.

    **Two of the five lanes will be absent and that is the current deployment.**
    ``build_lanes`` wires account and personalization; knowledge needs a
    retriever against the live alias (``cc-e1sr``) and photo needs the upload
    route and a production catalogue loader (``cc-mpd``). So the label this
    yields is ``account+personalization`` today, and it will say something
    longer on its own the day those two land -- which is the property that keeps
    this from becoming a second statement of what is deployed.

    Args:
        persona: The archetype to bind the run to, as ``population.toml`` spells
            it. The golden set's :data:`~chip_chat.eval.golden.slice.
            SLICE_PERSONA` is what a golden, trajectory, grounding or experiment
            run passes.
        env: The environment to read the credential from. ``None`` is the
            process environment.
        pool_size: Live connections. See :data:`_POOL_SIZE`.

    Yields:
        The lanes, the visitor and the pool, for the life of the block.

    Raises:
        LaneWiringError: If there is no Snowflake credential in the environment,
            or the roster holds no populated fixture for ``persona``. Both mean
            the run cannot measure what it was asked to measure, and both are
            better as a refusal than as a quiet fall back to the unwired slice.
    """
    # Imported here rather than at module scope for the reason
    # `chip_chat.api.connect` imports the driver inside its factory: a free run
    # imports this module for `Wiring` alone, and it should not pay for FastAPI,
    # the Snowflake connector or the Azure identity chain to print a label.
    from chip_chat.api.app import build_lanes
    from chip_chat.api.connect import ACCOUNT_VARIABLE, snowflake_connect
    from chip_chat.api.pool import VisitorPool

    connect = snowflake_connect(env)
    if connect is None:
        raise LaneWiringError(
            "there is no Snowflake credential in this environment, so the "
            f"account and personalization lanes cannot be wired: {ACCOUNT_VARIABLE} "
            "and one of SNOWFLAKE_PRIVATE_KEY, SNOWFLAKE_PRIVATE_KEY_PATH or "
            "AZURE_KEY_VAULT_URI have to be set. Run without --lanes wired to "
            "score the unwired slice, and read the result as that"
        )
    sessions = OneVisitor()
    pool = VisitorPool(connect, sessions=sessions, size=pool_size)
    try:
        visitor = _visitor(pool, persona)
        sessions.bind(visitor.demo_id)
        wired = WiredLanes(lanes=build_lanes(pool), visitor=visitor, close=pool.close)
        if not wired.wiring.wired:
            raise LaneWiringError(
                "a Snowflake connection was opened but no lane came up, which "
                "means chip_chat.api.app.build_lanes declined the pool it was "
                "given; there is nothing here that a wired run would measure"
            )
        yield wired
    finally:
        pool.close()


def _visitor(pool: "VisitorPool", persona: str) -> "PersonaFixture":
    """The strongest exemplar of ``persona`` on the roster.

    Read through :class:`~chip_chat.api.visitors.SnowflakeRoster` -- the same
    query the application's entry flow chooses a visitor with -- on the pool's
    one deliberately unbound checkout, which is the read #43's ``entry_roster``
    policy exists for.

    Args:
        pool: The connection pool.
        persona: The archetype.

    Returns:
        The rank-one populated fixture for that archetype.

    Raises:
        LaneWiringError: If the roster holds none. An empty roster means #47's
            load has not run, and a run bound to nobody would answer every
            account question with an empty account -- the exact failure
            ``docs/decisions/shipped-persona-roster.md`` is written about.
    """
    from chip_chat.api.visitors import SnowflakeRoster

    fixtures = [
        fixture
        for fixture in SnowflakeRoster(pool).fixtures()
        if fixture.persona_id == persona
    ]
    if not fixtures:
        raise LaneWiringError(
            f"the roster holds no populated fixture for the {persona!r} persona, "
            "so there is no synthetic customer to bind this run to; a wired run "
            "against an empty account measures the roster rather than the agent"
        )
    return sorted(fixtures, key=lambda fixture: fixture.rank)[0]


UNWIRED: Final = "none"
"""``--lanes none``: the week-one slice. Free, offline, and the default."""

WIRED: Final = "wired"
"""``--lanes wired``: whatever this environment's credential can bring up."""

LANE_CHOICES: Final = (UNWIRED, WIRED)
"""What ``--lanes`` accepts.

Two values rather than one per lane, because the wired set is not a thing the
person running the eval chooses: it is what the deployment has, and #64's
argument is that a lane which cannot answer must not be offered. ``wired`` means
*ask the deployment's own assembly what it can bring up and record the answer*,
which is why the recorded label is read off the lanes rather than off the flag.
"""


def add_lanes_option(parser: argparse.ArgumentParser) -> None:
    """Add ``--lanes`` to a runner's command line.

    Written once and shared by the four entry points that can run a deployment,
    so the flag means the same thing and reads the same way in all of them. The
    alternative -- four ``add_argument`` calls with four help strings -- is four
    places for the default to drift, and the default is the load-bearing part:
    ``make ci`` must stay free, offline and credential-free.

    Args:
        parser: The parser to add it to.
    """
    parser.add_argument(
        "--lanes",
        choices=LANE_CHOICES,
        default=UNWIRED,
        help=(
            f"which lanes to run against (default: {UNWIRED}). {WIRED!r} builds "
            "the account and personalization lanes the deployment builds, from "
            "the Snowflake credential in the environment, and needs one"
        ),
    )


@contextmanager
def run_lanes(
    choice: str,
    persona: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Iterator[WiredLanes]:
    """The lanes a run asked for on its command line.

    Args:
        choice: One of :data:`LANE_CHOICES`.
        persona: The archetype a wired run binds to. See :func:`wire_lanes`.
        env: The environment to read the credential from.

    Yields:
        The lanes, wired or not, for the life of the block. An unwired run
        yields a :class:`WiredLanes` too, carrying
        :data:`~chip_chat.agent.lanes.NO_LANES` and no visitor, so that a caller
        cannot report one kind of run and be handed the other.

    Raises:
        LaneWiringError: If ``wired`` was asked for and could not be assembled.
        ValueError: If ``choice`` is not one of :data:`LANE_CHOICES`.
    """
    if choice == UNWIRED:
        yield WiredLanes()
        return
    if choice != WIRED:
        raise ValueError(f"{choice!r} is not one of {LANE_CHOICES}")
    with wire_lanes(persona, env=env) as wired:
        yield wired
