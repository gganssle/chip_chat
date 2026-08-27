"""What each dataset row expects of a trajectory, and the two tables behind it.

Issue #74's first acceptance criterion is *a trajectory eval running against the
dataset*, so the register of what is expected is read from
:class:`~chip_chat.eval.dataset.entries.DatasetEntry` rather than from the golden
set directly. That is not a formality. A score taken against a dataset version
can be compared with a score taken three weeks later against the same version;
a score taken against "the manifest as it was on somebody's laptop" cannot, and
#72 exists because of the difference.

Only the rows that can be scored on lane selection become expectations.
:attr:`~chip_chat.eval.dataset.entries.DatasetEntry.scores_routing` is the flag,
and ``eval/README.md`` draws the line: a labeled photograph runs the vision lane
*directly*, so no model ever chose to enter it and there is no trajectory to
score.

Two tables live here, and both are design decisions this package owns rather
than facts it reads off something else.

:data:`SANCTIONED`
    Which second tool a turn may legitimately reach for without that being
    *reached for three when one would do*. ``get me my usual but add guac``
    reaches ``get_usual_order`` and then ``propose_order``, and scoring the
    second call as waste would mark the correct trajectory wrong --
    :mod:`chip_chat.eval.golden.scoring` makes the same argument in reverse
    when it explains why routing is reach-and-avoid rather than an exact call
    list. The chains run one way only: a read may be followed into the action
    lane's draft, and the action lane may not reach back for a read.

:data:`QUERY_ARGUMENT`
    The two tools whose argument is a restatement of the ask, and therefore the
    only two where *right lane, wrong query* is a thing that can be observed.
    Every other tool takes an id or a structure; a ``place_order`` with the
    wrong ``draft_id`` is a broken call, not a badly-phrased question, and
    putting it in the same column would make the shape mean two things.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.dataset.entries import DatasetEntry
from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.otel.schema import ToolName

__all__ = [
    "QUERY_ARGUMENT",
    "SANCTIONED",
    "Expectation",
    "ExpectationError",
    "expectations",
]


class ExpectationError(ValueError):
    """A dataset row that cannot be read as an expectation.

    Raised while the register is being built, never while a trajectory is being
    scored -- the rule every set in ``eval/`` follows, because a register that
    contradicts itself produces numbers that look exactly like numbers.
    """


SANCTIONED: Final[Mapping[ToolName, frozenset[ToolName]]] = {
    ToolName.SEARCH_MENU_KNOWLEDGE: frozenset(),
    ToolName.ASK_ACCOUNT_QUESTION: frozenset(),
    ToolName.GET_POINTS_BALANCE: frozenset(),
    ToolName.GET_USUAL_ORDER: frozenset({ToolName.PROPOSE_ORDER}),
    ToolName.GET_RECOMMENDATIONS: frozenset({ToolName.PROPOSE_ORDER}),
    ToolName.MATCH_MEAL_FROM_PHOTO: frozenset({ToolName.PROPOSE_ORDER}),
    ToolName.PROPOSE_ORDER: frozenset(),
    ToolName.PLACE_ORDER: frozenset(),
    ToolName.CANCEL_ORDER: frozenset(),
    ToolName.REDEEM_POINTS: frozenset({ToolName.GET_POINTS_BALANCE}),
    ToolName.UPDATE_PREFERENCES: frozenset(),
}
"""Which companion call each expected tool may make without it counting as extra.

Total over :class:`~chip_chat.otel.schema.ToolName`, and a test holds it that
way for the reason :data:`~chip_chat.eval.golden.lanes.LANE_OF` is total: a
twelfth tool added without an entry here would sanction nothing by accident
rather than by decision, and every turn expecting it would start scoring
``extra_tools``.

Four chains, and each is a turn a visitor actually has. The three reads that
produce items a draft can be built from -- the usual, a recommendation, a
photograph -- may be followed by ``propose_order``, because *"get me that"* is
one request and not two. ``redeem_points`` may be preceded by the balance,
because what a reward costs is a fact the turn needs.

Nothing runs the other way. A turn that expects ``propose_order`` already has
the items on screen, so re-fetching them is the third call this shape exists to
count. And a sanctioned companion the row *forbids* is not sanctioned:
:attr:`Expectation.sanctioned` subtracts, because a case that names a tool as
the wrong answer has said something more specific than this table does.
"""

QUERY_ARGUMENT: Final[Mapping[ToolName, str]] = {
    ToolName.SEARCH_MENU_KNOWLEDGE: "query",
    ToolName.ASK_ACCOUNT_QUESTION: "question",
}
"""The tools whose argument restates the ask, and the argument that does it.

Two of eleven. ``chip_chat.agent.surface`` is where the numbers come from:
``blob_ref``, ``draft_id``, ``order_id``, ``reward_id``, ``items`` and ``prefs``
are references and structures, ``get_points_balance``, ``get_usual_order`` and
``get_recommendations`` take nothing at all, and only these two hand the model's
own words to the system behind the lane.

So *right lane, wrong query* is scoreable on knowledge and account turns and on
no others, and :attr:`Expectation.scores_query` is how the report says which
rows those were rather than quietly counting the rest as clean.
"""


@dataclass(frozen=True, slots=True)
class Expectation:
    """One dataset row, as the thing a span tree is scored against.

    Attributes:
        entry_id: The dataset's join key. A trajectory is matched to its
            expectation by this and by nothing else, because a partial run is a
            normal thing to score and a positional match would silently score
            the wrong rows.
        lane: The lane the turn should take, :attr:`~chip_chat.eval.golden.
            lanes.Lane.NONE` included -- a turn that should reach for nothing
            is a turn routing can be wrong about.
        tool: The tool inside that lane, or ``None`` where the answer is to
            call nothing.
        forbidden: Tools this turn must not reach for. The confusable half of a
            boundary row, and the reason a wrong lane can be named rather than
            merely counted.
        message: What the visitor said. Read by the query check, and printed
            beside a failure.
        menu_terms: Published terms the row leans on. Also read by the query
            check: a knowledge question about guacamole whose search says
            nothing about guacamole asked something else.
        why: What this row is for, carried through so a failing shape arrives
            with the argument for the case attached.
    """

    entry_id: str
    lane: Lane
    tool: ToolName | None
    forbidden: frozenset[ToolName] = frozenset()
    message: str = ""
    menu_terms: tuple[str, ...] = ()
    why: str = ""

    @property
    def sanctioned(self) -> frozenset[ToolName]:
        """The companions this turn may also call. See :data:`SANCTIONED`."""
        if self.tool is None:
            return frozenset()
        return SANCTIONED[self.tool] - self.forbidden

    @property
    def scores_query(self) -> bool:
        """Whether *right lane, wrong query* is observable on this row."""
        return self.tool in QUERY_ARGUMENT

    @property
    def query_argument(self) -> str | None:
        """Which argument carries the ask, or ``None`` where none does."""
        return None if self.tool is None else QUERY_ARGUMENT.get(self.tool)


def expectations(dataset: Dataset) -> tuple[Expectation, ...]:
    """The rows of ``dataset`` that a trajectory can be scored against.

    Args:
        dataset: The built dataset. Its version is what a report quotes, which
            is the whole reason this reads a dataset rather than a manifest.

    Returns:
        One expectation per row that scores routing, in dataset order.

    Raises:
        ExpectationError: If a row names a tool that is not one of the eleven,
            or a lane its tool does not belong to.
    """
    return tuple(_expectation(entry) for entry in dataset.entries if entry.scores_routing)


def _expectation(entry: DatasetEntry) -> Expectation:
    """Read one row, refusing one that contradicts the schema it was built from."""
    tool = _tool(entry.expected_tool, entry.entry_id)
    if lane_of(tool) is not entry.expected_lane:
        raise ExpectationError(
            f"{entry.entry_id}: expects {entry.expected_lane.value} but "
            f"{entry.expected_tool or 'no tool'} is in "
            f"{lane_of(tool).value}"
        )
    return Expectation(
        entry_id=entry.entry_id,
        lane=entry.expected_lane,
        tool=tool,
        forbidden=frozenset(
            _named(value, entry.entry_id) for value in entry.forbidden_tools
        ),
        message=entry.input,
        menu_terms=entry.menu_terms,
        why=entry.why,
    )


def _tool(value: str, where: str) -> ToolName | None:
    """The expected tool, or ``None`` for a row that expects no call at all."""
    return None if not value else _named(value, where)


def _named(value: str, where: str) -> ToolName:
    """One tool name, refusing anything outside the eleven."""
    try:
        return ToolName(value)
    except ValueError as error:
        raise ExpectationError(f"{where}: {value!r} is not one of the tools") from error
