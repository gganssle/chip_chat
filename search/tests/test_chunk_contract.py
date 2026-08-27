"""The chunk schema here is the chunk schema in gold, or this fails.

``chip_chat.search.chunks`` restates ``chip_chat.databricks.gold_chunks``'
``FIELDS`` so that the index can be built without importing a Spark driver
module — the same convention ``gold_chunks.py`` itself uses for the constants it
shares with ``silver.py``, and the same one ``silver.py`` uses for
``bronze.py``'s. What makes that a convention rather than a duplicate is this
file.

**These assertions used to skip, and the skip is the reason this docstring is
long.** #48 built the index against #35's chunk schema while #35 was on an
unmerged branch, and the two had landed in a module of the same name — ``gold``
was #36's four marts here and #35's chunk renderers there — so there was nothing
importable to compare against and the test skipped with that reason. What it
cost was measurable: the search index was being built against a chunk schema
nothing verified it matched, and a skipped test reports as a pass at the summary
line. #35 has since landed as :mod:`chip_chat.databricks.gold_chunks`, which is
the rename the merge queue was waiting on, so the import below is direct and any
drift between the two schemas fails ``make ci``. That was cc-6rb.
"""

from chip_chat.databricks import gold_chunks as gold
from chip_chat.search import chunks


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
