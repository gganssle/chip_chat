"""The chunk schema here is the chunk schema in gold, or this fails.

``chip_chat.search.chunks`` restates ``chip_chat.databricks.gold``'s ``FIELDS``
so that the index can be built without importing a Spark driver module — the
same convention ``gold.py`` itself uses for the constants it shares with
``silver.py``, and the same one ``silver.py`` uses for ``bronze.py``'s. What
makes that a convention rather than a duplicate is this file.

**While #35's chunk pipeline is not on ``main``, these skip with a reason.**
That is deliberate and it is temporary. ``chip_chat.databricks.gold`` on ``main``
today is #36's four marts; #35's chunk renderers are on
``polecat/mica/cc-zix``, and the two landed in a module of the same name, so the
merge queue has a rename to resolve before both can exist. The moment they do,
these assertions become live and any drift between the two schemas fails
``make ci``. Skipping is the right behaviour in the meantime and importing
optimistically is not: a contract test that cannot see the other side of the
contract has nothing to say, and saying it loudly is better than an import error
that reads like a broken package. Tracked as cc-6rb.
"""

import pytest

gold = pytest.importorskip(
    "chip_chat.databricks.gold", reason="the gold chunk pipeline (#35) is not merged"
)

if not hasattr(gold, "FIELDS"):  # pragma: no cover - depends on what merged
    pytest.skip(
        "chip_chat.databricks.gold is #36's marts, not #35's chunks: the chunk "
        "schema this package mirrors is not importable yet. See cc-6rb.",
        allow_module_level=True,
    )

from chip_chat.search import chunks  # noqa: E402 - after the skip, on purpose


def test_the_kinds_are_the_same_kinds() -> None:
    assert chunks.KINDS == gold.KINDS


def test_the_fields_are_the_same_fields_in_the_same_order() -> None:
    assert chunks.names() == tuple(entry.name for entry in gold.FIELDS)


def test_every_field_carries_the_same_type_and_flags() -> None:
    for here, there in zip(chunks.FIELDS, gold.FIELDS, strict=True):
        assert (
            here.name,
            here.sql_type,
            here.retrievable,
            here.filterable,
            here.facetable,
            here.kinds,
        ) == (
            there.name,
            there.sql_type,
            there.retrievable,
            there.filterable,
            there.facetable,
            there.kinds,
        )


def test_the_derived_lists_agree() -> None:
    assert chunks.retrievable() == gold.retrievable()
    assert chunks.filterable() == gold.filterable()
    assert chunks.facetable() == gold.facetable()
