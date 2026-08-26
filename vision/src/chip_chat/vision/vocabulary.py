"""What the model is allowed to say, loaded rather than written.

RFC-001 section 07: *"Every enum is generated from the live catalogue at build
time, so the model's vocabulary cannot drift from what is orderable."* The
generating half of that sentence lives in :mod:`chip_chat.catalog.vocabulary`,
which renders a module of :class:`~enum.StrEnum` classes and a
``DESCRIBE_SCHEMA`` from catalogue rows. This module is the consuming half, and
its most important property is what it does **not** contain: there is no food
name anywhere in this package. Not a protein, not a salsa, not a vessel.
``tests/test_vocabulary.py`` asserts that against the source text, because a
hand-maintained enum list is the exact bug the generation exists to prevent and
it would arrive here first.

Loading, not importing
----------------------

The generated module is deliberately not committed -- ``python -m
chip_chat.catalog --vocabulary <path>`` writes it, and a copy checked in beside
this file would be a hand-maintained list with an extra step. So it is resolved
by dotted name at runtime:

.. code-block:: bash

    python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
    python -m chip_chat.catalog --landing landing --offline \\
        --vocabulary "$SITE_PACKAGES/chip_chat/vision_vocabulary.py"
    export CHIP_CHAT_VISION_VOCABULARY=chip_chat.vision_vocabulary

.. code-block:: python

    vocabulary = Vocabulary.from_env()   # CHIP_CHAT_VISION_VOCABULARY

That is also why :meth:`Vocabulary.from_env` fails loudly with the command that
produces the module instead of falling back to something built in. A fallback
vocabulary is a vocabulary nobody regenerated.

Two schemas, and the difference is the API's fault
--------------------------------------------------

:attr:`Vocabulary.schema` is RFC-001 section 07's schema exactly as the
catalogue rendered it: nine properties, two of them required, because a
photograph that shows no beans should come back with no ``beans``.

The model API's strict structured-output mode does not allow that. It requires
every key in ``properties`` to appear in ``required``, so "optional" has to be
spelled as "required, and may be null". :meth:`Vocabulary.strict_schema` is that
translation and nothing else, and :meth:`Vocabulary.validate` maps the nulls
back to absent slots on the way in. The catalogue's schema stays the definition;
the strict form is an adapter to one vendor's enforcement mechanism, and keeping
them separate is what stops the vendor's constraint leaking into the design.

Failing closed on a schema we do not understand
-----------------------------------------------

:meth:`Vocabulary.validate` implements the small, closed subset of JSON Schema
the catalogue renders, and raises :class:`VocabularyError` -- not a rejection --
on any construct it does not recognise. A validator that skipped what it could
not read would silently stop checking the day the schema grew a keyword, and
would do it while still returning valid-looking descriptions.
"""

import copy
import importlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Any, Final

__all__ = [
    "MODULE_VARIABLE",
    "SCHEMA_ATTRIBUTE",
    "SCHEMA_NAME",
    "SLOT_ITEMS_ATTRIBUTE",
    "STRICT_UNSUPPORTED",
    "SchemaViolationError",
    "Vocabulary",
    "VocabularyError",
]

MODULE_VARIABLE: Final = "CHIP_CHAT_VISION_VOCABULARY"
"""Dotted name of the generated module. No default: see the module docstring."""

SCHEMA_ATTRIBUTE: Final = "DESCRIBE_SCHEMA"
SLOT_ITEMS_ATTRIBUTE: Final = "SLOT_ITEMS"
"""The two names :meth:`Vocabulary.from_module` reads off a generated module."""

SCHEMA_NAME: Final = "described_meal"
"""The name the response format carries. Cosmetic to us, required by the API."""

_VERSION_PATTERN: Final = re.compile(
    r"Catalogue content version:\s*\n\s*([0-9a-f]{8,})", re.MULTILINE
)
"""How the content version is recovered from the generated module's docstring.

The catalogue writes it there rather than as a module attribute. Reading it is
optional -- :attr:`Vocabulary.content_version` is ``None`` when the docstring
does not carry one -- because a vocabulary that works is more important than a
vocabulary we can label, and this is only ever used to tell two builds apart.
"""

_GENERATE_HINT: Final = (
    "Generate it with: python -m chip_chat.catalog --landing <dir> --offline "
    "--vocabulary <path>. It is not committed on purpose -- RFC-001 section 07 "
    "wants the model's vocabulary generated from the live catalogue."
)


class VocabularyError(RuntimeError):
    """The vocabulary itself is missing, unreadable, or shaped unexpectedly.

    Always a build or configuration fault rather than anything a visitor did:
    the generated module was never written, or it was written by something that
    does not agree with this module about its shape. Raised eagerly, because
    the alternative is discovering it on the first photograph.
    """


class SchemaViolationError(ValueError):
    """The model returned something the generated schema does not permit.

    Carries the JSON-pointer-ish path to the offending value so a trace can say
    *which* slot was wrong, and never carries the value itself -- a violation is
    model output, and model output is exactly what must not become a product
    name by being copied somewhere convenient.
    """

    def __init__(self, path: str, detail: str) -> None:
        """Record where the response broke the schema.

        Args:
            path: Dotted path to the offending member, e.g. ``toppings[0].value``.
            detail: What was wrong with it, in terms of the schema.
        """
        super().__init__(f"{path or '<root>'}: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """One catalogue build's constrained vocabulary, as stage 4 uses it."""

    schema: Mapping[str, Any]
    """RFC-001 section 07's schema, as the catalogue rendered it."""

    slot_items: Mapping[str, Mapping[str, tuple[str, ...]]]
    """Slot to term to the catalogue items it may resolve to.

    Stage 4 does not use this -- resolving a term to a SKU is stage 5's job, and
    :mod:`chip_chat.vision.matcher` does it against the catalogue's own rows.
    It is generated from the same rows as the enums and travels with them, so
    that the matcher and the describer cannot end up holding vocabularies from
    two different catalogue builds; :attr:`content_version` is what
    :meth:`~chip_chat.vision.matcher.MealMatcher.resolve` checks to make that
    structural rather than hoped for.
    """

    content_version: str | None = None
    """The catalogue build this vocabulary came from, when it says."""

    @classmethod
    def from_module(cls, module: ModuleType) -> "Vocabulary":
        """Read a vocabulary off a generated module.

        Args:
            module: A module written by ``chip_chat.catalog.vocabulary``. Only
                its ``DESCRIBE_SCHEMA`` and ``SLOT_ITEMS`` are read; the
                :class:`~enum.StrEnum` classes beside them are for humans and
                for the matcher, and stage 4 works from the schema so that the
                enums and the schema cannot disagree.

        Returns:
            The vocabulary.

        Raises:
            VocabularyError: If either name is missing or is not the shape the
                generator produces.
        """
        schema = getattr(module, SCHEMA_ATTRIBUTE, None)
        if not isinstance(schema, Mapping):
            raise VocabularyError(
                f"{module.__name__} has no {SCHEMA_ATTRIBUTE} mapping. {_GENERATE_HINT}"
            )
        items = getattr(module, SLOT_ITEMS_ATTRIBUTE, None)
        if not isinstance(items, Mapping):
            raise VocabularyError(
                f"{module.__name__} has no {SLOT_ITEMS_ATTRIBUTE} mapping. "
                f"{_GENERATE_HINT}"
            )
        version = _content_version(module.__doc__)
        return cls(
            schema=MappingProxyType(copy.deepcopy(dict(schema))),
            slot_items=MappingProxyType(
                {
                    str(slot): MappingProxyType(
                        {str(term): tuple(ids) for term, ids in terms.items()}
                    )
                    for slot, terms in items.items()
                }
            ),
            content_version=version,
        )

    @classmethod
    def load(cls, dotted_name: str) -> "Vocabulary":
        """Import ``dotted_name`` and read a vocabulary off it.

        Args:
            dotted_name: Importable module name, e.g. ``chip_chat.vision_vocabulary``.

        Returns:
            The vocabulary.

        Raises:
            VocabularyError: If the module cannot be imported or is not one of
                the generator's.
        """
        try:
            module = importlib.import_module(dotted_name)
        except ImportError as error:
            raise VocabularyError(
                f"cannot import the generated vocabulary {dotted_name!r}: {error}. "
                f"{_GENERATE_HINT}"
            ) from error
        return cls.from_module(module)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Vocabulary":
        """Load the vocabulary named by ``CHIP_CHAT_VISION_VOCABULARY``.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The vocabulary.

        Raises:
            VocabularyError: If the variable is unset, or names a module that is
                not a generated vocabulary. There is deliberately no fallback:
                a built-in default would be the hand-maintained list this design
                exists to prevent, and it would be reached on exactly the
                deployment where the build step was forgotten.
        """
        source = os.environ if env is None else env
        dotted_name = source.get(MODULE_VARIABLE, "").strip()
        if not dotted_name:
            raise VocabularyError(
                f"the vision vocabulary is not configured: {MODULE_VARIABLE}. "
                f"{_GENERATE_HINT}"
            )
        return cls.load(dotted_name)

    @property
    def slots(self) -> tuple[str, ...]:
        """Every slot the schema defines, in the order it defines them."""
        return tuple(
            name for name in _properties(self.schema) if _is_slot(self.schema, name)
        )

    def values(self, slot: str) -> tuple[str, ...]:
        """Return the terms the model may use for ``slot``.

        Args:
            slot: A slot name, e.g. ``toppings``.

        Returns:
            The permitted values, in schema order. Empty is a legitimate answer
            -- a catalogue with no salsa rows publishes no salsa terms -- and it
            means the model may never fill that slot, which is correct.

        Raises:
            KeyError: If ``slot`` is not one of :attr:`slots`.
        """
        properties = _properties(self.schema)
        if slot not in properties or not _is_slot(self.schema, slot):
            raise KeyError(slot)
        return tuple(_enum_of(_slot_object(properties[slot])))

    def strict_schema(self) -> dict[str, Any]:
        """Return the schema in the form the API's strict mode will enforce.

        Two edits, both of them the vendor's constraints and neither of them a
        change to what the catalogue means:

        **Every property becomes required.** Strict structured output has no
        notion of an optional key, so the schema's genuinely optional members --
        every slot but two -- are re-expressed as required-and-nullable, and
        :meth:`validate` maps the nulls back to absent.

        **Numeric bounds are dropped.** ``minimum`` and ``maximum`` are on the
        list of keywords strict mode does not accept, and a schema carrying them
        is refused outright rather than merely unenforced -- so leaving them in
        would break every call, not just the ones with an out-of-range
        confidence. They stay in :attr:`schema`, which is what :meth:`validate`
        checks against, so the bound is still enforced. It moves from the
        model's decoder to our side of the wire, which is why a confidence of
        1.4 is a case with a test rather than a case that cannot arise.

        Returns:
            A fresh schema. The original is never mutated: a caller holding
            :attr:`schema` is holding the catalogue's definition, and an adapter
            for one vendor has no business editing it.

        Raises:
            VocabularyError: If the schema is not the object-with-properties
                shape this translation is defined for.
        """
        strict = _without_unsupported(copy.deepcopy(dict(self.schema)))
        properties = strict.get("properties")
        if not isinstance(properties, dict):
            raise VocabularyError("the generated schema has no properties object")
        required = {str(name) for name in strict.get("required", ())}
        for name, definition in properties.items():
            if name in required or not isinstance(definition, dict):
                continue
            properties[name] = _nullable(definition)
        strict["required"] = list(properties)
        strict["additionalProperties"] = False
        return strict

    def response_format(self) -> dict[str, Any]:
        """Return the ``response_format`` argument for a chat completion.

        Structured output is enforced by the model API rather than by parsing
        free text and hoping, which is the whole of RFC-001 section 07's
        "structured JSON only". :meth:`validate` still runs on the answer,
        because "the vendor promised" is not a thing D3 is willing to rest on.
        """
        return {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": self.strict_schema(),
            },
        }

    def validate(self, payload: object) -> dict[str, Any]:
        """Check one model response against the generated schema.

        Nulls are dropped first, because they are how :meth:`strict_schema`
        spells "the model did not fill this slot" and the catalogue's schema has
        no null in it.

        Args:
            payload: The parsed JSON the model returned.

        Returns:
            The response, with absent slots absent rather than null. A plain
            :class:`dict`, containing only what the schema permits.

        Raises:
            SchemaViolationError: If the response is not something the schema allows.
                Nothing is coerced, nothing is dropped, nothing is defaulted --
                a response that does not fit is refused, because every
                repair this function could perform is a guess about a
                photograph made by something that never saw it.
            VocabularyError: If the schema contains a construct this validator
                does not implement. A programming fault, not a model one.
        """
        return _check(_drop_nulls(payload), self.schema, "")


# --- validation -------------------------------------------------------------
#
# The subset below is exactly what chip_chat.catalog.vocabulary renders. Any
# other construct raises VocabularyError rather than being skipped, so a schema
# that grows a keyword fails loudly instead of quietly ceasing to be checked.

_LEAF_TYPES: Final[Mapping[str, type | tuple[type, ...]]] = MappingProxyType(
    {"boolean": bool, "string": str, "number": (int, float), "integer": int}
)

_KNOWN_KEYWORDS: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
    }
)


def _check(value: object, schema: Mapping[str, Any], path: str) -> Any:
    """Validate ``value`` against ``schema``, returning it unchanged."""
    unknown = sorted(set(schema) - _KNOWN_KEYWORDS)
    if unknown:
        raise VocabularyError(
            f"{path or '<root>'}: the generated schema uses keywords this "
            f"validator does not implement: {', '.join(unknown)}"
        )

    declared = schema.get("type")
    if not isinstance(declared, str):
        raise VocabularyError(f"{path or '<root>'}: schema has no single type")

    match declared:
        case "object":
            return _check_object(value, schema, path)
        case "array":
            return _check_array(value, schema, path)
        case "boolean" | "string" | "number" | "integer":
            return _check_leaf(value, schema, path, declared)
        case _:
            raise VocabularyError(f"{path or '<root>'}: unsupported type {declared!r}")


def _check_object(value: object, schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaViolationError(path, f"expected an object, got {_kind(value)}")
    properties = _properties(schema)
    if schema.get("additionalProperties", True) is not False:
        raise VocabularyError(
            f"{path or '<root>'}: the generated schema must close every object"
        )
    extra = sorted(set(value) - set(properties))
    if extra:
        # The one place the model can smuggle a product name past a constrained
        # vocabulary is a key nobody declared, so an unexpected key is a
        # violation rather than something to ignore.
        raise SchemaViolationError(path, f"unexpected keys: {', '.join(extra)}")
    missing = sorted(name for name in schema.get("required", ()) if name not in value)
    if missing:
        raise SchemaViolationError(path, f"missing required keys: {', '.join(missing)}")
    return {
        name: _check(member, properties[name], _join(path, name))
        for name, member in value.items()
    }


def _check_array(value: object, schema: Mapping[str, Any], path: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaViolationError(path, f"expected an array, got {_kind(value)}")
    items = schema.get("items")
    if not isinstance(items, Mapping):
        raise VocabularyError(f"{path or '<root>'}: array schema has no items")
    return [
        _check(member, items, f"{path}[{index}]") for index, member in enumerate(value)
    ]


def _check_leaf(
    value: object, schema: Mapping[str, Any], path: str, declared: str
) -> Any:
    expected = _LEAF_TYPES[declared]
    # bool is an int in Python and is not a number here: a confidence of `true`
    # is a violation, not a 1.0.
    if isinstance(value, bool) != (declared == "boolean") or not isinstance(
        value, expected
    ):
        raise SchemaViolationError(path, f"expected {declared}, got {_kind(value)}")

    permitted = schema.get("enum")
    if permitted is not None:
        if not isinstance(permitted, Sequence) or isinstance(permitted, str):
            raise VocabularyError(f"{path or '<root>'}: enum is not a list")
        if value not in permitted:
            # D3, arriving as an assertion. The model named something the
            # catalogue does not publish, and the answer is no rather than the
            # nearest match -- a nearest match is a fabricated SKU with a
            # plausible spelling.
            raise SchemaViolationError(path, "not a value the catalogue publishes")

    for keyword, ok in (
        ("minimum", lambda bound: value >= bound),
        ("maximum", lambda bound: value <= bound),
    ):
        bound = schema.get(keyword)
        if bound is None:
            continue
        if not isinstance(bound, int | float) or isinstance(bound, bool):
            raise VocabularyError(f"{path or '<root>'}: {keyword} is not a number")
        if not ok(bound):
            raise SchemaViolationError(path, f"outside the schema's {keyword} of {bound}")
    return value


def _drop_nulls(value: object) -> object:
    """Remove the nulls :meth:`Vocabulary.strict_schema` asked the model for."""
    if isinstance(value, dict):
        return {
            key: _drop_nulls(member)
            for key, member in value.items()
            if member is not None
        }
    if isinstance(value, list):
        return [_drop_nulls(member) for member in value if member is not None]
    return value


# --- schema reading ---------------------------------------------------------


def _properties(schema: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise VocabularyError("the generated schema has no properties object")
    return properties


def _slot_object(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the slot object itself, whether or not it is inside an array."""
    if definition.get("type") == "array":
        items = definition.get("items")
        if not isinstance(items, Mapping):
            raise VocabularyError("an array property has no items")
        return items
    return definition


def _is_slot(schema: Mapping[str, Any], name: str) -> bool:
    """Whether ``name`` is a ``{value, confidence}`` slot rather than a scalar."""
    definition = _properties(schema)[name]
    if not isinstance(definition, Mapping):
        return False
    inner = _slot_object(definition)
    return isinstance(inner, Mapping) and "value" in (inner.get("properties") or {})


def _enum_of(definition: Mapping[str, Any]) -> Sequence[str]:
    properties = definition.get("properties")
    if not isinstance(properties, Mapping):
        raise VocabularyError("a slot object has no properties")
    value = properties.get("value")
    if not isinstance(value, Mapping):
        raise VocabularyError("a slot object has no value property")
    permitted = value.get("enum", ())
    if not isinstance(permitted, Sequence) or isinstance(permitted, str):
        raise VocabularyError("a slot's enum is not a list")
    return [str(term) for term in permitted]


STRICT_UNSUPPORTED: Final = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "patternProperties",
        "propertyNames",
        "uniqueItems",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
"""Keywords strict structured output refuses.

Stripped from what :meth:`Vocabulary.strict_schema` sends.

Wider than what the catalogue renders today, on purpose. The catalogue emits two
of these -- ``minimum`` and ``maximum`` -- but it is generated from a schema in
RFC-001 that somebody may reasonably tighten later, and the failure mode of
sending one is a 400 on every photograph rather than a looser check on one. The
constraint is not lost: it stays in :attr:`Vocabulary.schema`, which is what
:meth:`Vocabulary.validate` reads.
"""


def _without_unsupported(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop the keywords strict mode will not accept."""
    stripped = {
        key: value for key, value in schema.items() if key not in STRICT_UNSUPPORTED
    }
    for key in ("properties", "items"):
        member = stripped.get(key)
        if isinstance(member, dict) and key == "items":
            stripped[key] = _without_unsupported(member)
        elif isinstance(member, dict):
            stripped[key] = {
                name: _without_unsupported(definition)
                if isinstance(definition, dict)
                else definition
                for name, definition in member.items()
            }
    return stripped


def _nullable(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``definition`` with null added to the types it permits."""
    nullable = dict(definition)
    declared = nullable.get("type")
    if isinstance(declared, str):
        nullable["type"] = [declared, "null"]
    elif isinstance(declared, list) and "null" not in declared:
        nullable["type"] = [*declared, "null"]
    return nullable


def _content_version(docstring: str | None) -> str | None:
    if not docstring:
        return None
    found = _VERSION_PATTERN.search(docstring)
    return found.group(1) if found else None


def _kind(value: object) -> str:
    """Name a JSON value's type the way the schema would, for a message."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name
