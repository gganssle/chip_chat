"""The tool bodies: six reads, one draft, one write, and what backs each of them.

The eleven tools of RFC-001 §06 are the agent's whole action surface;
:mod:`chip_chat.agent.surface` is the *definition* and this is where the built
ones actually run. Since #61 all six read tools are here, each against its own
backing service, and each with a stand-in only where a stand-in can be honest:

============================ ===================================================
Tool                         What answers it
============================ ===================================================
``search_menu_knowledge``    :class:`~chip_chat.search.lane.KnowledgeLane` --
                             hybrid retrieval with citations (#49) -- when one
                             is wired. Word overlap against three hardcoded
                             items when none is. Nests ``retriever.search``
                             either way.
``ask_account_question``     :class:`~chip_chat.snowflake.lane.AccountLane`:
                             Cortex Analyst writes the SQL, ``analyst.decide``
                             judges it, the bound connection runs it. Nests
                             ``db.cortex_analyst``. **No stand-in.**
``get_points_balance``       The same lane, one fixed query. The hardcoded
                             account when none is wired. No child span.
``get_usual_order``          :class:`~chip_chat.snowflake.lane.PersonalizationLane`
                             over ``MARTS.usual_order``, with the mart's own
                             confidence and ``derived_at``. The hardcoded
                             account when none is wired. No child span.
``get_recommendations``      The same lane, over the ranked mart, with the
                             rationale each row was scored with. **No
                             stand-in.**
``match_meal_from_photo``    :class:`~chip_chat.vision.lane.PhotoLane`. Nests
                             ``vision.describe`` and ``matcher.resolve`` under
                             the *one* tool span.
============================ ===================================================

Two of them are offered only when their lane is wired, and
:mod:`chip_chat.agent.lanes` carries the argument for why that is honest rather
than incomplete: a hardcoded NL→SQL answer is the plausible number PRD A4
forbids, and a hardcoded rationale is a sentence attributed to a model that
never ran. :func:`offered_tools` is what a call site should ask.

Three things here are not placeholders and should survive every data source
becoming real.

**No tool takes a visitor identifier.** RFC-001 §05: identity is bound to the
session by the app, and the absence of the parameter *is* the enforcement
mechanism. :func:`dispatch` takes ``session_id`` as an argument of its own,
never from :attr:`~chip_chat.agent.model.ToolInvocation.arguments`, and hands it
to the lanes, which hand it to #44's pool. A model that invented a ``demo_id``
finds it rejected by :mod:`chip_chat.agent.surface` before any body runs, and
would find it ignored even if it were not.

**Every call opens exactly one ``tool.<tool_name>`` span**, named from
:class:`~chip_chat.otel.schema.ToolName` rather than from the string the model
emitted. A model that asks for a tool nobody wrote gets a typed refusal and no
span at all, because an off-schema span name is the one failure mode
``otel/README.md`` says breaks every eval built on top of it.

**A lane may fail; the conversation may not fail with it.** RFC-001 §10, and it
is a property of this module rather than a rule the lanes are trusted to follow:
every lane returns its own decline instead of raising, :func:`dispatch` turns a
declined result into a failed *span* so the outage is visible, and the model is
handed a sentence it can say. A visitor asking about their points in the next
breath is served by a lane that never heard about it.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Final

from chip_chat.agent.desk import ActionOutcome, Desk, OrderableMenu
from chip_chat.agent.envelope import Citation, citations_from
from chip_chat.agent.hardcoded import ACCOUNT, MENU, SIMULATION_NOTICE, search_menu
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.model import ToolInvocation, UnknownToolError
from chip_chat.agent.orders import HARDCODED_MENU, OrderRejectedError
from chip_chat.agent.surface import ToolCallRejectedError, spec
from chip_chat.otel import (
    ConfirmationState,
    Document,
    OpsAction,
    TokenUsage,
    ToolName,
    ops_write,
    retriever_search,
    tool_call,
)
from chip_chat.otel.spans import OpsRecorder, ToolRecorder
from chip_chat.search.lane import KnowledgeLane
from chip_chat.search.retrieve import Retrieval
from chip_chat.snowflake.lane import AccountLane, PersonalizationLane
from chip_chat.vision.describe import DescribeError
from chip_chat.vision.lane import PhotoLane, PhotoMatch
from chip_chat.vision.matcher import Outcome, Resolution
from chip_chat.vision.store import PHOTO_REF_ARGUMENT, BlobRef

__all__ = [
    "DESK_WRITES",
    "PHOTO_UNAVAILABLE_MESSAGE",
    "TOOLS",
    "TOOL_SCHEMAS",
    "dispatch",
    "offered_schemas",
    "offered_tools",
]

_MENU_INDEX = "hardcoded-menu"
"""What ``retriever.search`` records as the index searched when no knowledge lane
is wired. Deliberately not a plausible index name: a trace should say out loud
that this is not AI Search. The real lane records its own alias."""

TOOLS: tuple[ToolName, ...] = (
    ToolName.SEARCH_MENU_KNOWLEDGE,
    ToolName.GET_POINTS_BALANCE,
    ToolName.GET_USUAL_ORDER,
    ToolName.PROPOSE_ORDER,
    ToolName.PLACE_ORDER,
)
"""The tools offered on every deployment, wired or not.

Five, and each of them answerable from :mod:`chip_chat.agent.hardcoded` when
nothing better is available. The three that are *not* here --
``ask_account_question``, ``get_recommendations``, ``match_meal_from_photo`` --
depend on a lane; :data:`chip_chat.agent.lanes.CONDITIONAL_TOOLS` is that list
and :func:`offered_tools` is what a call site should ask.
"""


DESK_WRITES: tuple[ToolName, ...] = (
    ToolName.CANCEL_ORDER,
    ToolName.REDEEM_POINTS,
    ToolName.UPDATE_PREFERENCES,
)
"""The three write tools that are offered only when a desk can answer them.

PRD T1 requires all four -- *place an order, cancel a pending order, redeem
points, and update stated preferences* -- and for a long time this list was the
distance between that sentence and the deployment. They are conditional for
exactly the reason :data:`chip_chat.agent.lanes.CONDITIONAL_TOOLS` is: each names
a row in ``CHIP_CHAT.ACCOUNTS`` that only the ops API can reach, there is no
honest stand-in for a points balance moving, and a tool the model can see and
nothing can answer is worse than an absent one.

``place_order`` is deliberately **not** here. It is answerable by either desk --
the week-one one simulates it against three hardcoded items and says so in the
receipt's own notice -- so withdrawing it on a deployment without an ops API
would take the confirmation gate off the only path a visitor has to it, which is
the one thing the week-one slice was built to keep.
"""


def _narrowed_to_the_orderable_menu(
    definition: dict[str, Any], menu: OrderableMenu | None
) -> dict[str, Any]:
    """Pin ``propose_order``'s item ids to what the desk can actually price.

    RFC-001 D3 says the model may describe food and may never name a SKU. The
    real enforcement is the deterministic matcher (#54) and the ops API's
    catalogue check (#63). The schema narrowing is a third and cheaper layer: an
    item id the desk cannot price is not expressible rather than merely
    rejected.

    The vocabulary comes from the **desk** rather than from this module, which
    is the change ``cc-jqs`` had left as a sentence. It used to be pinned to the
    three items in :data:`chip_chat.agent.hardcoded.MENU` with a note saying
    *"when the real catalogue reaches the desk, the enum is generated from it and
    this is the function that does it"*. The real catalogue has reached the desk,
    and since GitHub #106 it is the whole published menu -- 192 rows, where it
    was ten. Pinning the enum to three would break ordering outright, because
    ``get_usual_order`` answers off the real marts and names real ids the model
    could not then propose.

    **192 is not free.** The enum and the description it travels with cost
    about 5,150 tokens of every request, against 120 when the catalogue was ten
    rows; ``docs/menu-data.md`` §5 has the measurement.
    :meth:`chip_chat.agent.desk.Desk.orderable_menu` answering ``None`` is the
    lever if that becomes the wrong trade, and it is deliberately not pulled
    here -- an open schema is a *policy* change to what the model may name, and
    it belongs in the desk that knows what it can price rather than in the
    function that formats a definition.

    **The description travels with the enum**, and leaving it out was a real
    outage rather than a hypothetical one. A model shown opaque ``CMG-*`` ids and
    nothing else cannot compose a draft: it has no names to match a visitor's
    words against and no way to know a bowl requires a rice. It guesses, gets
    ``REQUIRED_SLOT_EMPTY``, guesses again, and reaches the loop's step ceiling.
    :class:`chip_chat.agent.desk.OrderableMenu` carries both halves for that
    reason.

    Args:
        definition: One tool definition, as the surface composed it.
        menu: What the desk can price, or ``None`` to leave the schema open.

    Returns:
        The definition, narrowed where it is ``propose_order`` and there is a
        vocabulary to narrow it to. The mapping is modified in place, which is
        safe because :func:`_definition` builds a fresh one per call.
    """
    if definition["function"]["name"] != ToolName.PROPOSE_ORDER.value or menu is None:
        return definition
    parameters = definition["function"]["parameters"]["properties"]["items"]
    parameters["description"] = (
        f"{parameters['description']} What this deployment can price: {menu.described}"
    )
    line = parameters["items"]
    line["properties"]["item_id"] = {
        **line["properties"]["item_id"],
        "enum": sorted(menu.item_ids),
    }
    return definition


def _definition(name: ToolName, menu: OrderableMenu | None) -> Mapping[str, Any]:
    """Return one tool definition, narrowed to what the desk can price."""
    return _narrowed_to_the_orderable_menu(spec(name).as_tool_definition(), menu)


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = tuple(
    _definition(name, HARDCODED_MENU) for name in TOOLS
)
"""The unconditional tool definitions, and what ``llm.completion`` records so
Arize's tool-selection evals can compare choice against offer.

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

A lane may fail; the conversation may not fail with it (RFC-001 §10). So this
comes back as a tool *result* the model can read and act on, the span is marked
failed, and the visitor gets asked a question rather than an error."""


def offered_tools(
    lanes: Lanes = NO_LANES, desk: Desk | None = None
) -> tuple[ToolName, ...]:
    """The tools that can actually be answered, given what is wired.

    Args:
        lanes: The backing services this deployment has.
        desk: The action lane. ``None`` and a desk that answers only
            ``place_order`` are the same answer here, which is what keeps the
            week-one slice's tool list byte-identical to what it was.

    Returns:
        :data:`TOOLS`, plus :data:`DESK_WRITES` where the desk can answer them,
        plus whichever of :data:`chip_chat.agent.lanes.CONDITIONAL_TOOLS` have a
        lane behind them -- and minus anything
        :attr:`chip_chat.agent.lanes.Lanes.withheld` names.

    The order is fixed and it matters: ``chip_chat.agent.loop.run_turn`` raises
    :class:`~chip_chat.agent.threads.ToolRegistrationError` when the list a
    conversation was opened with differs from the list a turn offers, and it
    compares tuples. Every call site therefore has to pass both arguments or
    neither -- and the failure is loud rather than a model politely declining a
    lane it can in fact reach.

    The withheld filter is applied to the whole list rather than only to the
    conditional tail, because *"the model can see a name nothing can answer"* is
    the same defect wherever the name came from. It is the last thing that
    happens here so that the order of what survives is unchanged: withdrawing a
    tool shortens the list and never reshuffles it.
    """
    writes = DESK_WRITES if desk is not None and desk.offers_every_write() else ()
    return tuple(
        tool
        for tool in (*TOOLS, *writes, *lanes.conditional_tools())
        if lanes.offers(tool)
    )


def offered_schemas(
    lanes: Lanes = NO_LANES, desk: Desk | None = None
) -> tuple[Mapping[str, Any], ...]:
    """The tool definitions to offer the model, aligned with :func:`offered_tools`.

    Derived from the surface for the same reason :data:`TOOL_SCHEMAS` is: the
    schema the model is shown and the schema its arguments are checked against
    have to be the same object.
    """
    menu = HARDCODED_MENU if desk is None else desk.orderable_menu()
    return tuple(_definition(name, menu) for name in offered_tools(lanes, desk))


def dispatch(
    invocation: ToolInvocation,
    *,
    session_id: str,
    desk: Desk,
    lanes: Lanes = NO_LANES,
    record_spend: Callable[[TokenUsage], None] | None = None,
    record_citations: Callable[[Mapping[str, Citation]], None] | None = None,
) -> Mapping[str, Any]:
    """Run one tool call and return what the model should see.

    Args:
        invocation: The call the model asked for.
        session_id: The bound conversation. Supplied by the request handler and
            never read out of ``invocation.arguments``. Handed to the lanes,
            which hand it to #44's pool, which is where it becomes an identity.
        desk: The order desk holding this session's drafts.
        lanes: The backing services this deployment has. The default is the
            week-one slice: hardcoded menu, hardcoded account, and the three
            conditional tools not offered at all -- which is what
            :func:`offered_tools` has already told the model.
        record_spend: Called with what a tool's *own* model calls cost, where a
            tool makes any. The photo lane does -- stage 4 is a vision
            completion -- and those tokens are as real as the agent's. Without
            this they would reach ``tool.match_meal_from_photo`` and stop
            there: the turn's rollup would undercount, and the spend ceiling
            would count a photo turn as cheaper than it was, which is the one
            direction a ceiling must never be wrong in.
        record_citations: Called with the citations ``retriever.search``
            returned, keyed by passage id, where a call retrieved anything.
            D9's mechanism needs both halves of the turn in one place: the ids
            the model names come back on the completion, and what those ids may
            resolve to comes from here. A turn that collected the second cannot
            have a source minted into it -- an id that is not in this mapping is
            dropped by :func:`chip_chat.agent.envelope.render` and counted.

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
            lanes=lanes,
            recorder=recorder,
            record_spend=record_spend,
            record_citations=record_citations,
        )
        recorder.record_result(result)
        return result


def _dispatch_inside_span(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: Desk,
    lanes: Lanes,
    recorder: ToolRecorder,
    record_spend: Callable[[TokenUsage], None] | None = None,
    record_citations: Callable[[Mapping[str, Citation]], None] | None = None,
) -> Mapping[str, Any]:
    """Validate the arguments, then run the tool. Refusals are results.

    The validation is the surface's, not this module's: nothing reaches a tool
    body until :mod:`chip_chat.agent.surface` has agreed that every argument on
    the call is one the tool declares. That is where "no tool takes a visitor
    identifier" stops being a claim about a schema document and becomes a
    property of the call path -- a model that emits ``demo_id`` gets a refusal
    it can read, and no tool body is ever offered the extra field.
    """
    if tool not in offered_tools(lanes, desk):
        # Checked before the arguments are, because "that lane is not available
        # on this deployment" is a more useful thing to tell a model than "your
        # blob_ref is missing" for a tool that would not have run either way.
        return _not_implemented(tool, lanes=lanes, desk=desk)
    try:
        bound = spec(tool).bind(arguments)
    except ToolCallRejectedError as rejection:
        recorder.record_failure(rejection)
        return {"rejected": "ARGUMENTS_REJECTED", "detail": rejection.reason}
    try:
        result = _run(
            tool, bound.arguments, session_id=session_id, desk=desk, lanes=lanes
        )
    except OrderRejectedError as rejection:
        recorder.record_failure(rejection.message)
        return dict(rejection.as_result())
    except DescribeError as declined:
        # The photo lane declining. §10 again: the span says so and the model is
        # handed something it can say to the visitor.
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
            if record_spend is not None:
                # And onward to the turn. The rollup answers "what does this
                # lane cost per call"; this is what keeps the turn total and
                # the spend ceiling from stopping at the tool boundary.
                record_spend(result.usage)
        return _photo_result(result)
    if isinstance(result, Retrieval):
        # Flattened here and not in the tool body, because this is the last
        # point at which anything holds the `source_url` D9 keeps away from the
        # model. Onward to the turn, where the ids the completion names are
        # resolved against it; `as_tool_result` is what the model gets, and it
        # does not carry the URL.
        if record_citations is not None:
            record_citations(citations_from(result.citations()))
        result = result.as_tool_result()
    _mark_a_declining_lane(recorder, result)
    return result


def _mark_a_declining_lane(recorder: ToolRecorder, result: Mapping[str, Any]) -> None:
    """Fail the tool span when the lane behind it declined.

    Every lane returns its decline rather than raising, which is what keeps the
    conversation alive -- and which would otherwise make an outage dangerously
    invisible, because a tool span that ended cleanly with a polite sentence in
    it looks exactly like a tool span that worked. One rule in one place, rather
    than a ``record_failure`` remembered in five tool bodies: a result carrying
    ``declined`` is a failed span.

    A ``rejected`` result is deliberately *not* failed here. That is the model
    getting an argument wrong or reaching for a lane this deployment does not
    have, which is a fact about the call rather than about a service, and the
    two are worth telling apart in a trace.
    """
    declined = result.get("declined")
    if declined is None:
        return
    reason = result.get("reason") or result.get("detail") or ""
    recorder.record_failure(f"{declined}: {reason}".rstrip(": "))


def _run(
    tool: ToolName,
    arguments: Mapping[str, Any],
    *,
    session_id: str,
    desk: Desk,
    lanes: Lanes,
) -> Mapping[str, Any] | PhotoMatch | Retrieval:
    """Body of one tool, inside its span.

    Returns a :class:`~chip_chat.vision.lane.PhotoMatch` for the photo tool, a
    :class:`~chip_chat.search.retrieve.Retrieval` for the knowledge tool where a
    lane is wired, and a plain mapping for the rest;
    :func:`_dispatch_inside_span` reads the tokens off the first and the
    citations off the second before flattening either, because both are things
    the turn needs and the model must not be shown -- a token count it would
    reason about, and a ``source_url`` it could paste.

    Raises:
        OrderRejectedError: From the two order tools, caught by the caller.
        DescribeError: From the photo lane, caught by the caller.
    """
    match tool:
        case ToolName.SEARCH_MENU_KNOWLEDGE:
            return _search_menu_knowledge(
                str(arguments.get("query", "")), lanes.knowledge
            )
        case ToolName.ASK_ACCOUNT_QUESTION if lanes.account is not None:
            return _ask_account_question(
                str(arguments.get("question", "")), lanes.account, session_id
            )
        case ToolName.GET_POINTS_BALANCE:
            return _get_points_balance(lanes.account, session_id)
        case ToolName.GET_USUAL_ORDER:
            return _get_usual_order(lanes.personalization, session_id)
        case ToolName.GET_RECOMMENDATIONS if lanes.personalization is not None:
            return _get_recommendations(lanes.personalization, session_id)
        case ToolName.PROPOSE_ORDER:
            return _propose_order(arguments.get("items"), session_id, desk)
        case ToolName.PLACE_ORDER:
            return _place_order(str(arguments.get("draft_id", "")), session_id, desk)
        case ToolName.CANCEL_ORDER:
            return _act(
                OpsAction.CANCEL_ORDER,
                str(arguments.get("order_id", "")),
                {"order_id": arguments.get("order_id")},
                session_id,
                desk,
            )
        case ToolName.REDEEM_POINTS:
            return _act(
                OpsAction.REDEEM_POINTS,
                str(arguments.get("reward_id", "")),
                {"reward_id": arguments.get("reward_id")},
                session_id,
                desk,
            )
        case ToolName.UPDATE_PREFERENCES:
            return _act(
                OpsAction.UPDATE_PREFERENCES,
                "(the preferences on the card)",
                {"prefs": arguments.get("prefs") or {}},
                session_id,
                desk,
            )
        case ToolName.MATCH_MEAL_FROM_PHOTO if lanes.photo is not None:
            return _match_meal_from_photo(
                str(arguments.get(PHOTO_REF_ARGUMENT, "")), lanes.photo
            )
        case _:  # pragma: no cover - dispatch refuses these before _run is reached
            return _not_implemented(tool, lanes=lanes, desk=desk)


def _not_implemented(
    tool: ToolName, *, lanes: Lanes, desk: Desk | None = None
) -> Mapping[str, Any]:
    """A real tool of the eleven that this deployment cannot answer.

    A typed refusal the model can read and act on, not an exception: it needs to
    tell the visitor that the lane is not available rather than fail the turn.
    """
    return {
        "rejected": "TOOL_NOT_IMPLEMENTED",
        "detail": (
            f"{tool.value} is not available on this deployment. Tools available "
            f"now: {', '.join(name.value for name in offered_tools(lanes, desk))}."
        ),
    }


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


def _search_menu_knowledge(
    query: str, lane: KnowledgeLane | None
) -> Mapping[str, Any] | Retrieval:
    """Menu knowledge. Nests ``retriever.search``, as the schema requires.

    The lane owns its own span, its own scores and its own decline (#49), so
    when one is wired this is a delegation and nothing else -- and the fallback
    below opens the same child span, so a trace has the same shape either way
    and ``retriever.search``'s ``index`` attribute is what says which corpus was
    actually searched.

    **The wired branch returns the retrieval rather than the tool result**, and
    the reason is D9. ``as_tool_result`` deliberately withholds ``source_url``
    from the model -- that is the field a model could paste into prose, and the
    whole mechanism is that it never reaches one -- so the citations cannot be
    reconstructed from what this hands back. They have to survive as far as
    :func:`_dispatch_inside_span`, which flattens the retrieval and passes the
    citations to the turn. Exactly the shape :class:`~chip_chat.vision.lane.PhotoMatch`
    already has for token counts, and for the same reason: something the span
    layer needs and the model must not see.
    """
    if lane is not None:
        return lane.search(query)
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


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def _ask_account_question(
    question: str, lane: AccountLane, session_id: str
) -> Mapping[str, Any]:
    """The account lane. Nests ``db.cortex_analyst``, which the lane opens.

    Offered only when a lane is wired, so there is no branch here and no
    fallback. That absence is PRD A4 and RFC-001 §10 in one line: a question
    this lane will not answer is a question nothing else in this file answers
    either, and there is no hand-written query to reach for because there is no
    hand-written query.
    """
    return lane.ask(question, session_id=session_id).as_tool_result()


def _get_points_balance(lane: AccountLane | None, session_id: str) -> Mapping[str, Any]:
    """The points read. No child span: the tool span is the whole of it.

    RFC-001 §09 gives ``db.cortex_analyst`` to the generated query and nothing
    to a fixed one, which is also the argument for this being a separate tool:
    one question with one answer does not need a language model to write its
    SQL, and the surface's description tells the model to prefer it for exactly
    that reason.
    """
    if lane is not None:
        return lane.points_balance(session_id=session_id).as_tool_result()
    return {
        "points_balance": ACCOUNT.points_balance,
        "member_since": ACCOUNT.member_since,
        "home_store": ACCOUNT.home_store.name,
        "usual_order": ACCOUNT.usual_order,
        "source": (
            "A hardcoded account fixture, not this visitor's rows -- no "
            "Snowflake account lane is wired on this deployment. Do not offer "
            "to redeem anything against it."
        ),
    }


# ---------------------------------------------------------------------------
# Personalization
# ---------------------------------------------------------------------------


def _get_usual_order(
    lane: PersonalizationLane | None, session_id: str
) -> Mapping[str, Any]:
    """The habit. No child span: the tool span is the whole of it.

    Returns item ids and not only a sentence, because "reorder my usual" has to
    become a draft over real rows. A model handed only ``"a chicken burrito bowl
    with a side of guac"`` would have to turn prose back into identifiers, and
    the one thing this architecture is arranged to prevent is a menu item
    arrived at by inference.

    The surface promises a confidence with the habit, and says the confidence is
    real and sometimes low. With a lane wired that is the mart's own number and
    the mart's own ``derived_at``. Without one, what is reported is the
    *absence* of a mart rather than a number invented to fill the field: an
    account fixture is not evidence of a habit, and a confidence fabricated here
    would be exactly the guess-presented-as-a-habit the tool description warns
    against.
    """
    if lane is not None:
        return lane.usual_order(session_id=session_id).as_tool_result()
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
            "history -- no personalization lane is wired on this deployment. "
            "Do not describe it to the visitor as something learned from their "
            "orders."
        ),
        "next_step": (
            "Call propose_order with these item_ids, adjusted for anything the "
            "visitor asked to change -- 'extra guac' is a second unit of the "
            "guacamole line, not a new item. Then show them the card."
        ),
    }


def _get_recommendations(lane: PersonalizationLane, session_id: str) -> Mapping[str, Any]:
    """The ranked mart. No child span: the tool span is the whole of it.

    Offered only when a lane is wired, and for a sharper reason than the account
    lane's: every row of this mart carries a ``rationale`` that #37 rendered from
    the visitor's own order share at scoring time, and a sentence composed here
    instead would be an explanation attributed to a model that never saw this
    visitor. There is no honest fixture for that, so there is no fixture.
    """
    return lane.recommendations(session_id=session_id).as_tool_result()


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


def _propose_order(items: Any, session_id: str, desk: Desk) -> Mapping[str, Any]:
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


def _place_order(draft_id: str, session_id: str, desk: Desk) -> Mapping[str, Any]:
    """Place a confirmed draft. ``ops.place_order`` is nested under this call.

    The ops span is opened even when the write is refused, because a refusal is
    exactly the thing an eval needs to see: ``confirmation_state=rejected`` on
    this span is a launch-gate violation, and a turn that quietly emitted no
    span would hide it.

    **Which process opens it depends on which desk this is**, and that is not a
    detail. A local desk performs the write here, so the span is opened here; a
    remote desk posts to the deployed ops API, which opens its own
    ``ops.place_order`` as a child of *this* ``tool.place_order`` from the trace
    context on the request. Opening one on both sides would put two gate
    decisions in one trace -- and worse, the ops API's edge refuses any write
    whose parent span is not a tool span, so an ops span opened here would make
    every remote write fail with ``TRACE_CONTEXT_REQUIRED``.
    :attr:`chip_chat.agent.desk.Desk.writes_here` is the flag and
    :func:`_ops_span` is where the choice is made once.
    """
    with _ops_span(desk, OpsAction.PLACE_ORDER, draft_id or "(none)") as ops:
        try:
            receipt = desk.place(session_id, draft_id)
        except OrderRejectedError:
            if ops is not None:
                ops.record_confirmation(ConfirmationState.REJECTED)
            raise
        body = receipt.as_dict()
        if ops is not None:
            ops.record_confirmation(ConfirmationState.CONFIRMED)
            ops.record_receipt(body)
    return {"receipt": body, "notice": SIMULATION_NOTICE}


@contextmanager
def _ops_span(
    desk: Desk, action: OpsAction, reference_id: str
) -> Iterator[OpsRecorder | None]:
    """Open ``ops.<action>`` here, or leave it to the process that writes.

    Yields ``None`` for a remote desk, and the yield being ``None`` rather than a
    no-op recorder is deliberate: a recorder that silently accepted
    ``record_confirmation`` would let a reader of this module believe a
    confirmation state was written somewhere, when for a remote desk it is
    written by ``api/functions/function_app.py`` off the record it actually
    claimed. Nothing here is in a position to know that answer.
    """
    if not desk.writes_here:
        yield None
        return
    with ops_write(action, reference_id=reference_id) as recorder:
        yield recorder


def _act(
    action: OpsAction,
    reference_id: str,
    arguments: Mapping[str, Any],
    session_id: str,
    desk: Desk,
) -> Mapping[str, Any]:
    """Offer or perform one of the three writes that name a row.

    One tool call, two possible answers, and the model is told which it got in a
    sentence it can act on. The first call finds no confirmation and comes back
    with a card; the visitor presses Confirm, which is a request carrying their
    session and nothing the model can compose; the second call finds the
    confirmation and writes.

    That the model is *told to call again* is worth being precise about, because
    it looks like the gate asking to be talked past and is the opposite. The
    second call succeeds only if a confirming request arrived in between. A model
    that calls twice in one turn gets the same card twice, which is what
    ``agent/tests/test_sabotage.py`` establishes has no other outcome available
    to it: there is no argument on any of these three tools through which a
    confirmation can be asserted.
    """
    with _ops_span(desk, action, reference_id) as ops:
        try:
            outcome = desk.act(session_id, action, arguments)
        except OrderRejectedError:
            if ops is not None:
                ops.record_confirmation(ConfirmationState.REJECTED)
            raise
        if ops is not None:
            ops.record_confirmation(
                ConfirmationState.CONFIRMED
                if outcome.confirmed
                else ConfirmationState.REJECTED
            )
            if outcome.receipt is not None:
                ops.record_receipt(outcome.receipt)
    return _act_result(action, outcome)


def _act_result(action: OpsAction, outcome: ActionOutcome) -> Mapping[str, Any]:
    """What the model is handed back for one of the three. See :func:`_act`."""
    if outcome.receipt is not None:
        return {"receipt": outcome.receipt, "notice": SIMULATION_NOTICE}
    return {
        "card": outcome.card,
        "requires_confirmation": True,
        "next_step": (
            "Show the visitor what is on the card and ask them to press "
            f"Confirm. Call {action.value} again only after they have; nothing "
            "is written until then."
        ),
        "notice": SIMULATION_NOTICE,
    }
