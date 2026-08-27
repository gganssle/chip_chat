"""Issue #43's coverage requirement, asked of the checked-in SQL.

RFC-001 §05 is enforced by row access policies on every visitor-scoped table,
and the failure mode of that design is not a wrong policy -- it is a table
that quietly never got one. So the interesting test here is
:func:`test_every_visitor_scoped_table_is_attached_to_its_policy`, and it fails
in both directions: a table declared visitor-scoped with no attachment in
`sql/10_policies.sql`, and an attachment for a table nobody declared
visitor-scoped. That is the ticket's "a test that adds a new visitor-scoped
table without a policy and fails CI, so the guarantee does not decay as the
schema grows".

Free, offline, in `make ci`. `chip_chat.snowflake.verify` asks the live account
the same question and then proves the question is still being asked, by
creating an unprotected table and requiring the check to name it -- a coverage
check that has stopped looking reports a clean account forever, which is the
worst thing a security check can do.

The rest of the file is about the two properties the policy bodies have to
keep, both of which one OR clause could undo while reading like a convenience:

:func:`test_the_isolation_policy_denies_an_unbound_session` -- default deny is
written into the body rather than inherited from three-valued logic.

:func:`test_no_policy_body_exempts_a_lane_role` -- the ops API is not exempt,
and neither is the read lane. The only role either body may name is the owner
role, which no service user holds and which can detach the policy outright.
"""

import re

import pytest
from sql_text import flat, statements

from chip_chat.snowflake import account, schema
from chip_chat.snowflake.apply import ordered_files

POLICY_FILE = "10_policies.sql"
"""The file that creates both policies and attaches every one of them."""

_ATTACHMENT = re.compile(
    r"^\s+\('(?P<schema>ACCOUNTS|MARTS)',\s+'(?P<table>\w+)',\s+"
    r"'CHIP_CHAT\.ACCOUNTS\.(?P<policy>\w+)'\),?$",
    re.MULTILINE,
)
"""One row of the attachment list `10_policies.sql` drives its ALTERs from.

Parsed rather than executed, for the same reason `test_schema_layout.py` parses
the DDL: the live account is `verify`'s question and it needs a trial and a
credential, and a guarantee that is only checkable against a running account is
one that is checked once.
"""


@pytest.fixture(scope="module")
def sql() -> dict[str, str]:
    """Return every numbered SQL file's text, keyed by filename."""
    return {path.name: path.read_text() for path in ordered_files()}


@pytest.fixture(scope="module")
def source(sql: dict[str, str]) -> str:
    """Return `10_policies.sql`."""
    return sql[POLICY_FILE]


@pytest.fixture(scope="module")
def bodies(source: str) -> dict[str, str]:
    """Return each policy's body, as ``ALTER ... SET BODY`` writes it.

    The body is read out of the ALTER rather than out of the CREATE on purpose.
    The CREATE says ``FALSE`` -- Snowflake refuses ``CREATE OR REPLACE`` on a
    policy that is attached to anything, so the real expression has to arrive by
    ALTER, and a policy created denying everything leaves a half-run apply
    closed rather than open.
    """
    found: dict[str, str] = {}
    for statement in statements(source):
        match = re.fullmatch(
            r"ALTER ROW ACCESS POLICY (?P<name>\w+) SET BODY -> (?P<body>.+)",
            statement,
        )
        if match:
            found[match.group("name")] = match.group("body")
    return found


@pytest.fixture(scope="module")
def attachments(source: str) -> dict[tuple[str, str], str]:
    """Return ``(schema, table) -> policy`` for every attachment in the file."""
    return {
        (match.group("schema"), match.group("table")): match.group("policy")
        for match in _ATTACHMENT.finditer(source)
    }


def test_the_parsers_still_read_the_file(
    bodies: dict[str, str], attachments: dict[tuple[str, str], str]
) -> None:
    """A parser that matches nothing would make every test below vacuous."""
    assert set(bodies) == {policy.name for policy in schema.POLICIES}, (
        f"parsed the bodies of {sorted(bodies)} out of {POLICY_FILE} and "
        f"chip_chat.snowflake.schema declares "
        f"{sorted(policy.name for policy in schema.POLICIES)}. Either a policy "
        "was added to one and not the other, or the file has been reformatted "
        "into a shape this test no longer understands -- in which case the "
        "checks below are proving nothing."
    )
    assert attachments, (
        f"parsed no attachments out of {POLICY_FILE}. Every check about which "
        "tables are protected is now vacuously true."
    )


# ---------------------------------------------------------------------------
# Coverage -- the criterion this file exists for
# ---------------------------------------------------------------------------


def test_every_visitor_scoped_table_is_attached_to_its_policy(
    attachments: dict[tuple[str, str], str],
) -> None:
    """Both directions, and the second one is the half that is easy to lose.

    A visitor-scoped table with no attachment is the isolation guarantee with a
    hole in it shaped exactly like that table. An attachment for a table nobody
    declared visitor-scoped is the opposite mistake and just as worth failing
    on: it means the two descriptions of what a visitor owns have come apart,
    and only one of them is the one Snowflake enforces.
    """
    declared = {
        (table.schema, table.name.upper()): table.policy.upper()
        for table in schema.visitor_scoped()
    }
    found = {key: value.upper() for key, value in attachments.items()}

    unprotected = sorted(key for key in declared if key not in found)
    assert not unprotected, (
        f"{unprotected} are visitor-scoped in chip_chat.snowflake.schema and "
        f"{POLICY_FILE} attaches no row access policy to them. Every row of "
        "each is readable by every visitor, and nothing else in this tree "
        "would have said so."
    )
    unexpected = sorted(key for key in found if key not in declared)
    assert not unexpected, (
        f"{POLICY_FILE} attaches a policy to {unexpected}, which "
        "chip_chat.snowflake.schema does not declare visitor-scoped. One of "
        "the two is wrong about what a row of that table is."
    )
    assert found == declared, (
        "a visitor-scoped table is attached to a different policy than the one "
        f"it declares: {sorted(key for key in declared if found[key] != declared[key])}"
    )


def test_every_attachment_names_the_demo_id_column(source: str) -> None:
    """A policy is attached ``ON (demo_id)`` and there is no other option.

    A row access policy filters ONE table against its arguments and cannot
    follow a join, which is why `07_accounts.sql` carries demo_id down onto
    order_items rather than reaching the visitor through orders. Attaching on
    any other column would be attaching on something that is not the visitor.
    """
    assert f"ON ({schema.DEMO_ID})" in flat(source), (
        f"no attachment in {POLICY_FILE} names the {schema.DEMO_ID} column"
    )
    others = re.findall(r"ADD ROW ACCESS POLICY [\w.'| ]+ ON \((\w+)\)", flat(source))
    assert set(others) == {schema.DEMO_ID}, (
        f"{POLICY_FILE} attaches a policy on {sorted(set(others))}. The column "
        "a policy compares against is the one that says whose row it is, and "
        f"that is {schema.DEMO_ID} on every table in CHIP_CHAT."
    )


def test_no_policy_guards_a_table_the_demo_id_rule_exempts(
    attachments: dict[tuple[str, str], str],
) -> None:
    """`personas` and `item_affinity` are about nobody, and stay readable.

    A policy on either would not tighten anything -- there is no visitor in
    them to keep apart -- and would make the only personalization input a
    visitor with no history has return nothing at all.
    """
    for schema_name, name in schema.EXEMPT:
        assert (schema_name, name.upper()) not in attachments, (
            f"{schema_name}.{name} is exempt from the demo_id rule and "
            f"{POLICY_FILE} attaches a row access policy to it anyway. It has "
            "no demo_id column to compare, so the policy filters every row out"
        )


def test_the_catalogue_is_not_touched(attachments: dict[tuple[str, str], str]) -> None:
    """The real half has no visitor in it, and a menu is the same for everybody."""
    assert not [key for key in attachments if key[0] == "CATALOGUE"], (
        "a row access policy on CHIP_CHAT.CATALOGUE would scope the published "
        "menu to a visitor, which is not a thing a menu is"
    )


# ---------------------------------------------------------------------------
# Default deny -- "the failure mode that turns a bug into a breach"
# ---------------------------------------------------------------------------


def test_the_isolation_policy_denies_an_unbound_session(bodies: dict[str, str]) -> None:
    """Written into the body rather than inherited from three-valued logic.

    ``row_demo_id = GETVARIABLE('DEMO_ID')`` already denies an unbound session,
    because an unset variable is NULL and a comparison against NULL is NULL
    rather than TRUE. That is correct, and it is also a fact a reviewer has to
    know before the body is safe to review. The redundant IS NOT NULL is what
    makes the most important property of the mechanism legible in the policy
    instead of a consequence of it.
    """
    body = bodies[schema.ISOLATION_POLICY]
    assert f"GETVARIABLE('{schema.SESSION_VARIABLE}') IS NOT NULL" in body, (
        f"{schema.ISOLATION_POLICY} no longer requires "
        f"{schema.SESSION_VARIABLE} to be set before it returns anything. An "
        "unset session variable must return zero rows and never all of them: "
        "that is the difference between a connection the pool forgot to bind "
        "and a disclosure of every visitor in the database."
    )
    assert f"row_demo_id = GETVARIABLE('{schema.SESSION_VARIABLE}')" in body, (
        f"{schema.ISOLATION_POLICY} does not compare the row's visitor against "
        f"{schema.SESSION_VARIABLE}"
    )


def test_exactly_one_policy_is_open_when_nothing_is_bound() -> None:
    """And it guards exactly one table, and its argument is written down."""
    inverted = [policy for policy in schema.POLICIES if policy.open_when_unbound]
    assert [policy.name for policy in inverted] == [schema.ROSTER_POLICY], (
        "a second row access policy returns rows to a session that has bound "
        "no visitor. There is one such table in this database and the "
        "inversion is argued in sql/10_policies.sql; a second one needs the "
        "same argument made again, in Policy.why, and made convincingly."
    )
    guarded = [
        table.qualified()
        for table in schema.visitor_scoped()
        if table.policy == schema.ROSTER_POLICY
    ]
    assert guarded == ["CHIP_CHAT.ACCOUNTS.persona_fixtures"], (
        f"{schema.ROSTER_POLICY} guards {guarded}. It is open to an unbound "
        "session, so every table on that list is readable by anything holding "
        "a connection the pool has not bound yet."
    )
    for policy in schema.POLICIES:
        assert len(policy.why) > 40, (
            f"{policy.name}'s reason is a placeholder rather than an argument"
        )


def test_the_roster_policy_still_binds_a_bound_session(bodies: dict[str, str]) -> None:
    """The inversion is the unbound case ONLY.

    Open when nothing is bound is what entry needs. Open to everybody would be
    a table on which one visitor reads another's balance, which is the thing
    this whole file exists to make impossible.
    """
    body = bodies[schema.ROSTER_POLICY]
    assert f"row_demo_id = GETVARIABLE('{schema.SESSION_VARIABLE}')" in body, (
        f"{schema.ROSTER_POLICY} no longer narrows a bound session to its own "
        "fixture, which makes it a policy that permits everything"
    )
    assert f"GETVARIABLE('{schema.SESSION_VARIABLE}') IS NULL" in body, (
        f"{schema.ROSTER_POLICY} no longer opens the unbound case, which is "
        "the only reason it exists rather than visitor_isolation"
    )


# ---------------------------------------------------------------------------
# Nobody is exempt -- issue #43, "the ops API is not exempt"
# ---------------------------------------------------------------------------


def test_no_policy_body_exempts_a_lane_role(bodies: dict[str, str]) -> None:
    """One OR clause is all it would take, and it would read like a fix.

    Snowflake has no owner exemption: a row access policy filters the table for
    whoever reads it. The only way a role gets out from under one is a clause
    in the body naming it, so that is what this test refuses. Issue #41's third
    criterion -- the write role cannot read another visitor's rows either -- is
    a property of this assertion plus the grants file.
    """
    for name, body in bodies.items():
        for role in account.LANE_ROLES:
            assert role not in body, (
                f"{name} names {role} in its body. A lane role that appears in "
                "a policy body is a lane role the policy has stopped applying "
                "to, and CHIP_CHAT_READ is what Cortex Analyst runs as over "
                "SQL a language model wrote."
            )


def test_the_only_escape_needs_the_owner_role_and_a_second_variable(
    bodies: dict[str, str],
) -> None:
    """The maintenance clause, and the two things it takes to reach it.

    `chip_chat.snowflake.load` counts the rows it has just landed, which is a
    question about every visitor at once, and issue #47's nightly reset will
    ask a similar one. Neither can be answered by a session bound to one
    visitor, so an escape has to exist somewhere; what matters is what it costs
    to reach.

    The role is the first half and gives nothing away: CHIP_CHAT_ADMIN already
    holds APPLY ROW ACCESS POLICY, which no other role does, so it can detach
    this policy outright -- a role that can remove the lock is not further
    empowered by being handed a key. No service user holds it and
    `04_users.sql` sets DEFAULT_SECONDARY_ROLES = (), so no session picks it up
    by accident.

    The variable is the second half and is what keeps default deny true for
    every role including the owner: the escape is the PRESENCE of ALL_VISITORS,
    not the absence of DEMO_ID, so an owner session that simply forgot to bind
    a visitor reads zero rows exactly as a lane role does.
    """
    body = bodies[schema.ISOLATION_POLICY]
    assert f"CURRENT_ROLE() = '{account.ADMIN_ROLE}'" in body, (
        "the maintenance clause no longer requires the owner role, or requires "
        "it by some other means. IS_ROLE_IN_SESSION is true for secondary "
        "roles and for roles reached through the hierarchy, which is a wider "
        "door than a maintenance escape needs"
    )
    assert f"GETVARIABLE('{schema.MAINTENANCE_VARIABLE}') IS NOT NULL" in body, (
        f"the maintenance clause no longer requires {schema.MAINTENANCE_VARIABLE}. "
        "Without it the owner role reads every visitor by default, and 'an "
        "unset session variable returns zero rows' stops being true of the "
        "role that runs every load and every rebuild"
    )
    assert schema.MAINTENANCE_VARIABLE not in bodies[schema.ROSTER_POLICY], (
        f"{schema.ROSTER_POLICY} has grown a maintenance escape it does not "
        "need -- an unbound session already reads the whole roster"
    )


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def test_the_policies_are_attached_after_the_tables_exist() -> None:
    """Numeric prefixes are load-bearing here as everywhere else in this directory."""
    order = [path.name for path in ordered_files()]
    for earlier in ("03_grants.sql", "07_accounts.sql", "08_marts.sql"):
        assert order.index(earlier) < order.index(POLICY_FILE), (
            f"{POLICY_FILE} runs before {earlier}, so it would attach a policy "
            "to a table that does not exist yet and the apply would stop there"
        )


def test_a_policy_is_never_replaced_while_it_is_attached(source: str) -> None:
    """Which is why the body arrives by ALTER and the CREATE denies everything.

    Snowflake refuses ``CREATE OR REPLACE ROW ACCESS POLICY`` on a policy that
    is attached to a table, so a file that re-asserted the body that way would
    work exactly once and fail on every apply afterwards. The created body is
    ``FALSE`` for a second reason: an apply that dies between the CREATE and the
    ALTER leaves the account closed rather than open.
    """
    assert "CREATE OR REPLACE ROW ACCESS POLICY" not in flat(source), (
        f"{POLICY_FILE} replaces a policy. Snowflake refuses that once the "
        "policy is attached to anything, so the second apply fails"
    )
    for policy in schema.POLICIES:
        assert (
            f"CREATE ROW ACCESS POLICY IF NOT EXISTS {policy.name} "
            "AS (row_demo_id VARCHAR) RETURNS BOOLEAN -> FALSE" in flat(source)
        ), (
            f"{policy.name} is not created denying everything. The window "
            "between creating a policy and setting its body should be a window "
            "in which nothing is readable, not one in which everything is"
        )


def test_the_attachment_is_a_single_atomic_alter(source: str) -> None:
    """Re-attaching detaches and adds in one statement, never in two.

    ``ALTER TABLE ... ADD ROW ACCESS POLICY`` fails if the table already has
    one and ``DROP`` fails if it does not, so a re-runnable file has to choose
    per table -- which is what the scripting block does. The branch for a table
    that is already protected is the DROP and the ADD in one ALTER, because the
    two-statement version leaves a window in which the table is unprotected,
    and a window is a thing somebody eventually queries through.
    """
    body = flat(source)
    assert "DROP ROW ACCESS POLICY ' || w.policy_name || ','" in body, (
        "the re-attachment is no longer the single-statement DROP+ADD swap"
    )
    for statement in statements(source):
        assert not statement.startswith("ALTER TABLE"), (
            "an attachment is written as a bare ALTER TABLE. That statement "
            "fails on whichever of the two runs -- first or repeat -- it was "
            "not written for, and every file under snowflake/sql is re-runnable"
        )
