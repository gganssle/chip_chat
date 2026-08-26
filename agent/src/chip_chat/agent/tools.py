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
from chip_chat.otel import (
    ConfirmationState,
    Document,
    OpsAction,
    ToolName,
    ops_write,
    retriever_search,
    tool_call,
)

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

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": ToolName.SEARCH_MENU_KNOWLEDGE.value,
            "description": (
                "Search the menu for items, descriptions, prices, calories and "
                "allergens. Use it for any question about what is on the menu or "
                "what is in a dish. Returns passages to answer from; do not "
                "answer menu questions from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The visitor's question, in their words.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.GET_POINTS_BALANCE.value,
            "description": (
                "Read the signed-in visitor's rewards points balance and account "
                "summary. Takes no arguments: the account is bound to the "
                "session, never named by the caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.PROPOSE_ORDER.value,
            "description": (
                "Draft an order for the visitor to confirm. This does not place "
                "anything. Returns a draft_id and a card showing the items and "
                "the total; tell the visitor what is on the card and ask them to "
                "press Confirm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "The lines to draft.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {
                                    "type": "string",
                                    "enum": sorted(MENU),
                                },
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                            "required": ["item_id"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.PLACE_ORDER.value,
            "description": (
                "Place a draft the visitor has already confirmed by pressing "
                "Confirm. It is refused unless they have. Never call it to ask "
                "for confirmation -- pressing the button is the confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "The draft_id propose_order returned.",
                    }
                },
                "required": ["draft_id"],
                "additionalProperties": False,
            },
        },
    },
)
"""The tool definitions the model is offered, and which ``llm.completion``
records so Arize's tool-selection evals can compare choice against offer."""


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
        try:
            result = _run(tool, arguments, session_id=session_id, desk=desk)
        except OrderRejectedError as rejection:
            recorder.record_failure(rejection.message)
            result = dict(rejection.as_result())
        recorder.record_result(result)
        return result


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
        case _:
            return {
                "rejected": "TOOL_NOT_IMPLEMENTED",
                "detail": (
                    f"{tool.value} arrives in a later phase. Tools available "
                    f"now: {', '.join(name.value for name in TOOLS)}."
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
