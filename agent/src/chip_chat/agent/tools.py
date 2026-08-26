"""Six of the eleven tools: five against hardcoded data, one against the real lane.

The eleven tools of RFC-001 section 06 are the agent's whole action surface. Five
are implemented here against the week-one slice's hardcoded data -- one per lane
the issue names, the reorder the Phase 7 demo criterion turns on, and the draft
that has to exist before a write can be confirmed -- and the sixth is not
hardcoded at all:

============================ ===================================================
Tool                         What it does here, and what it will do
============================ ===================================================
``search_menu_knowledge``    Word overlap against three items. Becomes hybrid
                             retrieval over AI Search (#45); the ``retriever.search``
                             span it nests is already the right span.
``get_points_balance``       Reads one hardcoded account. Becomes a gold-mart
                             read (#38). No child span, then or now.
``get_usual_order``          Reads the same account's usual order and returns
                             the item ids it is made of, so "reorder my usual"
                             is a lookup rather than a guess at prose. Becomes
                             a gold-mart read alongside the balance (#38).
``propose_order``            Mints a draft. Becomes the ops API's draft endpoint
                             with §7.1's twelve rules behind it (#60).
``place_order``              Places a *confirmed* draft, and nests
                             ``ops.place_order``. The confirmation rule is
                             already the real one -- see :mod:`chip_chat.agent.orders`.
``match_meal_from_photo``    The real photo lane, offered only when one is
                             wired. Nests ``vision.describe`` and
                             ``matcher.resolve`` under the *one* tool span --
                             see :mod:`chip_chat.vision.lane`.
============================ ===================================================

The sixth is conditional, and deliberately so. A tool definition the model can
see and nothing can answer is worse than an absent one: the model will call it,
the call will fail, and the trace will show a tool span with a refusal in it
that reads as a lane outage rather than as a deployment nobody finished.
:func:`offered_tools` and :func:`offered_schemas` therefore take the lane and
return what is actually answerable, and the loop offers the model that -- both
as tool definitions and in the runtime context that names what is registered.

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
from typing import Any, Final

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
from chip_chat.vision.describe import DescribeError
from chip_chat.vision.lane import PhotoLane, PhotoMatch
from chip_chat.vision.matcher import Outcome, Resolution
from chip_chat.vision.store import PHOTO_REF_ARGUMENT, BlobRef

__all__ = [
    "PHOTO_UNAVAILABLE_MESSAGE",
    "TOOLS",
    "TOOL_SCHEMAS",
    "dispatch",
    "offered_schemas",
    "offered_tools",
]

_MENU_INDEX = "hardcoded-menu"
"""What ``retriever.search`` records as the index searched. Deliberately not a
plausible index name: a trace should say out loud that this is not AI Search."""

TOOLS: tuple[ToolName, ...] = (
    ToolName.SEARCH_MENU_KNOWLEDGE,
    ToolName.GET_POINTS_BALANCE,
    ToolName.GET_USUAL_ORDER,
    ToolName.PROPOSE_ORDER,
    ToolName.PLACE_ORDER,
)
"""The tools this slice offers unconditionally. Five of the eleven remain.

``match_meal_from_photo`` is the sixth and is not here, because whether it can
be answered depends on whether a lane was wired. :func:`offered_tools` is what a
call site should ask."""


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

PHOTO_UNAVAILABLE_MESSAGE: Final = (
    "I couldn't read that photo just now. Tell me what you're after and I'll "
    "sort it out from there."
)
"""What the model is told when the photo lane declines.

A lane may fail; the conversation may not fail with it (RFC-001 section 10). So
this comes back as a tool *result* the model can read and act on, the span is
marked failed, and the visitor gets asked a question rather than an error."""


def offered_tools(*, lane: PhotoLane | None = None) -> tuple[ToolName, ...]:
    """The tools that can actually be answered, given what is wired.

    Args:
        lane: The photo lane, or ``None`` where none is configured.

    Returns:
        :data:`TOOLS`, plus ``match_meal_from_photo`` when ``lane`` is given.
    """
    if lane is None:
        return TOOLS
    return (*TOOLS, ToolName.MATCH_MEAL_FROM_PHOTO)


def offered_schemas(*, lane: PhotoLane | None = None) -> tuple[Mapping[str, Any], ...]:
    """The tool definitions to offer the model, aligned with :func:`offered_tools`.

    Derived from the surface for the same reason :data:`TOOL_SCHEMAS` is: the
    schema the model is shown and the schema its arguments are checked against
    have to be the same object.
    """
    if lane is None:
        return TOOL_SCHEMAS
    return (
        *TOOL_SCHEMAS,
        _narrowed_to_the_hardcoded_menu(
            spec(ToolName.MATCH_MEAL_FROM_PHOTO).as_tool_definition()
        ),
    )


def dispatch(
    invocation: ToolInvocation,
    *,
    session_id: str,
    desk: OrderDesk,
    lane: PhotoLane | None = None,
) -> Mapping[str, Any]:
    """Run one tool call and return what the model should see.

    Args:
        invocation: The call the model asked for.
        session_id: The bound conversation. Supplied by the request handler and
            never read out of ``invocation.arguments``.
        desk: The order desk holding this session's drafts.
        lane: The photo lane, where one is wired. ``None`` leaves
            ``match_meal_from_photo`` unimplemented, which is what
            :func:`offered_tools` has already told the model.

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
            tool,
            arguments,
            session_id=session_id,
            desk=desk,
            lane=lane,
            recorder=recorder,
        )
        recorder.record_result(result)
        return result


def _dispatch_inside_span(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: OrderDesk,
    lane: PhotoLane | None,
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
    if tool not in offered_tools(lane=lane):
        # Checked before the arguments are, because "that tool arrives in a
        # later phase" is a more useful thing to tell a model than "your
        # blob_ref is missing" for a tool that would not have run either way.
        return _not_implemented(tool, lane=lane)
    try:
        bound = spec(tool).bind(arguments)
    except ToolCallRejectedError as rejection:
        recorder.record_failure(rejection)
        return {"rejected": "ARGUMENTS_REJECTED", "detail": rejection.reason}
    try:
        result = _run(tool, bound.arguments, session_id=session_id, desk=desk, lane=lane)
    except OrderRejectedError as rejection:
        recorder.record_failure(rejection.message)
        return dict(rejection.as_result())
    except DescribeError as declined:
        # The photo lane declining. Section 10 again: the span says so and the
        # model is handed something it can say to the visitor.
        recorder.record_failure(declined)
        return {
            "declined": type(declined).__name__,
            "detail": PHOTO_UNAVAILABLE_MESSAGE,
        }
    if isinstance(result, PhotoMatch):
        # The lane's model calls, rolled onto the span that contains them: "what
        # does the photo lane cost per call" is then one attribute rather than a
        # tree walk. See `ChipChatAttributes.TOKENS_TOTAL`. The model never sees
        # a token count -- the rollup goes on the span, and `_photo_result`
        # builds what the model is given.
        if result.usage is not None:
            recorder.record_token_rollup(result.usage)
        return _photo_result(result)
    return result


def _run(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: OrderDesk,
    lane: PhotoLane | None = None,
) -> Mapping[str, Any] | PhotoMatch:
    """Body of one tool, inside its span.

    Returns a :class:`~chip_chat.vision.lane.PhotoMatch` for the photo tool and
    a plain mapping for the rest; :func:`_dispatch_inside_span` reads the tokens
    off the former before flattening it, because the span rollup has to happen
    where the span is and the model must never see a token count.

    Raises:
        OrderRejectedError: From the two order tools, caught by the caller.
        DescribeError: From the photo lane, caught by the caller.
    """
    match tool:
        case ToolName.SEARCH_MENU_KNOWLEDGE:
            return _search_menu_knowledge(str(arguments.get("query", "")))
        case ToolName.GET_POINTS_BALANCE:
            return _get_points_balance()
        case ToolName.GET_USUAL_ORDER:
            return _get_usual_order()
        case ToolName.PROPOSE_ORDER:
            return _propose_order(arguments.get("items"), session_id, desk)
        case ToolName.PLACE_ORDER:
            return _place_order(str(arguments.get("draft_id", "")), session_id, desk)
        case ToolName.MATCH_MEAL_FROM_PHOTO if lane is not None:
            return _match_meal_from_photo(
                str(arguments.get(PHOTO_REF_ARGUMENT, "")), lane
            )
        case _:  # pragma: no cover - dispatch refuses these before _run is reached
            return _not_implemented(tool, lane=lane)


def _not_implemented(
    tool: ToolName, *, lane: PhotoLane | None = None
) -> Mapping[str, Any]:
    """A real tool of the eleven that this slice has not built yet.

    A typed refusal the model can read and act on, not an exception: it needs to
    tell the visitor that the lane is not available rather than fail the turn.
    """
    return {
        "rejected": "TOOL_NOT_IMPLEMENTED",
        "detail": (
            f"{tool.value} arrives in a later phase. Tools available now: "
            f"{', '.join(name.value for name in offered_tools(lane=lane))}."
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


def _get_usual_order() -> Mapping[str, Any]:
    """The reorder lane. No child span: the tool span is the whole of it.

    Returns the item ids and not only the sentence, because "reorder my usual"
    has to become a draft over real rows. A model handed only
    ``"a chicken burrito bowl with a side of guac"`` would have to turn prose
    back into identifiers, and the one thing this architecture is arranged to
    prevent is a menu item arrived at by inference.

    The surface promises a confidence with the habit, and says the confidence is
    real and sometimes low. There is no gold mart behind this yet, so what is
    reported is the *absence* of one rather than a number invented to fill the
    field: an account fixture is not evidence of a habit, and a confidence
    fabricated here would be exactly the guess-presented-as-a-habit the tool
    description warns against.
    """
    items = [
        {
            "item_id": item_id,
            "name": MENU[item_id].name,
            "unit_price": str(MENU[item_id].unit_price),
        }
        for item_id in ACCOUNT.favourite_items
        if item_id in MENU
    ]
    return {
        "usual_order": ACCOUNT.usual_order,
        "items": items,
        "confidence": None,
        "how_it_was_worked_out": (
            "Read off the hardcoded account fixture, not computed from order "
            "history -- the gold mart behind this arrives with #38. Do not "
            "describe it to the visitor as something learned from their orders."
        ),
        "next_step": (
            "Call propose_order with these item_ids, adjusted for anything the "
            "visitor asked to change -- 'extra guac' is a second unit of the "
            "guacamole line, not a new item. Then show them the card."
        ),
    }


def _match_meal_from_photo(
    blob_ref: str, lane: PhotoLane
) -> Mapping[str, Any] | PhotoMatch:
    """The photo lane. Nests ``vision.describe`` and ``matcher.resolve``.

    Both of them, under the *one* ``tool.match_meal_from_photo`` this call
    already opened -- which is issue #64's second acceptance criterion: one
    trace holding the image, the structured description and the resolved SKUs
    together. :mod:`chip_chat.vision.lane` is where that composition lives, and
    why this is one call rather than two.

    Returns:
        The :class:`~chip_chat.vision.lane.PhotoMatch`, which the caller
        flattens after reading its tokens; or a refusal mapping where the model
        supplied nothing usable as a reference.

    Raises:
        DescribeError: The lane declining, caught by the caller.
        CatalogueDriftError: Stage 4 and stage 5 disagree about the catalogue.
            Deliberately not caught: a build fault is not a declining lane, and
            a turn that resolved against a vocabulary from a different catalogue
            build could put a real SKU on a fabricated order.
    """
    try:
        ref = BlobRef.parse(blob_ref)
    except ValueError as error:
        # `BlobRef.parse` is where a reference arriving from outside this
        # process is checked, and a model that invented one has to be refused
        # here rather than reach a container client. A refusal it can read, so
        # it asks the visitor for a photo instead of retrying the invention.
        return {
            "rejected": "NO_PHOTO",
            "detail": (
                f"{error} Ask the visitor to upload a photo; never compose a "
                "reference yourself."
            ),
        }
    return lane.match(ref)


def _photo_result(match: PhotoMatch) -> Mapping[str, Any]:
    """Flatten a :class:`~chip_chat.vision.lane.PhotoMatch` for the model.

    What is deliberately absent is ``Description.notes``. It is display-only
    prose and nothing downstream may parse it -- and a model asked to answer
    from it is the most enthusiastic parser there is. The model gets the
    catalogue rows and the questions; the sentence goes to the renderer.
    """
    resolution = match.resolution
    body: dict[str, Any] = {
        "outcome": resolution.outcome.value,
        "meals_visible": resolution.meals_visible,
    }
    if resolution.outcome is Outcome.RESOLVED:
        body["items"] = _photo_items(resolution)
        total = resolution.total()
        # A total missing one line is a wrong number that looks like a right
        # one, so `Resolution.total` returns None rather than a partial sum and
        # this passes that through instead of quoting a figure.
        body["total"] = None if total is None else str(total)
        body["next_step"] = (
            "Call propose_order with these item_ids so the visitor gets a card "
            "to confirm. Do not place anything yet."
        )
    if resolution.clarifications:
        body["clarifications"] = [
            {
                "slot": clarification.slot,
                "reason": clarification.reason.value,
                "term": clarification.term,
            }
            for clarification in resolution.clarifications
        ]
        body["next_step"] = (
            "Ask the visitor about each slot above. PRD V5: ask, do not guess."
        )
    if resolution.discarded:
        body["not_confident_about"] = [
            {"slot": dropped.slot, "term": dropped.term}
            for dropped in resolution.discarded
        ]
    return body


def _photo_items(resolution: Resolution) -> list[Mapping[str, Any]]:
    """The resolved catalogue rows, as the model should see them."""
    return [
        {
            "item_id": item.item_id,
            "name": item.name,
            "slot": item.slot,
            "unit_price": None if item.unit_price is None else str(item.unit_price),
            "available": item.available,
        }
        for item in resolution.items()
    ]


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
