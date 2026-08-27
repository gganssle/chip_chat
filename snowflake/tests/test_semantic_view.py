"""The semantic view, `chip_chat.snowflake.semantic` and the golden set agree.

Issue #45 in `make ci`, minus the parts that need a trial account. The DDL is
what Snowflake runs, `semantic.py` is what the rest of the tree reads, and
`eval/golden/cases.json` is the list of questions the account lane has to
answer -- three descriptions of one boundary, held together here.

The tests with the most value per line are the ones that fail on an ADDITION
rather than on a loss, because a semantic view does not go wrong by shrinking:

:func:`test_the_boundary_is_closed` refuses a table that is neither modelled
nor argued out of the model, so a fifteenth table in #42's DDL fails this
package until somebody decides whether the account lane may see it.

:func:`test_every_column_of_a_modelled_table_is_used_or_withheld` does the same
one level down, which is the level it actually gets lost at: `menu_items` is
here for two of its nine columns, and a calorie column that quietly became
reachable is the difference between the golden set's `a4-unanswerable-aggregate`
refusing and inventing.

:func:`test_no_verified_query_names_a_visitor` and its neighbours are #45's
fifth criterion asked of the checked-in SQL: demo_id appears in no element, no
relationship and no verified query, because isolation is #43's row access
policy and a model that mentioned the column would invite a generated query to
filter on an identifier no tool signature carries.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest
from sql_text import SemanticView, flat, semantic_view, statements

from chip_chat.snowflake import account, schema, semantic
from chip_chat.snowflake.apply import SQL_DIRECTORY, ordered_files

VIEW_FILE = "11_semantic_view.sql"
REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "eval" / "golden" / "cases.json"
POPULATION = REPO / "data-gen" / "src" / "chip_chat" / "data_gen" / "population.toml"


@pytest.fixture(scope="module")
def sql() -> str:
    """Return `sql/11_semantic_view.sql` as text."""
    return (SQL_DIRECTORY / VIEW_FILE).read_text()


@pytest.fixture(scope="module")
def view(sql: str) -> SemanticView:
    """Return the parsed ``CREATE SEMANTIC VIEW``."""
    return semantic_view(sql)


@pytest.fixture(scope="module")
def account_cases() -> list[dict]:
    """Return the golden-set cases that route to ``ask_account_question``."""
    cases = json.loads(GOLDEN.read_text())["cases"]
    return [case for case in cases if case.get("tool") == "ask_account_question"]


def test_the_semantic_view_parser_still_reads_the_file(view: SemanticView) -> None:
    """A parser that matched nothing would make every test below vacuous.

    It has happened once already in this file's short life: a ``COMMENT``
    reading "Present so that a single order can be quoted; do not sum it" ended
    the statement at the semicolon, and everything after the first fact
    silently disappeared.
    """
    assert view.name == semantic.VIEW_NAME
    assert len(view.tables) == len(semantic.LOGICAL_TABLES)
    assert len(view.elements) == len(semantic.ELEMENTS)
    assert len(view.verified) == len(semantic.VERIFIED_QUERIES)
    assert view.comment
    assert view.sql_generation
    assert view.question_categorization


# ---------------------------------------------------------------------------
# The view, two ways
# ---------------------------------------------------------------------------


def test_every_logical_table_matches_the_ddl(view: SemanticView) -> None:
    """Alias, physical table, key and synonyms, in declaration order."""
    found = {table.alias: table for table in view.tables}
    assert list(found) == [table.alias for table in semantic.LOGICAL_TABLES]
    for table in semantic.LOGICAL_TABLES:
        declared = found[table.alias]
        assert declared.table == account.table(table.schema, table.table), (
            f"{table.alias} stands for {declared.table} in the DDL and "
            f"{table.table} in semantic.py"
        )
        assert declared.key == table.key
        assert declared.synonyms == table.synonyms


def test_every_logical_key_is_the_tables_own_primary_key() -> None:
    """The view tells Cortex Analyst what one row is; the DDL already did.

    A semantic view may declare a key its table does not, and the two
    disagreeing is how a metric silently counts a fan-out.
    """
    for table in semantic.LOGICAL_TABLES:
        assert table.key == schema.table(table.table).key, (
            f"{table.alias} claims {table.key} and {table.table} declares "
            f"{schema.table(table.table).key}"
        )


def test_every_element_matches_the_ddl(view: SemanticView) -> None:
    """Kind, table, name, expression, USING and synonyms, in order."""
    found = {(element.table, element.name): element for element in view.elements}
    assert list(found) == [(element.table, element.name) for element in semantic.ELEMENTS]
    for element in semantic.ELEMENTS:
        declared = found[(element.table, element.name)]
        assert declared.kind == element.kind
        assert declared.expression == element.expression, (
            f"{element.qualified()} is {declared.expression!r} in the DDL and "
            f"{element.expression!r} in semantic.py"
        )
        assert declared.using == element.using
        assert declared.synonyms == element.synonyms


def test_every_relationship_matches_the_ddl(view: SemanticView) -> None:
    """Three joins, and the fourth one that is deliberately not declared."""
    assert view.relationships == tuple(
        (link.name, link.table, link.columns, link.references)
        for link in semantic.RELATIONSHIPS
    )
    assert not [link for link in semantic.RELATIONSHIPS if link.table == "points"], (
        "points joins to nothing on purpose: loyalty_ledger.order_id is null "
        "on a signup bonus and on an expiry, so a join through it drops the "
        "opening balance out of a question about points over a period"
    )


def test_every_metric_that_reaches_another_table_declares_the_relationship(
    view: SemanticView,
) -> None:
    """``USING`` is how an expression may name another logical table.

    Snowflake rejects a metric that reaches a physical column of another table
    -- ``invalid identifier 'ORDERS.STATUS'`` -- and accepts one that reaches a
    declared element through a named relationship. The two ``order_lines``
    metrics need the parent order's settled flag, and this is why.
    """
    names = {link.name for link in semantic.RELATIONSHIPS}
    for element in view.elements:
        for other in set(re.findall(r"\b(\w+)\.\w+", element.expression)):
            if other == element.table:
                continue
            assert element.using, (
                f"{element.table}.{element.name} names {other} with no USING "
                "clause, which Snowflake refuses"
            )
        for relationship in element.using:
            assert relationship in names


def test_every_table_and_every_element_carries_a_comment(view: SemanticView) -> None:
    """The comments ARE the retrieval, exactly as they are for the tables.

    `test_schema_layout.py` makes the same assertion about #42's DDL and gives
    the same reason: an element Cortex Analyst has no description of is one it
    will use anyway, on the strength of its name.
    """
    for table in view.tables:
        assert len(table.comment) > 80, (
            f"{table.alias}'s comment is a label rather than a description of "
            "what one row is"
        )
    for element in view.elements:
        assert len(element.comment) > 40, (
            f"{element.table}.{element.name} has no comment worth retrieving"
        )
        assert element.synonyms, (
            f"{element.table}.{element.name} has no synonyms. Visitors say "
            "'points' and 'rewards' and 'how much have I got', never 'delta' "
            "-- a field found only by its own name is one a question misses"
        )


# ---------------------------------------------------------------------------
# The boundary, which is the whole of #45
# ---------------------------------------------------------------------------


def test_the_boundary_is_closed() -> None:
    """Every table is modelled or argued out. There is no third state."""
    modelled = {(table.schema, table.table) for table in semantic.LOGICAL_TABLES}
    withheld = set(semantic.WITHHELD_TABLES)
    assert not modelled & withheld, "a table cannot be both modelled and withheld"
    every = {(table.schema, table.name) for table in schema.TABLES}
    assert modelled | withheld == every, (
        f"these tables are neither in the semantic view nor argued out of it: "
        f"{sorted(every - modelled - withheld)}. A table added to #42's DDL "
        "does not get to be undecided here -- the account lane either may see "
        "it or may not, and the reason is a string somebody has to write."
    )


def test_every_withheld_table_carries_an_argument() -> None:
    """A one-word exclusion is a decision nobody can re-examine."""
    for (schema_name, name), why in semantic.WITHHELD_TABLES.items():
        assert len(why) > 60, f"{schema_name}.{name} is excluded without an argument"


def test_every_column_of_a_modelled_table_is_used_or_withheld() -> None:
    """The level the boundary actually gets lost at.

    A table joins the model for one column and drags eight in behind it. Every
    column of every modelled table is therefore either withheld by name, or
    reachable -- named by an element's expression, by a relationship, or by the
    primary key that says what one row is.
    """
    used_in_joins = {
        (link.table, column) for link in semantic.RELATIONSHIPS for column in link.columns
    }
    for table in semantic.LOGICAL_TABLES:
        declared = schema.table(table.table)
        for column in declared.column_names():
            if (table.alias, column) in semantic.WITHHELD_COLUMNS:
                continue
            reachable = (
                column in declared.key
                or (table.alias, column) in used_in_joins
                or any(
                    re.search(rf"\b{table.alias}\.{column}\b", element.expression)
                    for element in semantic.ELEMENTS
                )
            )
            assert reachable, (
                f"{table.table}.{column} is in the semantic view's table and "
                "nothing names it -- so it is neither exposed on purpose nor "
                "withheld on purpose. Give it an element or put it in "
                "WITHHELD_COLUMNS with the reason."
            )


def test_the_columns_that_would_turn_an_account_answer_into_a_food_claim_are_out() -> (
    None
):
    """Named individually, because these are the ones that matter.

    The golden set's `a4-unanswerable-aggregate` -- "how many calories have i
    eaten here this year" -- is a case the account lane must refuse. The
    refusal is this absence and nothing else.
    """
    for column in ("calories", "allergens", "allergen_disclosure"):
        assert ("items", column) in semantic.WITHHELD_COLUMNS
        assert column not in semantic.exposed("items")


def test_no_element_names_a_visitor() -> None:
    """#45's fifth criterion: demo_id is nowhere in the model."""
    for element in semantic.ELEMENTS:
        assert "demo_id" not in element.expression.lower(), (
            f"{element.qualified()} names demo_id. Isolation is #43's row "
            "access policy on the base table; an element that exposed the "
            "column would invite a generated query to filter on an identifier "
            "no tool signature carries"
        )
    for link in semantic.RELATIONSHIPS:
        assert "demo_id" not in link.columns
    for table in semantic.LOGICAL_TABLES:
        if schema.table(table.table).visitor_scoped:
            assert (table.alias, "demo_id") in semantic.WITHHELD_COLUMNS


def test_no_verified_query_names_a_visitor(view: SemanticView) -> None:
    """And the same of the seven statements a person wrote by hand."""
    for query in view.verified:
        assert "demo_id" not in query.sql.lower(), (
            f"verified query {query.name} filters on demo_id. Every query here "
            "is written as though the visitor were the only person in the "
            "database, because the session cannot see anybody else"
        )


def test_no_verified_query_names_a_physical_table(view: SemanticView) -> None:
    """Measured, and expensive to find: physical SQL is silently dropped.

    A verified query whose SQL names ``CHIP_CHAT.ACCOUNTS.orders`` is accepted
    by CREATE, rewritten into a CTE per logical table that projects only the
    columns the rewriter thought were needed, and then discarded for the
    compilation error the rewrite causes -- while the request still succeeds
    and still reports the verified query as used. The warning appears in the
    response body and nowhere else.
    """
    for query in view.verified:
        assert account.DATABASE not in query.sql.upper(), (
            f"verified query {query.name} names a physical table. Write it "
            "against the logical model: __orders, and the element names"
        )
        for source in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", query.sql):
            if source.startswith("__"):
                assert source[2:] in {table.alias for table in semantic.LOGICAL_TABLES}, (
                    f"{query.name} reads {source}, which is not a logical table"
                )


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------


def test_the_verified_queries_match_the_ddl(view: SemanticView) -> None:
    """Name, question and onboarding flag, in declaration order."""
    found = {query.name: query for query in view.verified}
    assert list(found) == [query.name for query in semantic.VERIFIED_QUERIES]
    for query in semantic.VERIFIED_QUERIES:
        declared = found[query.name]
        assert declared.question == query.question
        assert declared.onboarding == query.onboarding
        assert declared.verified_at > 0, (
            f"{query.name} has no VERIFIED_AT, so nothing says when the person "
            "who verified it last looked at the data it runs against"
        )


def test_every_answerable_account_case_has_a_verified_query(
    account_cases: list[dict],
) -> None:
    """#45's second criterion, asked of the checked-in set.

    A golden-set case that routes to ``ask_account_question`` and is not a
    refusal case is a question the frequent path should not re-derive. The
    coverage is declared on the verified query rather than inferred from the
    text, so a renamed case fails here rather than quietly stopping being
    covered.
    """
    covered = {case_id for query in semantic.VERIFIED_QUERIES for case_id in query.covers}
    for case in account_cases:
        if "declines" in case.get("checks", ()):
            continue
        assert case["id"] in covered, (
            f"golden case {case['id']} -- {case['message']!r} -- routes to "
            "ask_account_question and no verified query claims it. Either add "
            "one, or add the case to UNANSWERABLE and say what it would need."
        )


def test_every_claimed_golden_case_exists(account_cases: list[dict]) -> None:
    """The other direction: a verified query cannot claim a case that is gone."""
    known = {case["id"] for case in account_cases}
    for query in semantic.VERIFIED_QUERIES:
        for case_id in query.covers:
            assert case_id in known, (
                f"verified query {query.name} claims golden case {case_id}, "
                "which no longer routes to ask_account_question"
            )


def test_the_refusal_set_covers_the_golden_refusal_case(
    account_cases: list[dict],
) -> None:
    """Every golden case checked for `declines` is in the unanswerable set."""
    refused = {case.golden_case for case in semantic.UNANSWERABLE}
    for case in account_cases:
        if "declines" in case.get("checks", ()):
            assert case["id"] in refused, (
                f"golden case {case['id']} expects a refusal and UNANSWERABLE "
                "does not carry it, so nothing here says why it is refused"
            )


def test_every_refusal_names_something_that_is_actually_withheld() -> None:
    """A refusal set that names a column the model exposes is a set that lies."""
    tables = {name for _, name in semantic.WITHHELD_TABLES}
    columns = {f"{alias}.{column}" for alias, column in semantic.WITHHELD_COLUMNS}
    for case in semantic.UNANSWERABLE:
        assert case.withheld or case.instructed, (
            f"{case.question!r} is in the refusal set with nothing withheld "
            "and no instruction behind it, which makes it a hope"
        )
        for name in case.withheld:
            assert name in tables or name in columns, (
                f"{case.question!r} says it needs {name}, which is neither a "
                "withheld table nor a withheld column -- so either the model "
                "does carry it, or the name is stale"
            )
        assert len(case.needs) > 30


def test_the_instructed_refusals_are_the_ones_the_schema_cannot_make(
    view: SemanticView,
) -> None:
    """Three of ten rest on the model's own instructions, and are marked so.

    A question about the population is not refused by an absent table -- the
    tables are there and a row access policy makes them look like one person.
    That failure is a right join reported under a plural noun, and nothing in
    the schema prevents it, so ``AI_QUESTION_CATEGORIZATION`` has to.
    """
    instructed = [case for case in semantic.UNANSWERABLE if case.instructed]
    assert instructed, "the weaker half of the refusal set has gone missing"
    lowered = view.question_categorization.lower()
    for word in ("population", "average", "future"):
        assert word in lowered, (
            f"AI_QUESTION_CATEGORIZATION does not mention {word!r}, and "
            f"{len(instructed)} refusals in UNANSWERABLE rest on it saying so"
        )


def test_the_instructions_say_the_two_things_only_they_can(view: SemanticView) -> None:
    """The settled rule and the demo_id prohibition, in the model's own words."""
    generation = view.sql_generation.lower()
    assert semantic.SETTLED_STATUS.lower() in generation
    assert "demo_id" in generation, (
        "AI_SQL_GENERATION does not tell the model never to filter on demo_id. "
        "The column is not in the view, but it IS on the physical tables the "
        "verified path reaches, so the instruction is the belt to the "
        "schema's braces"
    )
    assert "calorie" in view.question_categorization.lower()


# ---------------------------------------------------------------------------
# Agreement with the rest of the tree
# ---------------------------------------------------------------------------


def test_the_settled_status_is_the_one_the_population_was_generated_with() -> None:
    """`gold.py` copies this too, and a third copy that drifts is two answers.

    `population.toml` is the origin: the statuses the generator emits and the
    subset of them that earns loyalty points. Read here rather than imported,
    because this package does not depend on `chip_chat.data_gen` and should not
    start to for one string.
    """
    config = tomllib.loads(POPULATION.read_text())
    settled = tuple(config["orders"]["settled_statuses"])
    assert settled == (semantic.SETTLED_STATUS,), (
        f"population.toml settles {settled} and the semantic view settles "
        f"{(semantic.SETTLED_STATUS,)}. Every gold mart counts the first; a "
        "view counting the second puts two numbers called lifetime_spend in "
        "one conversation"
    )
    assert semantic.SETTLED_STATUS in config["orders"]["statuses"]


def test_the_settled_rule_is_in_the_facts_and_not_left_to_a_generated_where() -> None:
    """No money metric may aggregate a raw column."""
    for element in semantic.ELEMENTS:
        if element.kind != "METRIC" or element.table != "orders":
            continue
        assert "settled" in element.expression, (
            f"{element.qualified()} aggregates something other than a settled "
            "fact, so a cancelled order is spend in whatever it answers"
        )


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def test_the_view_is_created_after_every_table_it_reads() -> None:
    """A semantic view needs its tables to exist, so the prefix is load-bearing."""
    order = [path.name for path in ordered_files()]
    assert VIEW_FILE in order, (
        "11_semantic_view.sql is not in the numbered sequence, so "
        "`make snowflake-apply` does not create the account lane at all"
    )
    for earlier in ("03_grants.sql", "06_catalogue.sql", "07_accounts.sql"):
        assert order.index(earlier) < order.index(VIEW_FILE)


def test_the_view_is_replaced_with_copy_grants(view: SemanticView, sql: str) -> None:
    """Without it, every routine apply revokes the read role's access.

    CREATE OR REPLACE drops the grants on the object it replaces and a future
    grant does not re-apply to a replaced object, so the account lane would go
    dark on the next apply and stay dark until somebody re-ran a grants file.
    """
    assert view.copy_grants, "11_semantic_view.sql has lost its COPY GRANTS"
    assert (
        f"GRANT SELECT ON SEMANTIC VIEW {semantic.qualified()} TO ROLE CHIP_CHAT_READ"
        in flat(sql)
    ), (
        "nothing grants the read role SELECT on the view, so the first apply "
        "after a reset -- where COPY GRANTS has nothing to copy -- leaves it "
        "unreachable"
    )


def test_the_view_is_granted_to_the_read_role_and_to_no_other_lane(sql: str) -> None:
    """The account lane is a question, and questions are CHIP_CHAT_READ's."""
    for statement in statements(sql):
        match = re.fullmatch(
            r"GRANT (?P<privileges>.+?) ON SEMANTIC VIEW [\w.]+ TO ROLE (?P<role>\w+)",
            statement,
        )
        if match:
            assert match.group("role") == "CHIP_CHAT_READ", (
                f"{match.group('role')} is granted the semantic view. Only the "
                "read role asks questions; the ops API and the publisher have "
                "no business generating SQL"
            )


def test_the_cortex_role_is_granted_and_cross_region_is_narrowed(sql: str) -> None:
    """Two account-level facts this file owns, and both are easy to lose.

    Cortex Analyst needs SNOWFLAKE.CORTEX_USER on the role that asks. It is
    granted to PUBLIC by default, which is a default an administrator can
    revoke -- and the failure is a lane that stops answering rather than an
    error anybody connects to a grant.

    The account is AWS us-east-2, where Cortex Analyst is not native (#104), so
    it runs by cross-region inference. AWS_US is the narrowest setting under
    which it works at all, and narrowing is what an apply is allowed to do.
    """
    text = flat(sql)
    assert "GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE CHIP_CHAT_READ" in text
    assert (
        "ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = "
        f"'{account.CORTEX_CROSS_REGION}'" in text
    ), (
        "the file and account.CORTEX_CROSS_REGION disagree about where "
        "inference may run, and the account is in "
        f"{account.REGION}, where Cortex Analyst is not native"
    )
