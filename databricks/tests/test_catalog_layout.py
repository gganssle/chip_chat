"""The Python layout and the Terraform layout are the same layout.

`chip_chat.databricks.catalog` exists so pipelines do not retype schema names,
which only helps if the names it carries are the ones Terraform actually
creates. Nothing enforces that at runtime — a pipeline writing to a schema that
does not exist fails in Databricks, minutes into a run, with a message about a
missing schema rather than about a drifted constant.

So it is enforced here, by reading the Terraform. The parse is deliberately
crude: it matches the three `locals` maps the schema names are built from, and
it fails loudly if their shape changes, because a silent pass against a file
that no longer says what this test thinks it says would be worse than no test.
"""

import re
from pathlib import Path

import pytest

from chip_chat.databricks.catalog import (
    CATALOG,
    LAYERS,
    SCHEMAS,
    STREAMS,
    schema,
    schemas_for,
    table,
)

TERRAFORM = (
    Path(__file__).resolve().parents[2] / "infra" / "terraform" / "databricks_catalog.tf"
)


def _block(source: str, name: str) -> str:
    """Return the body of the ``name = { ... }` assignment in ``source``.

    Args:
        source: Terraform source.
        name: The attribute name to find.

    Returns:
        Everything between the braces, exclusive.

    Raises:
        AssertionError: If the assignment is absent or its braces do not close,
            which means this test is reading a file it no longer understands.
    """
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*\{{", source, re.MULTILINE)
    assert match, f"{name} is not assigned in {TERRAFORM.name}"

    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end() : index]
    raise AssertionError(f"the braces of {name} never close in {TERRAFORM.name}")


def _top_level_keys(block: str) -> set[str]:
    """Return the keys assigned at depth zero within ``block``."""
    keys = set()
    depth = 0
    for line in block.splitlines():
        stripped = line.strip()
        if depth == 0:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
            if match:
                keys.add(match.group(1))
        depth += line.count("{") - line.count("}")
    return keys


@pytest.fixture(scope="module")
def terraform() -> str:
    assert TERRAFORM.exists(), f"{TERRAFORM} is missing"
    return TERRAFORM.read_text()


def test_the_layers_match(terraform: str) -> None:
    assert _top_level_keys(_block(terraform, "uc_layers")) == set(LAYERS)


def test_the_streams_match(terraform: str) -> None:
    assert _top_level_keys(_block(terraform, "uc_streams")) == set(STREAMS)


def test_every_schema_terraform_creates_has_a_note_and_a_python_name(
    terraform: str,
) -> None:
    """`uc_schema_notes` is keyed by the six schema names, one per cross product.

    It is the one place in the Terraform where all six are spelled out rather
    than computed, which makes it the honest thing to compare against.
    """
    assert _top_level_keys(_block(terraform, "uc_schema_notes")) == {
        candidate.name for candidate in SCHEMAS
    }


def test_the_catalog_name_matches(terraform: str) -> None:
    """Terraform derives the catalog from `local.base` by swapping the hyphen.

    `local.base` is `chip-chat` for the demo environment, so the catalog is
    `chip_chat` — an unquoted SQL identifier, which `chip-chat` would not be.
    """
    assert 'uc_catalog = replace(local.base, "-", "_")' in terraform
    assert CATALOG == "chip_chat"


def test_there_are_six_schemas_and_no_duplicates() -> None:
    assert len(SCHEMAS) == len(LAYERS) * len(STREAMS)
    assert len({candidate.name for candidate in SCHEMAS}) == len(SCHEMAS)


def test_a_schema_names_itself_the_way_unity_catalog_does() -> None:
    bronze = schema("bronze", "harvested")
    assert bronze.name == "bronze_harvested"
    assert bronze.full_name == "chip_chat.bronze_harvested"
    assert str(bronze) == "chip_chat.bronze_harvested"
    assert bronze.table("menu_items") == "chip_chat.bronze_harvested.menu_items"


def test_table_qualifies_fully() -> None:
    assert table("gold", "synthetic", "customer_360") == (
        "chip_chat.gold_synthetic.customer_360"
    )


def test_schemas_for_yields_one_per_layer_in_order() -> None:
    assert [candidate.name for candidate in schemas_for("synthetic")] == [
        "bronze_synthetic",
        "silver_synthetic",
        "gold_synthetic",
    ]


@pytest.mark.parametrize(
    ("layer", "stream"),
    [("platinum", "harvested"), ("bronze", "real"), ("", "")],
)
def test_an_unknown_layer_or_stream_is_refused(layer: str, stream: str) -> None:
    """A notebook parameter arrives as a string that nothing type-checked."""
    with pytest.raises(ValueError, match=r"unknown (layer|stream)"):
        schema(layer, stream)  # type: ignore[arg-type]


def test_a_table_needs_a_name() -> None:
    with pytest.raises(ValueError, match="a table needs a name"):
        table("gold", "synthetic", "")
