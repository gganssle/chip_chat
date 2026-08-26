"""The vision model's constrained vocabulary, derived and then rendered.

RFC-001 section 07 says that "every enum is generated from the live catalogue
at build time, so the model's vocabulary cannot drift from what is orderable".
This module is both halves of that sentence: :func:`build_vocabulary` derives
the terms from catalogue rows, and :func:`render_module` writes them out as a
Python module of :class:`~enum.StrEnum` classes with the stage-4 JSON schema
beside them.

Nothing here is hand-maintained, and the module it renders says so in its
first line. A term exists because a published row exists; a term disappears
when the row does. The one classification this package makes rather than reads
— splitting Chipotle's four salsas out of its toppings, which RFC-001 section
07 gives a slot of their own — is carried on every affected row as
:attr:`~chip_chat.catalog.records.VocabularyTerm.derivation`, so a reader can
tell the inferred part of the vocabulary from the published part without
reading this file.

Two slots resolve to no item at all. A described *bowl* and a described
*chicken* are each half of an entree — ``CMG-101`` is the Chicken Bowl — so
``vessel`` and ``protein`` terms carry a null ``item_id`` and the matcher
resolves the pair through ``(item_type, primary_filling)``. Giving either half
an ``item_id`` would let a matcher resolve "a bowl" to a SKU without ever
learning what was in it.
"""

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from chip_chat.catalog.errors import VocabularyCollisionError
from chip_chat.catalog.records import (
    Derivation,
    MenuItem,
    Modifier,
    Slot,
    VocabularyTerm,
)

SALSA_SUFFIX = "salsa"
"""The published name ending that moves a topping into the ``salsas`` slot.

Chipotle publishes a ``Salsa`` modifier type, but only on its build-your-own
items. On an ordinary entree the same four salsas — "Fresh Tomato Salsa",
"Tomatillo-Green Chili Salsa", "Tomatillo-Red Chili Salsa" and "Roasted
Chili-Corn Salsa" — arrive as ``Toppings``, with no published field separating
them from cheese or lettuce. This is the only classification in the package
that is not read off a published column, which is why it is a named constant
and why every term it produces is labelled
:attr:`~chip_chat.catalog.records.Derivation.NAME_SUFFIX`.
"""

ENTREE_CATEGORY = "Entree"
"""The published category whose ``item_type`` is the vessel vocabulary."""

TOPPINGS_TYPE = "Toppings"
"""The published modifier type the salsas hide inside on ordinary entrees."""

SLOT_TYPES: Mapping[str, Slot] = {
    "Rice": Slot.RICE,
    "Beans": Slot.BEANS,
    "Salsa": Slot.SALSAS,
    TOPPINGS_TYPE: Slot.TOPPINGS,
}
"""Published modifier type to the slot it fills.

Read off ``itemType``, which is the one column that answers the question for
every entree. The content groups say the same thing less reliably: rice is a
``RiceContentGroup`` choice on a burrito and a ``ToppingsContentGroup`` choice
on a salad, and both rows are the same rice.

Every other published type — ``ExtraPortion``, ``HalfPortion``, ``Tortillas``,
``Beverage``, ``Side`` — has no slot in RFC-001 section 07's described-meal
schema and gets ``None``. That is not a gap: a photograph of a bowl does not
say whether the extra chicken in it was ordered as extra.
"""

MODULE_HEADER = '''"""The vision model's slot vocabulary, generated from the catalogue.

DO NOT EDIT. This module is written by ``chip_chat.catalog.vocabulary`` from a
built catalogue and is overwritten whenever the catalogue is rebuilt. A term
here exists because a published menu row exists; editing one by hand would
reintroduce exactly the drift RFC-001 section 07 generates it to prevent.

Catalogue content version:
    {content_version}
"""

from enum import StrEnum
'''
"""The generated module's own docstring and imports."""


def slug(name: str) -> str:
    """Return a published name as a stable enum value.

    Accents are folded rather than dropped, punctuation becomes a separator,
    and runs of separators collapse. The result is deterministic and depends
    only on the published name, so the same menu yields the same vocabulary on
    every build.

    Args:
        name: A published name, e.g. ``Tomatillo-Green Chili Salsa``.

    Returns:
        The slug, e.g. ``tomatillo_green_chili_salsa``.
    """
    folded = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    parts: list[str] = []
    current: list[str] = []
    for char in stripped.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return "_".join(parts)


def member(value: str) -> str:
    """Return the enum member name for a slug.

    Args:
        value: A slug from :func:`slug`.

    Returns:
        The member name. A slug that starts with a digit is prefixed, because
        ``3_POINTER`` is not an identifier and silently renaming it to
        something prettier would break the correspondence between the member
        and the published name it came from.
    """
    upper = value.upper()
    return f"ITEM_{upper}" if upper[:1].isdigit() else upper


def build_vocabulary(
    menu_items: Sequence[MenuItem], modifiers: Sequence[Modifier]
) -> tuple[VocabularyTerm, ...]:
    """Derive the constrained vocabulary from catalogue rows.

    Args:
        menu_items: The catalogue's items. Vessels and proteins come from the
            orderable entrees among them.
        modifiers: The catalogue's modifiers, already carrying the slot each
            belongs to.

    Returns:
        The terms, deduplicated and sorted by ``(slot, value)``. The same rice
        is a modifier on every entree and one term here, and its
        ``item_ids`` gathers every identifier Chipotle publishes it under.

    Raises:
        VocabularyCollisionError: If two *different* published names slugify
            onto one value within a slot. Nothing is merged and nothing is
            renamed; two foods sharing one enum member is the fabricated-SKU
            failure arriving by a different route.

            One name published under several identifiers is not a collision.
            Chipotle does that routinely — guacamole is ``CMG-1001`` on some
            entrees and ``CMG-5301`` on others — and both identifiers land in
            one term's ``item_ids``.
    """
    terms: dict[tuple[Slot, str], VocabularyTerm] = {}

    def add(term: VocabularyTerm) -> None:
        key = (term.slot, term.value)
        seen = terms.get(key)
        if seen is None:
            terms[key] = term
            return
        if seen.name != term.name:
            raise VocabularyCollisionError(
                term.slot.value, term.value, seen.name, term.name
            )
        merged = tuple(sorted(set(seen.item_ids) | set(term.item_ids)))
        if merged != seen.item_ids:
            terms[key] = replace(seen, item_ids=merged)

    for item in menu_items:
        if item.category != ENTREE_CATEGORY:
            continue
        add(
            VocabularyTerm(
                slot=Slot.VESSEL,
                value=slug(item.item_type),
                name=item.item_type,
                item_ids=(),
                derivation=Derivation.ITEM_TYPE,
                source_url=item.source_url,
                harvested_at=item.harvested_at,
            )
        )
        if item.primary_filling is None:
            continue
        add(
            VocabularyTerm(
                slot=Slot.PROTEIN,
                value=slug(item.primary_filling),
                name=item.primary_filling,
                item_ids=(),
                derivation=Derivation.PRIMARY_FILLING,
                source_url=item.source_url,
                harvested_at=item.harvested_at,
            )
        )

    for modifier in modifiers:
        if modifier.slot is None or modifier.derivation is None:
            continue
        add(
            VocabularyTerm(
                slot=modifier.slot,
                value=slug(modifier.name),
                name=modifier.name,
                item_ids=(modifier.modifier_item_id,),
                derivation=modifier.derivation,
                source_url=modifier.source_url,
                harvested_at=modifier.harvested_at,
            )
        )

    return tuple(sorted(terms.values(), key=lambda term: (term.slot.value, term.value)))


def slot_of(*, modifier_type: str, name: str) -> tuple[Slot, Derivation] | None:
    """Return the slot a modifier belongs to, and how that was decided.

    Args:
        modifier_type: The modifier's published ``itemType``.
        name: Its published name, which is consulted only for the salsas.

    Returns:
        The slot and the derivation, or ``None`` for a modifier that is not
        part of the described-meal vocabulary at all — an extra portion, a
        half portion, a tortilla on the side.
    """
    slot = SLOT_TYPES.get(modifier_type)
    if slot is None:
        return None
    if slot is Slot.TOPPINGS and _is_salsa(name):
        return Slot.SALSAS, Derivation.NAME_SUFFIX
    return slot, Derivation.MODIFIER_TYPE


def _is_salsa(name: str) -> bool:
    """Whether a published topping name is one of the salsas."""
    value = slug(name)
    return value == SALSA_SUFFIX or value.endswith(f"_{SALSA_SUFFIX}")


def render_module(terms: Iterable[VocabularyTerm], content_version: str) -> str:
    """Render the generated enum module for a catalogue's vocabulary.

    The module holds one :class:`~enum.StrEnum` per slot, the mapping from
    each term to the catalogue item it resolves to, and the stage-4 response
    schema of RFC-001 section 07 with the enums substituted into it. A model
    handed that schema cannot name a food the catalogue does not have, which
    is the structural half of D3.

    Args:
        terms: The vocabulary, as :func:`build_vocabulary` returns it.
        content_version: The catalogue's content version, recorded in the
            module so a vocabulary that has drifted from its catalogue can be
            detected rather than trusted.

    Returns:
        The module source, ending in a newline.
    """
    by_slot: dict[Slot, list[VocabularyTerm]] = {slot: [] for slot in Slot}
    for term in terms:
        by_slot[term.slot].append(term)

    lines = [MODULE_HEADER.format(content_version=content_version)]
    for slot in Slot:
        lines.append(_render_enum(slot, by_slot[slot]))
    lines.append(_render_item_map(by_slot))
    lines.append(_render_schema(by_slot))
    return "\n".join(lines)


def _literal(value: str) -> str:
    """Return a Python string literal, double-quoted the way this repo writes them."""
    return json.dumps(value)


def _identifiers(item_ids: Sequence[str]) -> str:
    """Return a tuple literal of catalogue identifiers."""
    if not item_ids:
        return "()"
    inner = ", ".join(_literal(item_id) for item_id in item_ids)
    return f"({inner},)" if len(item_ids) == 1 else f"({inner})"


def _render_enum(slot: Slot, terms: Sequence[VocabularyTerm]) -> str:
    """Render one slot's enum class."""
    body = [
        f"\nclass {_class_name(slot)}(StrEnum):",
        f'    """Published {slot.value} the model may return."""',
        "",
    ]
    if not terms:
        body.append("    # The catalogue published no term for this slot.")
        body.append("    pass")
        return "\n".join(body) + "\n"
    for term in terms:
        body.append(f"    {member(term.value)} = {_literal(term.value)}")
        body.append(f'    """{term.name}."""')
    return "\n".join(body) + "\n"


def _render_item_map(by_slot: Mapping[Slot, Sequence[VocabularyTerm]]) -> str:
    """Render the term-to-item map, which is how a slot value becomes a SKU."""
    lines = [
        "\nSLOT_ITEMS: dict[str, dict[str, tuple[str, ...]]] = {",
        "    # Slot to term to the catalogue items the term may resolve to.",
        "    # More than one is normal: Chipotle publishes guacamole under two",
        "    # identifiers and white rice under two more, so which one a described",
        "    # meal means depends on the entree it is on. Resolve (entree, term)",
        "    # against the catalogue's `modifiers` table; this is the candidate set.",
        "    # `vessel` and `protein` are empty on purpose: each is half of an",
        "    # entree, and the pair resolves through the catalogue's",
        "    # `(item_type, primary_filling)` rather than through either half.",
    ]
    for slot, terms in by_slot.items():
        if not terms:
            lines.append(f"    {_literal(slot.value)}: {{}},")
            continue
        lines.append(f"    {_literal(slot.value)}: {{")
        for term in terms:
            lines.append(
                f"        {_literal(term.value)}: {_identifiers(term.item_ids)},"
            )
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_schema(by_slot: Mapping[Slot, Sequence[VocabularyTerm]]) -> str:
    """Render the stage-4 response schema, enums substituted in."""
    values = {
        slot.value: "[" + ", ".join(_literal(term.value) for term in terms) + "]"
        for slot, terms in by_slot.items()
    }
    lines = [
        "\nDESCRIBE_SCHEMA: dict[str, object] = {",
        "    # RFC-001 section 07, stage 4. The model may return nothing else.",
        '    "type": "object",',
        '    "additionalProperties": False,',
        '    "required": ["is_chipotle_style", "meals_visible"],',
        '    "properties": {',
        '        "is_chipotle_style": {"type": "boolean"},',
    ]
    for slot in (Slot.VESSEL, Slot.PROTEIN, Slot.RICE, Slot.BEANS):
        lines.append(f'        "{slot.value}": _slot({values[slot.value]}),')
    for slot in (Slot.SALSAS, Slot.TOPPINGS):
        lines.append(
            f'        "{slot.value}": {{"type": "array", '
            f'"items": _slot({values[slot.value]})}},'
        )
    lines.extend(
        [
            '        "meals_visible": {"type": "integer", "minimum": 0},',
            "        # Display-only. Nothing downstream parses it, which is what",
            "        # keeps the one free-text field from becoming a product name.",
            '        "notes": {"type": "string"},',
            "    },",
            "}",
        ]
    )
    helper = [
        "\ndef _slot(values: list[str]) -> dict[str, object]:",
        '    """One slot: a value from the catalogue, and how sure the model is."""',
        "    return {",
        '        "type": "object",',
        '        "additionalProperties": False,',
        '        "required": ["value", "confidence"],',
        '        "properties": {',
        '            "value": {"type": "string", "enum": values},',
        '            "confidence": {"type": "number", "minimum": 0, "maximum": 1},',
        "        },",
        "    }",
        "",
    ]
    return "\n".join(helper + lines) + "\n"


def _class_name(slot: Slot) -> str:
    """Return the enum class name for a slot, e.g. ``Salsa`` for ``salsas``."""
    stem = slot.value[:-1] if slot.value.endswith("s") else slot.value
    return "".join(part.capitalize() for part in stem.split("_"))
