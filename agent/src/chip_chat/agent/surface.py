"""The action surface: the eleven tools, and the column missing from all of them.

RFC-001 section 06 fixes the tool surface, and the sentence that matters most is
about something that is not there:

    *Eleven tools. Note the absent column: none of them takes a visitor
    identifier, and none of the read tools has side effects.*

This module is that table in executable form, and the absence is the point. The
first launch gate -- no visitor ever sees another visitor's data -- is not held
by an instruction in the system prompt asking the model to be careful with
identity. It is held by there being **no argument anywhere in this surface
through which a visitor could be named**. Identity is bound to the Snowflake
session by the app and enforced by row access policies underneath; a model
determined to leak has nothing to fill in.

Two things keep it that way, and they are different:

:data:`ARGUMENT_NAMES`
    Every property name in every schema, at every depth. It is derived from the
    schemas rather than maintained beside them, so a parameter added later
    appears in it whether or not anyone remembers this file. ``test_surface.py``
    asserts it is disjoint from the identity vocabulary.

:class:`BoundArguments`
    The only shape validated tool arguments take, and it cannot be constructed
    with an argument the spec does not declare. A model that emits ``demo_id`` gets a
    :class:`ToolCallRejectedError`, not a call with an extra field for someone
    downstream to trust. ``additionalProperties: false`` in the emitted schema
    says the same thing to the provider, but that half runs on their side of the
    wire and is a courtesy rather than a control.

The second gate -- no write without confirmation -- is held by the ops API
(issue #63), and this surface's contribution to it is again an absence: every
write tool takes an identifier for something the visitor has already been
shown, and there is no field on any of them through which the model could
assert that a confirmation happened. See :data:`WRITE_TOOLS`.

**Tool descriptions are load-bearing, in the way the prompt deliberately is
not.** Tool-selection accuracy is the metric this architecture exists to get
right (PRD, ~95%), and a model chooses between ``search_menu_knowledge`` and
``ask_account_question`` by reading their descriptions, not by reading the
prompt's lane section. The descriptions below are written to separate the
confusable pairs on their own, so the prompt does not have to compensate.
``python -m chip_chat.agent.selection`` measures whether they do.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from chip_chat.otel.schema import WRITE_TOOLS, ToolName

__all__ = [
    "ARGUMENT_NAMES",
    "CANCELLATION_REALITY",
    "REDEMPTION_FINALITY",
    "TOOL_SPECS",
    "BoundArguments",
    "Lane",
    "Parameter",
    "ToolCallRejectedError",
    "ToolSpec",
    "argument_names",
    "spec",
    "tools_for_lane",
]


class Lane(StrEnum):
    """The five lanes of the system design, which the tools partition."""

    KNOWLEDGE = "knowledge"
    ACCOUNT = "account"
    PERSONALIZATION = "personalization"
    VISION = "vision"
    ACTION = "action"


class ToolCallRejectedError(ValueError):
    """A tool call did not match the tool's declared arguments.

    Never repaired, never rounded into validity: RFC-001 section 06 and
    ``docs/action-surface.md`` section 7 both require a typed rejection the
    agent has to answer for, because a call that was quietly fixed is a call
    nobody can eval.
    """

    def __init__(self, tool: ToolName, reason: str) -> None:
        super().__init__(f"{tool.value}: {reason}")
        self.tool = tool
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Parameter:
    """One argument of one tool.

    Args:
        name: The property name the model emits.
        description: What it is, in the model's terms.
        schema: A JSON Schema fragment for the value. Objects and arrays carry
            their own ``additionalProperties: false``.
        required: Whether the call is malformed without it.
    """

    name: str
    description: str
    schema: Mapping[str, Any]
    required: bool = True

    def json_schema(self) -> dict[str, Any]:
        """Return the fragment with the description folded in."""
        return {**self.schema, "description": self.description}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One of the eleven tools, as the agent registers it and as it is called."""

    name: ToolName
    lane: Lane
    description: str
    backed_by: str
    """Which system answers the call. RFC-001 section 06's last column."""

    parameters: tuple[Parameter, ...] = ()
    invention: str | None = None
    """What this tool's design invented, and what removing it would cost.

    ``None`` for the nine tools that mirror something the restaurant actually
    does. The two that do not say so here rather than in a comment, so that the
    provenance travels with the definition instead of living only in
    ``docs/action-surface.md`` section 10.
    """

    @property
    def writes(self) -> bool:
        """True for the four write tools, which the ops API gates."""
        return self.name in WRITE_TOOLS

    def json_schema(self) -> dict[str, Any]:
        """Return the parameter schema the agent registers with the model."""
        return {
            "type": "object",
            "properties": {
                parameter.name: parameter.json_schema() for parameter in self.parameters
            },
            "required": [p.name for p in self.parameters if p.required],
            "additionalProperties": False,
        }

    def as_tool_definition(self) -> dict[str, Any]:
        """Return the OpenAI-shaped function tool definition.

        Foundry's model endpoint is OpenAI-shaped, so this is the wire form for
        both the hosted agent's registration and the direct chat completions the
        selection probe uses.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name.value,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }

    def bind(self, arguments: Mapping[str, Any]) -> "BoundArguments":
        """Validate model-supplied arguments against this spec.

        Args:
            arguments: The arguments as the model emitted them.

        Returns:
            The invocation, which by existing has already been checked.

        Raises:
            ToolCallRejectedError: If an argument is undeclared, a required argument
                is absent, or a value is the wrong JSON type.
        """
        return BoundArguments(spec=self, arguments=dict(arguments))


_JSON_TYPES: Mapping[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


@dataclass(frozen=True, slots=True)
class BoundArguments:
    """A tool call that matches its tool's declared arguments.

    There is no way to hold one of these whose arguments are off-schema: the
    check runs in ``__post_init__``, so it runs on every construction path
    rather than only on the one that goes through :meth:`ToolSpec.bind`. That is
    what makes "no tool takes a visitor identifier" a property of the type
    rather than a property of the caller's diligence.
    """

    spec: ToolSpec
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        declared = {parameter.name: parameter for parameter in self.spec.parameters}
        for name in self.arguments:
            if name not in declared:
                raise ToolCallRejectedError(
                    self.spec.name,
                    f"{name!r} is not an argument of this tool "
                    f"(declared: {', '.join(sorted(declared)) or 'none'})",
                )
        for name, parameter in declared.items():
            if name not in self.arguments:
                if parameter.required:
                    raise ToolCallRejectedError(
                        self.spec.name, f"required argument {name!r} is absent"
                    )
                continue
            expected = parameter.schema.get("type")
            python_type = _JSON_TYPES.get(expected) if isinstance(expected, str) else None
            if python_type is not None and not isinstance(
                self.arguments[name], python_type
            ):
                raise ToolCallRejectedError(
                    self.spec.name,
                    f"{name!r} must be a JSON {expected}, "
                    f"got {type(self.arguments[name]).__name__}",
                )

    @property
    def tool(self) -> ToolName:
        """Which tool was called."""
        return self.spec.name


# ---------------------------------------------------------------------------
# Reusable schema fragments.
# ---------------------------------------------------------------------------

CANCELLATION_REALITY = (
    "Chipotle cannot normally cancel an order once it has been submitted -- it "
    "goes straight to the restaurant crew. This demo can, because nothing here "
    "is real."
)
"""``docs/action-surface.md`` section 7.2 calls this sentence *required, not
optional*: ``cancel_order`` is the one tool that models an affordance the product
does not offer, and a visitor who is not told that has been shown a feature that
does not exist. It lives here, beside the tool's :attr:`ToolSpec.invention` note,
so the sentence and the reason for it cannot get separated. The ops API (#63)
puts it on the receipt."""

REDEMPTION_FINALITY = (
    "Redeeming cannot be undone. The reward goes onto the account with a "
    "sixty-day expiry, and only one reward can be used per order."
)
"""``docs/action-surface.md`` section 7.3. Redemption is the write with no undo,
so this belongs on the card the visitor confirms rather than on the receipt that
tells them afterwards."""


_PORTIONS = ["Light", "Extra", "Side", "Half"]
"""The published portion vocabulary of ``docs/action-surface.md`` section 1.3."""

_STANCES = ["always", "never", "light", "extra", "side"]
"""The five stances of ``docs/action-surface.md`` section 7.4. Not a design: four
are the published portion vocabulary and two are the presence axis the slot
grammar already carries in *No Rice* and *No Beans*."""

_SELECTION = {
    "type": "object",
    "properties": {
        "modifier_item_id": {
            "type": "string",
            "description": "A modifier item id from the published catalogue.",
        },
        "group_name": {
            "type": ["string", "null"],
            "description": "The modifier group this selection fills, if it fills one.",
        },
        "portion": {
            "type": ["string", "null"],
            "enum": [*_PORTIONS, None],
            "description": "Portion, where the item publishes one. Null for a "
            "standard portion.",
        },
    },
    "required": ["modifier_item_id"],
    "additionalProperties": False,
}

_LINE = {
    "type": "object",
    "properties": {
        "item_id": {
            "type": "string",
            "description": "An orderable item id from the published catalogue.",
        },
        "quantity": {
            "type": "integer",
            "minimum": 1,
            "description": "How many of this line. Entrees are 1.",
        },
        "selections": {
            "type": "array",
            "items": _SELECTION,
            "description": "Modifiers on this line. Required slots must be "
            "filled -- a bowl with no rice choice is a rejection, not a default.",
        },
    },
    "required": ["item_id", "quantity"],
    "additionalProperties": False,
}

_STATED_PREFERENCE = {
    "type": "object",
    "properties": {
        "modifier_item_id": {
            "type": "string",
            "description": "The modifier the visitor has an opinion about.",
        },
        "stance": {
            "type": "string",
            "enum": _STANCES,
            "description": "How they want it. A portion stance is only valid "
            "where the menu publishes that portion for that modifier.",
        },
    },
    "required": ["modifier_item_id", "stance"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# The eleven. Six read, one draft, four write -- in RFC-001 section 06's order.
# ---------------------------------------------------------------------------

_KNOWLEDGE = (
    ToolSpec(
        name=ToolName.SEARCH_MENU_KNOWLEDGE,
        lane=Lane.KNOWLEDGE,
        backed_by="Azure AI Search over the harvested corpus",
        description=(
            "Search the restaurant's published pages -- menu items, ingredients, "
            "nutrition, allergen marks, the rewards programme, and ordering and "
            "delivery policy. Use this for anything that is true for every "
            "customer rather than for this visitor: what is in a burrito bowl, "
            "whether the barbacoa is spicy, how points are earned, whether an "
            "order can be cancelled. Returns passages, each with an id to cite. "
            "It knows nothing about this visitor's account."
        ),
        parameters=(
            Parameter(
                name="query",
                description=(
                    "What to look for, in the visitor's own words. Keep proper "
                    "nouns -- item names are the part keyword search gets right."
                ),
                schema={"type": "string", "minLength": 1},
            ),
        ),
    ),
)

_ACCOUNT = (
    ToolSpec(
        name=ToolName.ASK_ACCOUNT_QUESTION,
        lane=Lane.ACCOUNT,
        backed_by="Snowflake Cortex Analyst",
        description=(
            "Ask a question about this visitor's own order history, spend and "
            "store visits, and get an answer computed from their rows. Use it "
            "for anything with a filter, an aggregate or a time range in it: "
            "what they spent this year, what they order most, when they last "
            "visited a given store. It answers about the visitor in front of "
            "you and cannot be pointed at anyone else. It knows nothing about "
            "the published menu, and it is not the fastest way to a points "
            "balance -- use get_points_balance for that."
        ),
        parameters=(
            Parameter(
                name="question",
                description=(
                    "The question in natural language, as the visitor asked it."
                ),
                schema={"type": "string", "minLength": 1},
            ),
        ),
    ),
    ToolSpec(
        name=ToolName.GET_POINTS_BALANCE,
        lane=Lane.ACCOUNT,
        backed_by="Snowflake",
        description=(
            "The visitor's current points balance and which published rewards "
            "that balance can afford right now. One fixed question with a fast "
            "answer, so prefer it over ask_account_question whenever the "
            "question is about points or what they can redeem. Takes no "
            "arguments; it answers for whoever is in the conversation. It is a "
            "read and only a read: when the visitor asks to actually redeem "
            "something, that is redeem_points, which re-checks the balance "
            "itself and does not need this called first."
        ),
    ),
)

_PERSONALIZATION = (
    ToolSpec(
        name=ToolName.GET_USUAL_ORDER,
        lane=Lane.PERSONALIZATION,
        backed_by="Databricks gold mart, computed nightly",
        description=(
            "What this visitor habitually orders, as a draft-shaped set of items "
            "with a confidence and a short account of how it was worked out. Use "
            "it for 'what's my usual' and as the starting point for 'the usual "
            "but with guac'. The confidence is real and sometimes low -- when it "
            "is, say so rather than presenting a guess as a habit."
        ),
    ),
    ToolSpec(
        name=ToolName.GET_RECOMMENDATIONS,
        lane=Lane.PERSONALIZATION,
        backed_by="Databricks gold mart, computed nightly",
        description=(
            "Items this visitor has not tried, ranked, each with the reason it "
            "was suggested -- grounded in what they actually order rather than "
            "in what is generally popular. Use it for 'what should I try'. Use "
            "get_usual_order instead when they are asking what they already do."
        ),
    ),
)

_VISION = (
    ToolSpec(
        name=ToolName.MATCH_MEAL_FROM_PHOTO,
        lane=Lane.VISION,
        backed_by="Foundry vision model, then the deterministic menu matcher",
        description=(
            "Read an uploaded photograph of a meal and match what is in it to "
            "real catalogue items. Returns a description of what was seen, a "
            "candidate draft, a per-component confidence, and how many meals "
            "were visible. The item names come from the catalogue, never from "
            "the model, so it cannot name something that does not exist. When "
            "more than one meal is visible it does not choose -- say how many "
            "and ask which. Call it only with a reference the app has given you "
            "for a photo the visitor uploaded on this turn."
        ),
        parameters=(
            Parameter(
                name="blob_ref",
                description=(
                    "The storage reference the upload returned. Not a URL the "
                    "visitor supplied and not a description of the image."
                ),
                schema={"type": "string", "minLength": 1},
            ),
        ),
    ),
)

_PROPOSE = (
    ToolSpec(
        name=ToolName.PROPOSE_ORDER,
        lane=Lane.ACTION,
        backed_by="the app's draft store",
        description=(
            "Price a set of items and mint a draft the visitor can read and "
            "edit. This is the first half of every order: nothing is placed "
            "here and nothing is charged. It returns a draft id, the priced "
            "lines and any rejection. The store the draft is priced at is the "
            "visitor's home store and is not yours to choose. Call it whenever "
            "you are unsure whether the visitor meant to commit -- a proposal "
            "costs them one tap. It takes catalogue item ids, so when the "
            "visitor names food in words you do not already have ids for, find "
            "them with search_menu_knowledge first and call this with the "
            "result."
        ),
        parameters=(
            Parameter(
                name="items",
                description=(
                    "The lines to price. Each names a catalogue item, a "
                    "quantity, and its modifier selections."
                ),
                schema={"type": "array", "minItems": 1, "items": _LINE},
            ),
        ),
    ),
)

_WRITES = (
    ToolSpec(
        name=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        backed_by="the ops API",
        description=(
            "Place a draft the visitor has confirmed, and return a receipt. "
            "Takes only the id of a draft propose_order minted and the app "
            "rendered as a card. A draft the visitor has not confirmed is "
            "rejected here, so calling this to save a step does not save one. "
            "The order is simulated and the receipt says so."
        ),
        parameters=(
            Parameter(
                name="draft_id",
                description="The id returned by propose_order for this draft.",
                schema={"type": "string", "minLength": 1},
            ),
        ),
    ),
    ToolSpec(
        name=ToolName.CANCEL_ORDER,
        lane=Lane.ACTION,
        backed_by="the ops API",
        description=(
            "Cancel one of this visitor's pending orders and reverse the points "
            "it earned. Takes only the id of an order they placed and were "
            "shown. Worth knowing, and worth telling them: the real restaurant "
            "sends a submitted order straight to the crew and cannot cancel it "
            "at all -- this works here because the demo holds orders in a "
            "pending state of its own."
        ),
        parameters=(
            Parameter(
                name="order_id",
                description="An order the visitor placed and was shown a receipt for.",
                schema={"type": "string", "minLength": 1},
            ),
        ),
        invention=(
            "The cancellation window. The real product's window is zero: "
            "docs/action-surface.md section 3 records the published FAQ refusing "
            "cancellation outright, and delivery cancellation as a Customer "
            "Service call that may carry a fee. PRD T1 requires the action "
            "anyway, so the demo holds orders in a pending state of its own for "
            "a fixed simulated interval, and the receipt says out loud that the "
            "restaurant does not normally allow this -- the sentence is "
            "CANCELLATION_REALITY in this module. "
            "Removing it: a PRD change dropping T1's cancellation clause, after "
            "which this whole ToolSpec is deleted. It is kept separable for that "
            "reason -- it shares no parameter, schema fragment or code path with "
            "the other ten, so the removal is this literal and the corresponding "
            "OpsAction, and nothing else. docs/action-surface.md section 10, row 1."
        ),
    ),
    ToolSpec(
        name=ToolName.REDEEM_POINTS,
        lane=Lane.ACTION,
        backed_by="the ops API",
        description=(
            "Redeem a published reward against the visitor's points balance, "
            "and return a receipt with the new balance. This is the tool for "
            "'redeem my free guac' or 'use my points on that' -- do not call "
            "get_points_balance first, because the balance and the point cost "
            "are both re-read here, and an insufficient balance comes back as a "
            "rejection saying how many points are missing. It mints a reward on "
            "the account with a sixty-day life; it does not order anything and "
            "it cannot be undone, so the cost has to be on a card the visitor "
            "confirmed before this is called."
        ),
        parameters=(
            Parameter(
                name="reward_id",
                description=(
                    "A reward from the current catalogue, as returned by "
                    "get_points_balance."
                ),
                schema={"type": "string", "minLength": 1},
            ),
        ),
        invention=(
            "The reward ids, and the mapping from a reward to what it entitles "
            "you to. The published catalogue has a name, a point cost and an "
            "image path, and no identifier at all -- issue #21 refused to invent "
            "one at harvest time, so rewards.reward_id is null on all eight "
            "rows. V0 mints a stable slug per published reward, keyed to the "
            "published name and position. The reward-to-menu-item mapping is "
            "ours too: the published record deliberately does not say, and the "
            "image paths mislead. Both live in the demo's own data, labelled as "
            "the demo's, and must never be rendered as published fact. "
            "Removing it: nothing public replaces it -- the signed-in Rewards "
            "Exchange is not accessible. docs/action-surface.md section 10, "
            "rows 2 and 3."
        ),
    ),
    ToolSpec(
        name=ToolName.UPDATE_PREFERENCES,
        lane=Lane.ACTION,
        backed_by="the ops API",
        description=(
            "Save the three things a visitor may change about their persona: "
            "display name, home store, and standing preferences over menu "
            "modifiers. Absent keys are left alone. Everything else about the "
            "account -- order history, points, the usual order -- is read-only "
            "and this tool will not reach it. Preferences filter what you "
            "propose and are said out loud when they do; they never rewrite what "
            "the visitor's history says. This is not an allergen control and the "
            "acknowledgement says so."
        ),
        parameters=(
            Parameter(
                name="prefs",
                description=(
                    "The fields to change. Send only the ones the visitor asked "
                    "to change; null clears a field."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "display_name": {
                            "type": ["string", "null"],
                            "maxLength": 40,
                            "description": "What to call them, 1-40 characters.",
                        },
                        "home_store": {
                            "type": ["string", "null"],
                            "description": (
                                "A store id. Changes where the next order is "
                                "priced and nothing about past orders."
                            ),
                        },
                        "stated_preferences": {
                            "type": ["array", "null"],
                            "maxItems": 20,
                            "items": _STATED_PREFERENCE,
                            "description": (
                                "Standing preferences over modifiers. Replaces "
                                "the current set."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            ),
        ),
        invention=(
            "The stated-preferences vocabulary. What the real product persists "
            "under 'preferences' is communication opt-ins, not food preferences "
            "(docs/action-surface.md section 4); PRD T1 requires the action "
            "anyway. The five stances are drawn from the real modifier taxonomy "
            "and refuse what the product refuses, which is as close to the "
            "product as this can get. docs/action-surface.md section 10, row 4."
        ),
    ),
)

TOOL_SPECS: tuple[ToolSpec, ...] = (
    *_KNOWLEDGE,
    *_ACCOUNT,
    *_PERSONALIZATION,
    *_VISION,
    *_PROPOSE,
    *_WRITES,
)
"""The eleven tools of RFC-001 section 06, in the order that table lists them.

A *definition*, not an implementation. :mod:`chip_chat.agent.tools` is where the
subset that has been built so far actually runs, and it takes its schemas from
here so that the two cannot drift."""

_BY_NAME: Mapping[ToolName, ToolSpec] = {tool.name: tool for tool in TOOL_SPECS}


def spec(name: ToolName) -> ToolSpec:
    """Return the specification for ``name``."""
    return _BY_NAME[name]


def tools_for_lane(lane: Lane) -> tuple[ToolSpec, ...]:
    """Return the tools that serve ``lane``, in registration order."""
    return tuple(tool for tool in TOOL_SPECS if tool.lane is lane)


def _walk_property_names(schema: Mapping[str, Any]) -> Iterator[str]:
    """Yield every property name in ``schema``, at every depth."""
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, subschema in properties.items():
            yield str(name)
            if isinstance(subschema, Mapping):
                yield from _walk_property_names(subschema)
    items = schema.get("items")
    if isinstance(items, Mapping):
        yield from _walk_property_names(items)
    elif isinstance(items, Sequence) and not isinstance(items, str | bytes):
        for entry in items:
            if isinstance(entry, Mapping):
                yield from _walk_property_names(entry)


def argument_names(tool: ToolSpec) -> frozenset[str]:
    """Return every property name ``tool`` accepts, at every depth."""
    return frozenset(_walk_property_names(tool.json_schema()))


ARGUMENT_NAMES: frozenset[str] = frozenset(
    name for tool in TOOL_SPECS for name in argument_names(tool)
)
"""Every name the model can put an argument under, across the whole surface.

Derived from the schemas rather than listed beside them, so a parameter added in
six months lands here without anyone remembering to. This is the set
``test_surface.py`` asserts contains no visitor identifier -- which is RFC-001
section 05's guarantee stated as a property of the code rather than as a
paragraph of prose.
"""
