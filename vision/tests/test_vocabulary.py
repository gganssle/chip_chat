"""The vocabulary is loaded from a build artefact, and this is what that buys.

The claim under test is RFC-001 section 07's: *"every enum is generated from the
live catalogue at build time, so the model's vocabulary cannot drift from what is
orderable."* A test can only settle that by changing the catalogue and watching
the accepted vocabulary change with it, which is what most of this file does.
"""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from chip_chat.vision.testing import (
    DEFAULT_TERMS,
    generated_vocabulary,
    vocabulary_module,
    vocabulary_module_source,
)
from chip_chat.vision.vocabulary import (
    MODULE_VARIABLE,
    STRICT_UNSUPPORTED,
    SchemaViolationError,
    Vocabulary,
    VocabularyError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "generated-vocabulary.py.txt"
"""What the real generator wrote. See ``fixtures/README.md``."""


@pytest.fixture
def real_module() -> ModuleType:
    """The catalogue generator's own output, loaded the way a deployment does."""
    return vocabulary_module(FIXTURE.read_text(encoding="utf-8"), "generated_fixture")


# --- what "generated at build time" actually means ---------------------------


def test_the_accepted_vocabulary_is_whatever_the_catalogue_published() -> None:
    published = generated_vocabulary({"protein": ("tofu", "carnitas")})
    assert published.values("protein") == ("tofu", "carnitas")

    # The same describer, a different catalogue build, a different vocabulary --
    # with no edit to this package in between. That is the whole design.
    republished = generated_vocabulary({"protein": ("tofu",)})
    assert republished.values("protein") == ("tofu",)


def test_a_term_that_left_the_catalogue_is_no_longer_accepted() -> None:
    before = generated_vocabulary({"protein": ("tofu", "carnitas")})
    after = generated_vocabulary({"protein": ("tofu",)})

    payload = {
        "is_chipotle_style": True,
        "meals_visible": 1,
        "protein": {"value": "carnitas", "confidence": 0.9},
    }
    assert before.validate(payload)["protein"]["value"] == "carnitas"
    with pytest.raises(SchemaViolationError):
        after.validate(payload)


def test_a_slot_the_catalogue_published_nothing_for_accepts_nothing() -> None:
    vocabulary = generated_vocabulary({**DEFAULT_TERMS, "salsas": ()})
    assert vocabulary.values("salsas") == ()
    with pytest.raises(SchemaViolationError):
        vocabulary.validate(
            {
                "is_chipotle_style": True,
                "meals_visible": 1,
                "salsas": [{"value": "anything", "confidence": 0.5}],
            }
        )


def test_the_loader_reads_the_real_generators_output(real_module: ModuleType) -> None:
    vocabulary = Vocabulary.from_module(real_module)

    assert vocabulary.slots == (
        "vessel",
        "protein",
        "rice",
        "beans",
        "salsas",
        "toppings",
    )
    assert vocabulary.values("vessel") == ("bowl", "burrito")
    assert vocabulary.slot_items["toppings"]["guacamole"] == ("CMG-1001",)
    # Recovered from the module docstring, which is where the generator puts it.
    assert vocabulary.content_version == real_module.__doc__.split()[-1]  # type: ignore[union-attr]


def test_the_fixture_builder_mirrors_the_real_generator(real_module: ModuleType) -> None:
    """The mirror in ``chip_chat.vision.testing`` and the generator agree.

    Not on their text -- they are different renderers -- but on everything this
    package reads off them. A mirror that had drifted would make every other
    test in this file a test of the mirror.
    """
    real = Vocabulary.from_module(real_module)
    mirrored = generated_vocabulary(
        {slot: real.values(slot) for slot in real.slots},
        content_version=real.content_version or "",
    )

    assert mirrored.slots == real.slots
    assert {slot: mirrored.values(slot) for slot in mirrored.slots} == {
        slot: real.values(slot) for slot in real.slots
    }
    assert mirrored.schema == real.schema
    assert mirrored.content_version == real.content_version


# --- loading, and refusing to invent a fallback ------------------------------


def test_from_env_refuses_to_fall_back_to_anything() -> None:
    with pytest.raises(VocabularyError, match=MODULE_VARIABLE):
        Vocabulary.from_env({})


def test_from_env_says_how_to_generate_the_module() -> None:
    with pytest.raises(VocabularyError, match=re.escape("python -m chip_chat.catalog")):
        Vocabulary.from_env({MODULE_VARIABLE: "   "})


def test_a_module_that_is_not_a_generated_vocabulary_is_refused() -> None:
    with pytest.raises(VocabularyError, match="DESCRIBE_SCHEMA"):
        Vocabulary.from_module(vocabulary_module("x = 1", "not_generated"))


def test_a_vocabulary_without_slot_items_is_refused() -> None:
    source = vocabulary_module_source().replace("SLOT_ITEMS", "SLOT_ITEMS_RENAMED")
    with pytest.raises(VocabularyError, match="SLOT_ITEMS"):
        Vocabulary.from_module(vocabulary_module(source))


def test_a_module_that_cannot_be_imported_names_itself() -> None:
    with pytest.raises(VocabularyError, match=re.escape("chip_chat.no_such_vocabulary")):
        Vocabulary.load("chip_chat.no_such_vocabulary")


def test_a_generated_module_without_a_content_version_still_loads() -> None:
    source = vocabulary_module_source().replace("Catalogue content version:", "Built:")
    assert Vocabulary.from_module(vocabulary_module(source)).content_version is None


def test_the_loaded_schema_is_a_copy() -> None:
    """A caller holding the schema is holding the catalogue's definition."""
    vocabulary = generated_vocabulary()
    strict = vocabulary.strict_schema()
    strict["properties"]["protein"]["type"] = ["object", "null", "nonsense"]

    assert vocabulary.schema["properties"]["protein"]["type"] == "object"
    assert vocabulary.strict_schema()["properties"]["protein"]["type"] == [
        "object",
        "null",
    ]


# --- the two schemas ---------------------------------------------------------


def test_the_catalogues_schema_keeps_optional_slots_optional() -> None:
    schema = generated_vocabulary().schema
    assert set(schema["required"]) == {"is_chipotle_style", "meals_visible"}


def test_the_strict_schema_requires_every_property_the_api_will_see() -> None:
    """Strict structured output has no notion of an optional key.

    So "optional" is spelled "required, and may be null", which is the only
    difference between the two schemas -- and the reason there are two.
    """
    vocabulary = generated_vocabulary()
    strict = vocabulary.strict_schema()

    assert set(strict["required"]) == set(strict["properties"])
    assert strict["additionalProperties"] is False
    # The two the catalogue already required are untouched.
    assert strict["properties"]["is_chipotle_style"]["type"] == "boolean"
    assert strict["properties"]["meals_visible"]["type"] == "integer"
    # Everything else gained a null.
    assert strict["properties"]["protein"]["type"] == ["object", "null"]
    assert strict["properties"]["toppings"]["type"] == ["array", "null"]
    assert strict["properties"]["notes"]["type"] == ["string", "null"]


def test_the_strict_schema_sends_no_keyword_the_api_would_refuse() -> None:
    """Numeric bounds are on strict mode's unsupported list.

    A schema carrying one is refused outright, so leaving ``minimum`` in would
    break every photograph rather than loosen one check.
    """
    strict = generated_vocabulary().strict_schema()
    assert not _keywords(strict) & STRICT_UNSUPPORTED

    # And the bound is still in the catalogue's schema, where the validator
    # reads it -- it moved to our side of the wire rather than disappearing.
    schema = generated_vocabulary().schema
    assert schema["properties"]["meals_visible"]["minimum"] == 0
    assert schema["properties"]["vessel"]["properties"]["confidence"]["maximum"] == 1


def test_a_bound_the_api_cannot_enforce_is_enforced_on_the_way_in() -> None:
    with pytest.raises(SchemaViolationError, match="maximum"):
        generated_vocabulary().validate(
            {
                "is_chipotle_style": True,
                "meals_visible": 1,
                "vessel": {"value": "bowl", "confidence": 1.4},
            }
        )


def _keywords(schema: object) -> set[str]:
    """Every schema keyword anywhere in ``schema``."""
    if isinstance(schema, dict):
        found = set(schema)
        for value in schema.values():
            found |= _keywords(value)
        return found
    if isinstance(schema, list):
        return set().union(*(_keywords(member) for member in schema)) if schema else set()
    return set()


def test_the_strict_schema_still_carries_the_generated_enums() -> None:
    strict = generated_vocabulary({"protein": ("tofu",)}).strict_schema()
    assert strict["properties"]["protein"]["properties"]["value"]["enum"] == ["tofu"]


def test_the_response_format_asks_the_api_to_enforce_it() -> None:
    """Enforcement is the API's job. Parsing free text and hoping is what D3 removed."""
    fmt = generated_vocabulary().response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == generated_vocabulary().strict_schema()


def test_nulls_are_how_the_strict_schema_says_a_slot_was_not_filled() -> None:
    validated = generated_vocabulary().validate(
        {
            "is_chipotle_style": True,
            "meals_visible": 1,
            "vessel": {"value": "bowl", "confidence": 0.9},
            "protein": None,
            "rice": None,
            "beans": None,
            "salsas": None,
            "toppings": None,
            "notes": None,
        }
    )
    assert set(validated) == {"is_chipotle_style", "meals_visible", "vessel"}


# --- rejection, never coercion ----------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "is_chipotle_style": True,
        "meals_visible": 1,
        "vessel": {"value": "bowl", "confidence": 0.9},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        pytest.param(
            _payload(vessel={"value": "cauldron", "confidence": 0.9}),
            "vessel.value",
            id="off-catalogue-term",
        ),
        pytest.param(
            _payload(vessel={"value": "bowl", "confidence": 1.4}),
            "vessel.confidence",
            id="confidence-above-one",
        ),
        pytest.param(
            _payload(vessel={"value": "bowl", "confidence": -0.1}),
            "vessel.confidence",
            id="confidence-below-zero",
        ),
        pytest.param(
            _payload(vessel={"value": "bowl"}), "vessel", id="slot-without-confidence"
        ),
        pytest.param(
            _payload(vessel={"value": "bowl", "confidence": 0.9, "sku": "CMG-101"}),
            "vessel",
            id="extra-key-on-a-slot",
        ),
        pytest.param(_payload(item_name="Chicken Bowl"), "", id="extra-key-at-the-root"),
        pytest.param(_payload(meals_visible=-1), "meals_visible", id="negative-count"),
        pytest.param(_payload(meals_visible="one"), "meals_visible", id="count-as-text"),
        pytest.param(
            _payload(is_chipotle_style="yes"), "is_chipotle_style", id="boolean-as-text"
        ),
        pytest.param(
            _payload(vessel={"value": "bowl", "confidence": True}),
            "vessel.confidence",
            id="confidence-as-boolean",
        ),
        pytest.param(
            _payload(toppings={"value": "cheese", "confidence": 0.5}),
            "toppings",
            id="array-slot-as-object",
        ),
        pytest.param(
            _payload(
                toppings=[
                    {"value": "cheese", "confidence": 0.5},
                    {"value": "sprinkles", "confidence": 0.5},
                ]
            ),
            "toppings[1].value",
            id="off-catalogue-term-in-an-array",
        ),
        pytest.param({"meals_visible": 1}, "", id="missing-required-key"),
        pytest.param([], "", id="not-an-object"),
    ],
)
def test_a_schema_violating_response_is_rejected(payload: object, path: str) -> None:
    """Issue #53's first acceptance criterion, one row per way to break it.

    Every one of these has a plausible repair -- drop the extra key, clamp the
    confidence, pick the nearest term -- and every repair is a guess about a
    photograph made by something that never saw it. So none is performed.
    """
    with pytest.raises(SchemaViolationError) as refusal:
        generated_vocabulary().validate(payload)
    assert refusal.value.path == path


def test_a_violation_never_repeats_what_the_model_said() -> None:
    """The message names the slot, never the value.

    A fabricated product name that ended up in an exception message ends up in a
    log line, and D3 is about product names not surviving contact with the rest
    of the system.
    """
    with pytest.raises(SchemaViolationError) as refusal:
        generated_vocabulary().validate(
            _payload(vessel={"value": "Chicken Burrito Bowl", "confidence": 0.9})
        )
    assert "Chicken Burrito Bowl" not in str(refusal.value)


def test_a_valid_response_comes_back_unchanged() -> None:
    payload = _payload(
        toppings=[{"value": "cheese", "confidence": 0.5}],
        notes="Plenty of cheese on that one.",
    )
    assert generated_vocabulary().validate(payload) == payload


# --- failing closed on a schema we do not understand -------------------------


def test_a_schema_keyword_the_validator_cannot_check_is_a_build_error() -> None:
    """Not a rejection: a validator that skipped what it could not read would
    quietly stop checking while still returning valid-looking descriptions."""
    source = vocabulary_module_source().replace(
        '"type": "integer", "minimum": 0',
        '"type": "integer", "minimum": 0, "multipleOf": 2',
    )
    vocabulary = Vocabulary.from_module(vocabulary_module(source))
    with pytest.raises(VocabularyError, match="multipleOf"):
        vocabulary.validate(_payload())


def test_an_object_the_schema_leaves_open_is_a_build_error() -> None:
    source = vocabulary_module_source().replace(
        '"additionalProperties": False,\n    "required": ["is_chipotle_style"',
        '"required": ["is_chipotle_style"',
    )
    vocabulary = Vocabulary.from_module(vocabulary_module(source))
    with pytest.raises(VocabularyError, match="close every object"):
        vocabulary.validate(_payload())


def test_asking_for_a_slot_the_catalogue_has_no_column_for_is_a_key_error() -> None:
    vocabulary = generated_vocabulary()
    with pytest.raises(KeyError):
        vocabulary.values("dessert")
    with pytest.raises(KeyError):
        vocabulary.values("meals_visible")


def test_the_slot_items_travel_with_the_enums() -> None:
    """Stage 5's map and stage 4's enums come from one catalogue build.

    Stage 4 does not read this, but carrying it means the matcher and the
    describer cannot end up holding vocabularies from two different builds.
    """
    vocabulary: Vocabulary = generated_vocabulary()
    items: Mapping[str, Mapping[str, Sequence[str]]] = vocabulary.slot_items
    assert set(items) == set(DEFAULT_TERMS)
    assert set(items["toppings"]) == set(DEFAULT_TERMS["toppings"])
    # A vessel resolves to no item on its own -- it is half of an entree.
    assert items["vessel"]["bowl"] == ()
