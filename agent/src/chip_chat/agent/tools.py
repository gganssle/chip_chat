"""Four of the eleven tools, implemented against hardcoded data.

The eleven tools of RFC-001 section 06 are the agent's whole action surface. The
week-one slice implements four of them -- one per lane the issue names, plus the
draft that has to exist before a write can be confirmed:

============================ ===================================================
Tool                         What it does here, and what it will do
============================ ===================================================
``search_menu_knowledge``    Word overlap against three items. Becomes hybrid
                             retrieval over AI Search (#45); the ``retriever.search``
                             span it nests is already the right span.
``get_points_balance``       Reads one hardcoded account. Becomes a gold-mart
                             read (#38). No child span, then or now.
``propose_order``            Mints a draft. Becomes the ops API's draft endpoint
                             with §7.1's twelve rules behind it (#60).
``place_order``              Places a *confirmed* draft, and nests
                             ``ops.place_order``. The confirmation rule is
                             already the real one -- see :mod:`chip_chat.agent.orders`.
============================ ===================================================

Two things here are not placeholders and should survive the data becoming real.

**No tool takes a visitor identifier.** RFC-001 section 05: identity is bound to
the session by the app, and the absence of the parameter *is* the enforcement
mechanism. :func:`dispatch` takes ``session_id`` as an argument of its own,
never from :attr:`~chip_chat.agent.model.ToolInvocation.arguments`, so a model
that invented one would find it ignored rather than honoured.

**Every call opens exactly one ``tool.<tool_name>`` span**, named from
:class:`~chip_chat.otel.schema.ToolName` rather than from the string the model
emitted. A model that asks for a tool nobody wrote gets a typed refusal and no
span at all, because an off-schema span name is the one failure mode
``otel/README.md`` says breaks every eval built on top of it.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from chip_chat.agent.hardcoded import ACCOUNT, MENU, SIMULATION_NOTICE, search_menu
from chip_chat.agent.model import ToolInvocation, UnknownToolError
from chip_chat.agent.orders import OrderDesk, OrderRejectedError
from chip_chat.agent.surface import ToolCallRejectedError, spec
from chip_chat.otel import (
    ConfirmationState,
    Document,
    OpsAction,
    ToolName,
    ops_write,
    retriever_search,
    tool_call,
)
from chip_chat.otel.spans import ToolRecorder

__all__ = ["TOOLS", "TOOL_SCHEMAS", "dispatch"]

_MENU_INDEX = "hardcoded-menu"
"""What ``retriever.search`` records as the index searched. Deliberately not a
plausible index name: a trace should say out loud that this is not AI Search."""

TOOLS: tuple[ToolName, ...] = (
    ToolName.SEARCH_MENU_KNOWLEDGE,
    ToolName.GET_POINTS_BALANCE,
    ToolName.PROPOSE_ORDER,
    ToolName.PLACE_ORDER,
)
"""The tools this slice offers. The other seven are later phases."""


def _narrowed_to_the_hardcoded_menu(definition: dict[str, Any]) -> dict[str, Any]:
    """Pin ``propose_order``'s item ids to the three items that exist here.

    RFC-001 D3 says the model may describe food and may never name a SKU. The
    real enforcement is the deterministic matcher (#54) and the ops API's
    catalogue check (#63), neither of which exists yet. What does exist is a
    three-item menu and a schema, so the vocabulary goes in the schema: an item
    id that is not on this menu is not expressible.

    The narrowing is the *slice's* and not the surface's, which is why it lives
    here rather than in :mod:`chip_chat.agent.surface`. When the real catalogue
    arrives, the enum is generated from it and this is the function that does it.
    """
    if definition["function"]["name"] != ToolName.PROPOSE_ORDER.value:
        return definition
    line = definition["function"]["parameters"]["properties"]["items"]["items"]
    line["properties"]["item_id"] = {
        **line["properties"]["item_id"],
        "enum": sorted(MENU),
    }
    return definition


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = tuple(
    _narrowed_to_the_hardcoded_menu(spec(name).as_tool_definition()) for name in TOOLS
)
"""The tool definitions the model is offered, and which ``llm.completion``
records so Arize's tool-selection evals can compare choice against offer.

Derived from :data:`chip_chat.agent.surface.TOOL_SPECS` rather than written out
a second time here, so the schema the model is shown and the schema its
arguments are checked against are the same object. Two copies would eventually
disagree, and the disagreement would be invisible in the worst way: the model
offered a field the validator refuses, and every call looking like a model
error."""


def dispatch(
    invocation: ToolInvocation, *, session_id: str, desk: OrderDesk
) -> Mapping[str, Any]:
    """Run one tool call and return what the model should see.

    Args:
        invocation: The call the model asked for.
        session_id: The bound conversation. Supplied by the request handler and
            never read out of ``invocation.arguments``.
        desk: The order desk holding this session's drafts.

    Returns:
        A JSON-serialisable result, which is both the tool message sent back to
        the model and the value recorded on the tool span. A refusal is a
        result, not an exception: the model has to be able to read it and ask
        the visitor something.
    """
    try:
        tool = invocation.tool
    except UnknownToolError as error:
        # No span. An invented name must not become one -- see the module
        # docstring -- and there is nothing here worth a node in the tree.
        return {"rejected": "UNKNOWN_TOOL", "detail": str(error)}

    arguments = invocation.arguments
    with tool_call(tool, arguments=arguments) as recorder:
        result = _dispatch_inside_span(
            tool, arguments, session_id=session_id, desk=desk, recorder=recorder
        )
        recorder.record_result(result)
        return result


def _dispatch_inside_span(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: OrderDesk,
    recorder: ToolRecorder,
) -> Mapping[str, Any]:
    """Validate the arguments, then run the tool. Refusals are results.

    The validation is the surface's, not this module's: nothing reaches a tool
    body until :mod:`chip_chat.agent.surface` has agreed that every argument on
    the call is one the tool declares. That is where "no tool takes a visitor
    identifier" stops being a claim about a schema document and becomes a
    property of the call path -- a model that emits ``demo_id`` gets a refusal
    it can read, and no tool body is ever offered the extra field.
    """
    if tool not in TOOLS:
        # Checked before the arguments are, because "that tool arrives in a
        # later phase" is a more useful thing to tell a model than "your
        # blob_ref is missing" for a tool that would not have run either way.
        return _not_implemented(tool)
    try:
        bound = spec(tool).bind(arguments)
    except ToolCallRejectedError as rejection:
        recorder.record_failure(rejection)
        return {"rejected": "ARGUMENTS_REJECTED", "detail": rejection.reason}
    try:
        return _run(tool, bound.arguments, session_id=session_id, desk=desk)
    except OrderRejectedError as rejection:
        recorder.record_failure(rejection.message)
        return dict(rejection.as_result())


def _run(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: OrderDesk,
) -> Mapping[str, Any]:
    """Body of one tool, inside its span.

    Raises:
        OrderRejectedError: From the two order tools, caught by :func:`dispatch`.
    """
    match tool:
        case ToolName.SEARCH_MENU_KNOWLEDGE:
            return _search_menu_knowledge(str(arguments.get("query", "")))
        case ToolName.GET_POINTS_BALANCE:
            return _get_points_balance()
        case ToolName.PROPOSE_ORDER:
            return _propose_order(arguments.get("items"), session_id, desk)
        case ToolName.PLACE_ORDER:
            return _place_order(str(arguments.get("draft_id", "")), session_id, desk)
        case _:  # pragma: no cover - dispatch refuses these before _run is reached
            return _not_implemented(tool)


def _not_implemented(tool: ToolName) -> Mapping[str, Any]:
    """A real tool of the eleven that this slice has not built yet.

    A typed refusal the model can read and act on, not an exception: it needs to
    tell the visitor that the lane is not available rather than fail the turn.
    """
    return {
        "rejected": "TOOL_NOT_IMPLEMENTED",
        "detail": (
            f"{tool.value} arrives in a later phase. Tools available now: "
            f"{', '.join(name.value for name in TOOLS)}."
        ),
    }


def _search_menu_knowledge(query: str) -> Mapping[str, Any]:
    """Menu knowledge. Nests ``retriever.search``, as the schema requires."""
    hits = search_menu(query)
    documents = [
        Document(
            id=f"menu-{item.item_id}",
            content=item.summary(),
            score=score,
            metadata={
                "item_id": item.item_id,
                "source": "hardcoded week-one slice, not the harvested menu",
            },
        )
        for item, score in hits
    ]
    with retriever_search(query=query, index=_MENU_INDEX) as search:
        search.record_documents(documents)
    if not documents:
        return {
            "passages": [],
            "note": (
                "Nothing on the menu matches. The menu is three items: "
                f"{', '.join(item.name for item in MENU.values())}. Say so "
                "rather than inventing an item."
            ),
        }
    return {
        "passages": [
            {"id": document.id, "text": document.content, "score": document.score}
            for document in documents
        ]
    }


def _get_points_balance() -> Mapping[str, Any]:
    """The account lane. No child span: the tool span is the whole of it."""
    return {
        "points_balance": ACCOUNT.points_balance,
        "member_since": ACCOUNT.member_since,
        "home_store": ACCOUNT.home_store.name,
        "usual_order": ACCOUNT.usual_order,
    }


def _propose_order(items: Any, session_id: str, desk: OrderDesk) -> Mapping[str, Any]:
    """Mint a draft. A draft is not a write, so it nests no ``ops`` span."""
    lines: Sequence[Mapping[str, Any]] = items if isinstance(items, list) else []
    draft = desk.propose(session_id, lines)
    return {
        "draft": draft.as_card(),
        "next_step": (
            "Show the visitor what is on the card and ask them to press Confirm. "
            "Do not call place_order until they have."
        ),
    }


def _place_order(draft_id: str, session_id: str, desk: OrderDesk) -> Mapping[str, Any]:
    """Place a confirmed draft. Nests ``ops.place_order``, which is the write.

    The ops span is opened even when the write is refused, because a refusal is
    exactly the thing an eval needs to see: ``confirmation_state=rejected`` on
    this span is a launch-gate violation, and a turn that quietly emitted no
    span would hide it.
    """
    with ops_write(OpsAction.PLACE_ORDER, reference_id=draft_id or "(none)") as ops:
        try:
            receipt = desk.place(session_id, draft_id)
        except OrderRejectedError:
            ops.record_confirmation(ConfirmationState.REJECTED)
            raise
        ops.record_confirmation(ConfirmationState.CONFIRMED)
        ops.record_receipt(receipt.as_dict())
    return {"receipt": receipt.as_dict(), "notice": SIMULATION_NOTICE}
