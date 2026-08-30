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

**A lane is usually all-or-nothing and once it was not.** ``chip-znk`` found
``get_recommendations`` offered on every turn and declining on every turn, on a
deployment whose personalization lane was up: the lane reads two marts and one
of them had never been published. So :attr:`Lanes.withheld` lets a wiring say
*"personalization, minus recommendations"* -- the tool is withdrawn from the
list rather than left on it declining, which is the same rule as above applied
one name at a time instead of one lane at a time.

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

from dataclasses import dataclass, replace

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
        withheld: Tools a wired lane cannot in fact answer. See
            :attr:`withheld` for why a lane is not always all-or-nothing.
    """

    knowledge: KnowledgeLane | None = None
    account: AccountLane | None = None
    personalization: PersonalizationLane | None = None
    photo: PhotoLane | None = None

    withheld: frozenset[ToolName] = frozenset()
    """Tools this deployment will not offer even though their lane is wired.

    A lane is four fields above and each of them is one object, which makes the
    unit of wiring the *lane* -- and for three of the four lanes that is exactly
    right, because a lane is one service and a service is up or it is not.
    Personalization is the exception and it is not a temporary one: its two
    tools read two different tables through one connection, so a mart that has
    not been published takes down one tool and leaves the other answering.

    That is the shape ``chip-znk`` measured on the deployment.
    ``CHIP_CHAT.MARTS.recommendations`` does not exist -- RFC-001 §04 fixes four
    serving marts and it would be a fifth, so
    :data:`chip_chat.snowflake.reads.RECOMMENDATIONS_MART` names the table and
    the decision not to create it in the same docstring -- and the consequence
    was that every turn offered ``get_recommendations`` and every call of it
    came back ``PERSONALIZATION_LANE_UNAVAILABLE``. Which is precisely the
    failure this module's own argument forbids, quoted from #64 above: *a tool
    definition the model can see and nothing can answer is worse than an absent
    one*. The trace showed a tool span with a refusal in it, which reads as a
    lane outage; the lane was up, and the table was missing.

    So the withdrawal is spelled here rather than papered over in the tool body.
    Withholding is not the same as un-wiring: personalization stays wired,
    ``get_usual_order`` goes on reading this visitor's own habit mart, and the
    only thing that changes is that the model is never shown a name it cannot
    use. What it deliberately does **not** do is decide whether the mart should
    exist -- that is bead ``cc-afo5`` and a schema decision, and a tool ticket
    that quietly published a serving table would be taking it in a file nobody
    reviewing the schema reads.

    Empty on every deployment that has nothing to withhold, which is the state
    the day ``cc-afo5`` lands: publish the mart, drop the name from
    :data:`chip_chat.api.app.WITHHELD_TOOLS`, and this is a frozenset again.
    """

    def conditional_tools(self) -> tuple[ToolName, ...]:
        """The tools this wiring adds beyond the unconditional set.

        Returns:
            The subset of :data:`CONDITIONAL_TOOLS` whose lane is present and
            which is not in :attr:`withheld`, in RFC-001 §06's order.
        """
        wired = {
            ToolName.ASK_ACCOUNT_QUESTION: self.account is not None,
            ToolName.GET_RECOMMENDATIONS: self.personalization is not None,
            ToolName.MATCH_MEAL_FROM_PHOTO: self.photo is not None,
        }
        return tuple(
            tool
            for tool in CONDITIONAL_TOOLS
            if wired[tool] and tool not in self.withheld
        )

    def offers(self, tool: ToolName) -> bool:
        """Whether this wiring will offer ``tool`` at all.

        The question :func:`~chip_chat.agent.tools.offered_tools` asks of every
        name it is about to append, including the unconditional ones: a
        withheld tool is withheld wherever it appears on the list, because a
        name the model is shown and cannot use is the same mistake whether the
        name was conditional or not.
        """
        return tool not in self.withheld

    def without(self, *tools: ToolName) -> "Lanes":
        """Return the same wiring with ``tools`` withheld.

        The sentence :attr:`withheld` exists to let a deployment say --
        *"personalization, minus recommendations"* -- written as a method
        rather than as a :func:`dataclasses.replace` at the call site, so that
        the union with whatever was already withheld happens in one place and
        cannot be forgotten.
        """
        return replace(self, withheld=self.withheld | frozenset(tools))

    def withdrawn(self) -> tuple[ToolName, ...]:
        """The tools a wired lane would have answered and this deployment hides.

        For a start-up log and for the health surface, and the reason it exists
        at all is that a silent withdrawal trades one invisible failure for
        another. *Offered and always declining* is visible in the wrong place --
        a red tool span, once per turn, that reads as an outage. *Absent and
        unexplained* is visible nowhere: an operator asking why Cilantro never
        recommends anything would find a tool list with no gap in it and a lane
        reporting ``up``. So the withdrawal is reported beside the lane that
        would otherwise have carried the tool.

        Returns:
            The withheld tools whose lane is wired, in :class:`ToolName`'s
            declaration order. A tool withheld on a deployment that never wired
            its lane is not here: nothing was taken away.
        """
        wired_lane_for = {
            ToolName.ASK_ACCOUNT_QUESTION: self.account is not None,
            ToolName.GET_RECOMMENDATIONS: self.personalization is not None,
            ToolName.MATCH_MEAL_FROM_PHOTO: self.photo is not None,
        }
        return tuple(
            tool
            for tool in ToolName
            if tool in self.withheld and wired_lane_for.get(tool, True)
        )

    def describe(self) -> dict[str, bool]:
        """Which lanes are wired, for a startup log or a health surface.

        Not for the model: what the model is told is the tool list, because a
        lane it cannot name is a lane it cannot reason about.

        Deliberately still four keys and still four booleans, even though
        :attr:`withheld` is now a fifth fact about the wiring.
        :class:`chip_chat.eval.wiring.Wiring` builds itself with
        ``cls(**lanes.describe())``, so this mapping is a structural contract
        rather than a log line, and the withdrawal is reported by
        :meth:`withdrawn` beside it rather than folded in here.
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
