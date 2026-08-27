"""What is wired on this deployment, and therefore what the model is offered.

The agent owns eleven tool *bodies* and none of the services behind them. Four
of the five lanes of the system design live in other packages -- knowledge in
``search/``, account and personalization in ``snowflake/``, vision in
``vision/`` -- and each arrives here as an object rather than as a client this
module builds. That is not a testing convenience: a lane that could construct
its own client would be a second place where a deployment name, an endpoint or a
credential is resolved, which is the argument
:mod:`chip_chat.vision.describe` already makes for the photo lane.

:class:`Lanes` is the four of them in one value. It replaced a single ``lane=``
keyword that meant the photo lane, because a fifth keyword on
:func:`~chip_chat.agent.tools.dispatch` would have been the fifth place to
remember to thread one through.

**An absent lane is a smaller tool list, never a broken tool.** #64's argument
about ``match_meal_from_photo`` is general and applies to all four:

    A tool definition the model can see and nothing can answer is worse than an
    absent one: the model will call it, the call will fail, and the trace will
    show a tool span with a refusal in it that reads as a lane outage rather
    than as a deployment nobody finished.

So :func:`~chip_chat.agent.tools.offered_tools` asks this object what is
answerable, the loop offers the model exactly that, and
:func:`~chip_chat.agent.loop.runtime_context` names the same list in words.

**Three tools keep a hardcoded stand-in and two do not**, and the line between
them is whether an invented answer would be a lie:

``search_menu_knowledge``, ``get_points_balance``, ``get_usual_order``
    Answerable from :mod:`chip_chat.agent.hardcoded` when no lane is wired.
    Each says in its own result what it is reading -- a three-item menu, an
    account fixture, a habit that was not computed from any history -- so the
    week-one slice is a demo that admits what it is.

``ask_account_question``, ``get_recommendations``
    Offered only when their lane is wired. A hardcoded NL→SQL answer is exactly
    the plausible number PRD A4 forbids, and a hardcoded rationale is a sentence
    attributed to a model that never ran. There is no honest stand-in for
    either, so there is no stand-in.
"""

from dataclasses import dataclass

from chip_chat.otel import ToolName
from chip_chat.search.lane import KnowledgeLane
from chip_chat.snowflake.lane import AccountLane, PersonalizationLane
from chip_chat.vision.lane import PhotoLane

__all__ = ["CONDITIONAL_TOOLS", "NO_LANES", "Lanes"]


@dataclass(frozen=True, slots=True)
class Lanes:
    """The backing services this deployment actually has.

    Every field defaults to ``None``, so ``Lanes()`` is the week-one slice: the
    hardcoded menu, the hardcoded account, and no tool offered that nothing can
    answer.

    Attributes:
        knowledge: Hybrid retrieval over the harvested corpus (#49). ``None``
            leaves ``search_menu_knowledge`` on the hardcoded three-item menu.
        account: Cortex Analyst and the points read (#45, #43, #44). ``None``
            withdraws ``ask_account_question`` entirely and leaves
            ``get_points_balance`` on the account fixture.
        personalization: The gold marts (#38, #37). ``None`` withdraws
            ``get_recommendations`` entirely and leaves ``get_usual_order`` on
            the account fixture.
        photo: Stage 4 and stage 5, composed (#64). ``None`` withdraws
            ``match_meal_from_photo``.
    """

    knowledge: KnowledgeLane | None = None
    account: AccountLane | None = None
    personalization: PersonalizationLane | None = None
    photo: PhotoLane | None = None

    def conditional_tools(self) -> tuple[ToolName, ...]:
        """The tools this wiring adds beyond the unconditional set.

        Returns:
            The subset of :data:`CONDITIONAL_TOOLS` whose lane is present, in
            RFC-001 §06's order.
        """
        wired = {
            ToolName.ASK_ACCOUNT_QUESTION: self.account is not None,
            ToolName.GET_RECOMMENDATIONS: self.personalization is not None,
            ToolName.MATCH_MEAL_FROM_PHOTO: self.photo is not None,
        }
        return tuple(tool for tool in CONDITIONAL_TOOLS if wired[tool])

    def describe(self) -> dict[str, bool]:
        """Which lanes are wired, for a startup log or a health surface.

        Not for the model: what the model is told is the tool list, because a
        lane it cannot name is a lane it cannot reason about.
        """
        return {
            "knowledge": self.knowledge is not None,
            "account": self.account is not None,
            "personalization": self.personalization is not None,
            "photo": self.photo is not None,
        }


CONDITIONAL_TOOLS: tuple[ToolName, ...] = (
    ToolName.ASK_ACCOUNT_QUESTION,
    ToolName.GET_RECOMMENDATIONS,
    ToolName.MATCH_MEAL_FROM_PHOTO,
)
"""The three tools that are offered only when something can answer them.

In RFC-001 §06's order, because :func:`~chip_chat.agent.tools.offered_tools`
appends them to the unconditional list and the order a model sees its tools in
is one of the few things about a registration that is not free to vary.
"""

NO_LANES: Lanes = Lanes()
"""Nothing wired: the week-one slice, and the default everywhere.

A named constant rather than ``Lanes()`` written into a dozen signatures,
because a frozen dataclass constructed in a default argument is a call every
linter is right to object to and a value every reader has to check for
mutability. There is exactly one of these and it cannot be changed.
"""
