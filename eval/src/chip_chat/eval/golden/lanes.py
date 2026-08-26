"""The five lanes, and which tool belongs to which.

The system design's opening table -- *five lanes, five paths* -- is the whole
architecture, and tool-selection accuracy is the one number it exists to get
right. So the mapping from a tool to its lane has to be somewhere a score can
read it, and it has to be total: a tool absent from :data:`LANE_OF` would score
as no lane at all and quietly stop counting.

:func:`lane_of` is the join. A case records the lane it expects *and* the tool it
expects, and :mod:`chip_chat.eval.golden.cases` refuses a case where those two
disagree -- which means a per-lane pass rate cannot drift from the tool it was
computed over.

Six tools share a lane with another, and those are the boundaries worth having
cases on: ``get_points_balance`` beside ``ask_account_question`` in the account
lane, ``get_usual_order`` beside ``get_recommendations`` in personalization, and
the five writes and near-writes of the action lane. A case sitting on a boundary
two tools share tells you something a case only one tool could answer cannot.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from chip_chat.otel.schema import ToolName

__all__ = ["LANE_OF", "TOOLS_IN", "Lane", "lane_of"]


class Lane(StrEnum):
    """One of the five paths a visitor message can take, or none of them.

    Attributes:
        KNOWLEDGE: Hybrid RAG over the published menu.
        ACCOUNT: NL to SQL over the visitor's own rows.
        PERSONALIZATION: The precomputed gold marts.
        ACTION: The confirmation card and the ops API behind it.
        VISION: The photo pipeline, which feeds the action lane.
        NONE: The turn should reach no tool at all. Not a failure state: a
            question the assistant should decline, or answer from the
            conversation it is already in, is a turn where calling any lane
            would be the error.
    """

    KNOWLEDGE = "knowledge"
    ACCOUNT = "account"
    PERSONALIZATION = "personalization"
    ACTION = "action"
    VISION = "vision"
    NONE = "none"


LANE_OF: Final[Mapping[ToolName, Lane]] = {
    ToolName.SEARCH_MENU_KNOWLEDGE: Lane.KNOWLEDGE,
    ToolName.ASK_ACCOUNT_QUESTION: Lane.ACCOUNT,
    ToolName.GET_POINTS_BALANCE: Lane.ACCOUNT,
    ToolName.GET_USUAL_ORDER: Lane.PERSONALIZATION,
    ToolName.GET_RECOMMENDATIONS: Lane.PERSONALIZATION,
    ToolName.MATCH_MEAL_FROM_PHOTO: Lane.VISION,
    ToolName.PROPOSE_ORDER: Lane.ACTION,
    ToolName.PLACE_ORDER: Lane.ACTION,
    ToolName.CANCEL_ORDER: Lane.ACTION,
    ToolName.REDEEM_POINTS: Lane.ACTION,
    ToolName.UPDATE_PREFERENCES: Lane.ACTION,
}
"""Every one of the eleven tools, and its lane.

Total over :class:`~chip_chat.otel.schema.ToolName` by construction, and a test
holds it that way: a twelfth tool added without a lane would otherwise make the
per-lane rates silently incomplete rather than obviously wrong.
"""


TOOLS_IN: Final[Mapping[Lane, tuple[ToolName, ...]]] = {
    lane: tuple(tool for tool, owner in LANE_OF.items() if owner is lane) for lane in Lane
}
"""The tools of each lane, in :data:`LANE_OF` order. Empty for :attr:`Lane.NONE`."""


def lane_of(tool: ToolName | None) -> Lane:
    """Which lane a tool belongs to.

    Args:
        tool: The tool, or ``None`` for a turn that should call nothing.

    Returns:
        Its lane, or :attr:`Lane.NONE`.
    """
    return Lane.NONE if tool is None else LANE_OF[tool]
