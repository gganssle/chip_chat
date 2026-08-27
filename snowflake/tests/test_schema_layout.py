"""The DDL, `chip_chat.snowflake.schema` and RFC-001 §04 are the same schema.

Three descriptions of one thing, and this is where they are held together. The
DDL is what Snowflake runs, `schema.py` is what the rest of the tree reads, and
§04 is what issue #42 says both of them must match exactly. Free, offline, and
in `make ci` -- the live account is `chip_chat.snowflake.verify`'s question and
it needs a trial and a credential.

The test with the most value per line is
:func:`test_every_visitor_scoped_table_carries_demo_id`, and it is issue #42's
second acceptance criterion asked of the checked-in DDL. `sql/09_audit.sql`
asks the live account the same question. Neither replaces the other: this one
sees a table somebody wrote down and did not think about, and the view sees a
table nobody wrote down at all.

Two of these tests exist to fail on an *addition* rather than on a loss, which
is the unusual direction:
:func:`test_no_column_exists_that_nobody_argued_for` refuses a column that is
neither in §04 nor in :attr:`~chip_chat.snowflake.schema.Table.additions`, and
:func:`test_the_editable_columns_live_only_on_demo_visitors` refuses a second
home for any of the three fields a visitor may edit. Both are about a schema
growing quietly rather than about one being wrong today.
"""

import re
from itertools import pairwise

import pytest
from sql_text import Declared, declared_tables, flat, statements

from chip_chat.snowflake import account, schema
from chip_chat.snowflake.apply import ordered_files

DDL_FILES = ("06_catalogue.sql", "07_accounts.sql", "08_marts.sql")
"""The three files that create tables. `09_audit.sql` creates the two views."""


@pytest.fixture(scope="module")
def sql() -> dict[str, str]:
    """Return every numbered SQL file's text, keyed by filename."""
    return {path.name: path.read_text() for path in ordered_files()}


@pytest.fixture(scope="module")
def declared(sql: dict[str, str]) -> dict[str, Declared]:
    """Return every table the DDL declares, keyed by name."""
    found: dict[str, Declared] = {}
    for name in DDL_FILES:
        for table in declared_tables(sql[name]):
            found[table.name] = table
    return found


def test_the_ddl_parser_still_reads_the_files(declared: dict[str, Declared]) -> None:
    """A parser that matches nothing would make every test below vacuous."""
    assert len(declared) == len(schema.TABLES), (
        f"parsed {len(declared)} tables out of {list(DDL_FILES)} and "
        f"chip_chat.snowflake.schema declares {len(schema.TABLES)}. Either a "
        "table was added to one and not the other, or the DDL has been "
        "reformatted into a shape sql_text.declared_tables no longer "
        "understands -- in which case the checks below are proving nothing."
    )


# ---------------------------------------------------------------------------
# The schema, three ways
# ---------------------------------------------------------------------------


def test_every_table_is_created_in_its_own_schema(sql: dict[str, str]) -> None:
    """Each table is declared in the file that owns its schema, under a USE."""
    by_schema = {
        "CATALOGUE": "06_catalogue.sql",
        "ACCOUNTS": "07_accounts.sql",
        "MARTS": "08_marts.sql",
    }
    for table in schema.TABLES:
        source = sql[by_schema[table.schema]]
        assert f"USE SCHEMA {account.schema(table.schema)}" in flat(source), (
            f"{by_schema[table.schema]} does not select {table.schema}, so its "
            "unqualified CREATE statements would land wherever the session was"
        )
        assert f"CREATE OR ALTER TABLE {table.name} (" in flat(source), (
            f"{table.qualified()} is in schema.py but {by_schema[table.schema]} "
            "does not create it"
        )


def test_every_column_matches_the_ddl(declared: dict[str, Declared]) -> None:
    """Name, order, type and NOT NULL, for every column of every table."""
    for table in schema.TABLES:
        found = declared[table.name]
        expected = tuple(
            (column.name, column.sql_type, column.required) for column in table.columns
        )
        assert found.columns == expected, (
            f"{table.name} disagrees between the DDL and schema.py. The order "
            "is part of the comparison: it is the order a publish lands columns "
            "in and the order a reader meets them."
        )


def test_every_primary_key_matches_the_ddl(declared: dict[str, Declared]) -> None:
    """Snowflake does not enforce these. #45's semantic view still reads them."""
    for table in schema.TABLES:
        found = declared[table.name]
        assert found.key == table.key, (
            f"{table.name}'s PRIMARY KEY is {found.key} in the DDL and "
            f"{table.key} in schema.py. A text-to-SQL system reads the "
            "declared key to know what one row is; an unenforced constraint "
            "that is also wrong is worse than none."
        )


def test_every_rfc_column_survives(declared: dict[str, Declared]) -> None:
    """Issue #42's "the schema is fixed, match it exactly", as an assertion."""
    for table in schema.TABLES:
        missing = [name for name in table.rfc if name not in table.column_names()]
        assert not missing, (
            f"{table.name} has lost {missing}, which RFC-001 §04 prints. The "
            "agent's read tools query these columns by name."
        )


def test_no_column_exists_that_nobody_argued_for() -> None:
    """Every column is either §04's or an addition with a reason written down."""
    for table in schema.TABLES:
        argued = set(table.rfc) | {addition.column for addition in table.additions}
        assert set(table.column_names()) == argued, (
            f"{table.name}'s columns are not exactly RFC-001 §04's plus the "
            "declared additions. A column beyond §04 needs an entry in "
            "Table.additions saying what a serving layer without it cannot do."
        )
        for addition in table.additions:
            assert addition.column in table.column_names(), (
                f"{table.name} argues for {addition.column}, which is not a column of it"
            )
            assert len(addition.why) > 40, (
                f"{table.name}.{addition.column}'s reason is a placeholder"
            )


# ---------------------------------------------------------------------------
# demo_id -- issue #42's second acceptance criterion
# ---------------------------------------------------------------------------


def test_every_visitor_scoped_table_carries_demo_id(
    declared: dict[str, Declared],
) -> None:
    """And it is NOT NULL, which is the one constraint Snowflake enforces.

    A nullable demo_id is a row a policy cannot decide about: the comparison
    against the session variable is neither true nor false, so the row is
    filtered out and the data is invisible to everybody including its owner --
    a leak's quieter cousin, and one nobody reports.
    """
    for table in schema.visitor_scoped():
        found = declared[table.name]
        columns = {name: required for name, _, required in found.columns}
        assert schema.DEMO_ID in columns, (
            f"{table.qualified()} is visitor-scoped and has no {schema.DEMO_ID} "
            "column. #43 attaches a row access policy to that column; without "
            "it there is nothing to attach one to, and the isolation guarantee "
            "of RFC-001 §05 has a hole shaped exactly like this table."
        )
        assert columns[schema.DEMO_ID], (
            f"{table.qualified()}.{schema.DEMO_ID} is nullable"
        )


def test_the_only_tables_without_demo_id_are_the_two_exempted() -> None:
    """Default-deny, in the same direction the live audit view defaults."""
    exempt = {
        (table.schema, table.name) for table in schema.TABLES if not table.visitor_scoped
    }
    catalogue = {(table.schema, table.name) for table in schema.tables_in("CATALOGUE")}
    assert exempt - catalogue == set(schema.EXEMPT), (
        "a table in ACCOUNTS or MARTS is marked not visitor-scoped without an "
        "entry in schema.EXEMPT saying why. The catalogue is exempt wholesale "
        "-- it is the real half and has no visitor in it at all."
    )
    for (schema_name, name), why in schema.EXEMPT.items():
        assert schema.table(name).schema == schema_name
        assert len(why) > 40, f"{name}'s exemption has no argument behind it"


def test_the_audit_view_carries_the_same_exemptions(sql: dict[str, str]) -> None:
    """`09_audit.sql` and `schema.EXEMPT` name the same two tables.

    The live audit reads its exemptions out of its own SQL, so a table exempted
    in Python and not there would pass `make ci` and fail against the account --
    or, worse, the other way round.
    """
    source = sql["09_audit.sql"]
    for view in schema.AUDIT_VIEWS:
        assert f"CREATE OR REPLACE VIEW {view}" in flat(source), (
            f"09_audit.sql does not create {view}"
        )
    exempted = set(re.findall(r"^\s+'(ACCOUNTS|MARTS)',\n\s+'(\w+)',", source, re.M))
    assert exempted == {
        (schema_name, name.upper()) for schema_name, name in schema.EXEMPT
    }, (
        "09_audit.sql exempts a different set of tables from schema.EXEMPT. One "
        "of the two lets a table out of the demo_id rule that the other does not."
    )
    assert "'DEMO_ID'" in flat(source), (
        "the audit view no longer looks for a column called DEMO_ID"
    )


# ---------------------------------------------------------------------------
# The containment PRD Q2 rests on
# ---------------------------------------------------------------------------


def test_the_editable_columns_live_only_on_demo_visitors() -> None:
    """The three fields a visitor may change are on one table and no other.

    docs/decisions/persona-editing.md argues that this placement *is* the
    mechanism: marts are computed from orders, order_items and loyalty_ledger,
    so no editable field is an input to one and an edit cannot make a mart
    stale. A copy of `stated_preferences` on a table a nightly job reads would
    undo that quietly, which is why the assertion is about every other table
    rather than about this one.
    """
    visitors = schema.table("demo_visitors")
    for name in schema.EDITABLE_COLUMNS:
        assert name in visitors.column_names(), (
            f"{name} is not on demo_visitors, and PRD Q2's answer says it is"
        )
    for table in schema.TABLES:
        if table.name == "demo_visitors":
            continue
        overlap = set(schema.EDITABLE_COLUMNS) & set(table.column_names())
        assert not overlap, (
            f"{table.qualified()} carries {sorted(overlap)}, which a visitor "
            "may edit. Every mart is computed from "
            f"{list(schema.MART_INPUTS)} only, and that containment is what "
            "makes 'an edit cannot invalidate a mart' a property of the schema "
            "rather than a rule somebody has to remember."
        )


def test_the_mart_inputs_are_tables_and_demo_visitors_is_not_one() -> None:
    """The other half of the same argument, stated as data."""
    for name in schema.MART_INPUTS:
        assert schema.table(name).schema == "ACCOUNTS"
    assert "demo_visitors" not in schema.MART_INPUTS


# ---------------------------------------------------------------------------
# Comments -- which are retrieval quality, not documentation
# ---------------------------------------------------------------------------


def test_every_table_and_every_column_carries_a_comment(
    declared: dict[str, Declared],
) -> None:
    """Issue #42 asks for this, and says why: these feed #45's semantic view.

    A column Cortex Analyst has no description of is a column it will use
    anyway, on the strength of its name.
    """
    for table in schema.TABLES:
        found = declared[table.name]
        assert len(found.table_comment) > 80, (
            f"{table.name}'s table comment is a label rather than a "
            "description of what one row is"
        )
        for column in table.columns:
            comment = found.comments.get(column.name, "")
            assert len(comment) > 20, (
                f"{table.name}.{column.name} has no comment worth retrieving. "
                "Every column, including the obvious ones -- the semantic view "
                "reads them, and an absent description is not a shorter one."
            )


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def test_the_ddl_runs_after_the_grants_that_cover_it() -> None:
    """Numeric prefixes are load-bearing, and this is the ordering they encode.

    `03_grants.sql` grants ``SELECT`` on FUTURE TABLES in every schema, which
    is what makes these tables readable by the lane roles without anybody
    re-running a grants file. Future grants are not retroactive: created first,
    the tables would be reachable by CHIP_CHAT_ADMIN and nothing else.
    """
    order = [path.name for path in ordered_files()]
    assert order.index("03_grants.sql") < order.index("06_catalogue.sql")
    for earlier, later in pairwise(DDL_FILES):
        assert order.index(earlier) < order.index(later)
    assert order.index("08_marts.sql") < order.index("09_audit.sql"), (
        "the audit view reads INFORMATION_SCHEMA for both schemas and would be "
        "created against a MARTS that has no tables in it yet"
    )


def test_no_table_is_created_before_a_foreign_key_that_points_at_it(
    sql: dict[str, str],
) -> None:
    """A FOREIGN KEY needs its target to exist, so declaration order matters.

    This is why `demo_visitors` is created before `persona_fixtures` rather
    than in RFC-001 §04's reading order.
    """
    created: list[str] = []
    for name in DDL_FILES:
        for statement in statements(sql[name]):
            match = re.match(r"CREATE OR ALTER TABLE (\w+) \(", statement)
            if not match:
                continue
            for referenced in re.findall(
                r"REFERENCES (?:CHIP_CHAT\.\w+\.)?(\w+) \(", statement
            ):
                assert referenced in created or referenced == match.group(1), (
                    f"{match.group(1)} references {referenced}, which no "
                    "earlier statement has created"
                )
            created.append(match.group(1))
    assert len(created) == len(schema.TABLES)
