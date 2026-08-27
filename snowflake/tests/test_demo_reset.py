"""`sql/14_demo_reset.sql`, `chip_chat.snowflake.reset` and issue #47 agree.

Three descriptions of one job, held together here. The SQL is what Snowflake
runs, `reset.py` is what the rest of the tree reads, and #47 plus #9's decision
record are what both of them implement. Free, offline, and in `make ci`; whether
the live account has the task and whether a run actually ages anybody out is
`chip_chat.snowflake.verify`'s question and it needs a trial and a credential.

The test with the most value per line is
:func:`test_no_delete_can_reach_generated_history`, and it is about a predicate.
Every ``DELETE`` this job runs is on a table holding eighteen months of
generated orders underneath the live identifier band, and a ``DELETE`` that lost
its band predicate would empty a persona, leave a plausible row count behind it
and produce exactly the cold-start failure #9 decided against. Reviewing for
that is reviewing for an absence, which is what this file is for.

Two of these fail on an *addition* rather than on a loss.
:func:`test_the_reset_writes_no_column_nobody_argued_for` refuses a new column
in the restore list, and :func:`test_the_reset_touches_no_mart_and_no_catalogue`
refuses a new table in the job at all -- #47 says the catalogue and the gold
marts belong to the nightly publish, and "does not touch them" is a claim about
statements that do not exist.
"""

import re

import pytest
from sql_text import DeclaredProcedure, declared_procedures, flat, statements

from chip_chat.snowflake import account, procedures, reset, schema
from chip_chat.snowflake.apply import ordered_files

RESET_FILE = "14_demo_reset.sql"
"""The one file that creates the reset. It creates nothing else, so removing
the feature is deleting it, dropping two objects and deleting `reset.py`."""


@pytest.fixture(scope="module")
def sql() -> dict[str, str]:
    """Return every numbered SQL file's text, keyed by filename."""
    return {path.name: path.read_text() for path in ordered_files()}


@pytest.fixture(scope="module")
def source(sql: dict[str, str]) -> str:
    """Return `14_demo_reset.sql`."""
    assert RESET_FILE in sql, (
        f"{RESET_FILE} is not among the numbered files an apply runs, so the "
        "nightly reset would never be created. `apply.ordered_files()` globs "
        "the directory, so this means the file is gone or renamed."
    )
    return sql[RESET_FILE]


@pytest.fixture(scope="module")
def declared(source: str) -> DeclaredProcedure:
    """Return the reset procedure as the DDL declares it."""
    found = declared_procedures(source)
    assert len(found) == 1, (
        f"parsed {[item.name for item in found]} out of {RESET_FILE} and "
        "expected exactly one procedure. Either a second one moved in -- which "
        "makes deleting this feature something other than deleting a file -- "
        "or the file has been reformatted into a shape sql_text no longer "
        "reads, in which case every check below is proving nothing."
    )
    assert found[0].name == reset.PROCEDURE
    return found[0]


def _dml(body: str, verb: str) -> list[str]:
    """Return the statements of ``body`` that start with ``verb``.

    Read off the procedure body rather than off the file, because a reset's
    interesting statements are all inside the ``$$`` and the comments between
    them are part of what is under test.
    """
    uncommented = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("--")
    )
    flattened = [re.sub(r"\s+", " ", part).strip() for part in uncommented.split(";")]
    return [part for part in flattened if part.upper().startswith(verb)]


# ---------------------------------------------------------------------------
# What it may delete, and what it must never reach
# ---------------------------------------------------------------------------


def test_no_delete_can_reach_generated_history(declared: DeclaredProcedure) -> None:
    """Every DELETE is scoped to one visitor, and to the live band or nothing.

    `reset.PRESERVED_BELOW_BAND` is what is underneath the band and why it
    matters. A DELETE on `orders` without the predicate would take eighteen
    months of a persona's history with it, which no count taken afterwards
    would look wrong.
    """
    deletes = _dml(declared.body, "DELETE")
    assert deletes, "the reset deletes nothing, which is not a reset"
    for statement in deletes:
        match = re.match(r"DELETE FROM CHIP_CHAT\.ACCOUNTS\.(\w+)", statement)
        assert match, f"a DELETE names something unqualified: {statement}"
        table = match.group(1)
        assert table in reset.BANDED_DELETES or table in reset.WHOLESALE_DELETES, (
            f"the reset deletes from {table}, which neither "
            "reset.BANDED_DELETES nor reset.WHOLESALE_DELETES names. A table "
            "this job may empty is one somebody has to have argued for."
        )
        assert "demo_id = :who" in statement, (
            f"the DELETE on {table} is not scoped to one visitor: {statement}. "
            "Ageing one session out must not be able to reach another, and the "
            "row access policy is not the check -- the escape is set."
        )
        if table in reset.WHOLESALE_DELETES:
            continue
        identifier = reset.BANDED_DELETES[table]
        assert (
            f"TRY_TO_NUMBER(SPLIT_PART({identifier}, '-', 2)) >= :LIVE_ID_BAND"
            in statement
        ), (
            f"the DELETE on {table} has lost its band predicate on "
            f"{identifier}: {statement}\n\n{reset.PRESERVED_BELOW_BAND}."
        )


def test_the_band_is_one_number_in_two_places(declared: DeclaredProcedure) -> None:
    """The reset and #46's write path agree about where generated history stops.

    The procedures mint identifiers from the band upward and this job deletes
    from the band upward. Two constants would eventually disagree, and the
    direction that hurts is a reset whose band is lower than the sequence's.
    """
    match = re.search(r"LIVE_ID_BAND NUMBER DEFAULT (\d+);", declared.body)
    assert match, "the reset no longer declares LIVE_ID_BAND"
    assert int(match.group(1)) == procedures.LIVE_ID_BAND == reset.LIVE_ID_BAND


def test_the_reset_never_truncates(source: str) -> None:
    """#9's decision, as an assertion about a word that must not appear.

    Visitor state persists between visits, so emptying a visitor-scoped table
    would empty the account of somebody who is coming back tomorrow. The whole
    shape of this job -- age out, then restore -- exists because of that, and a
    TRUNCATE creeping in would be the shape quietly reverting.
    """
    for statement in statements(source):
        assert not statement.upper().startswith("TRUNCATE"), (
            f"{RESET_FILE} truncates: {statement}. #9 decided visitor state "
            "persists, so a truncating reset empties a returning visitor's "
            "account mid-story."
        )


def test_the_reset_touches_no_mart_and_no_catalogue(source: str) -> None:
    """#47's second scope line, as a claim about tables nobody names.

    The catalogue and the four gold marts are the nightly publish's, and a
    reset that wrote either would be two jobs owning one table.
    """
    forbidden = {
        table.qualified()
        for table in schema.TABLES
        if table.schema in ("CATALOGUE", "MARTS")
    }
    for statement in statements(source):
        if not statement.upper().startswith(("DELETE", "UPDATE", "INSERT", "MERGE")):
            continue
        for name in forbidden:
            assert name not in statement, (
                f"{RESET_FILE} writes {name}, which #39's nightly publish owns. "
                "#47 says the reset touches neither the real catalogue nor the "
                "gold marts."
            )


# ---------------------------------------------------------------------------
# What it restores
# ---------------------------------------------------------------------------


def test_the_reset_writes_no_column_nobody_argued_for(
    declared: DeclaredProcedure,
) -> None:
    """The UPDATE writes exactly `RESTORED_COLUMNS` plus `CLEARED_COLUMNS`.

    Fails on an addition as well as on a loss. A column added to
    ``demo_visitors`` that a visitor can change is one somebody has to decide
    about: restored from the baseline, or cleared, or deliberately left alone
    with a reason. There is no fourth state and no default.
    """
    updates = _dml(declared.body, "UPDATE")
    assert len(updates) == 1, f"expected one UPDATE, parsed {len(updates)}"
    statement = updates[0]
    assert statement.startswith("UPDATE CHIP_CHAT.ACCOUNTS.demo_visitors"), statement

    written = {
        match.group(1) for match in re.finditer(r"(\w+)\s+= (?:b\.\w+|NULL)", statement)
    }
    assert written == set(reset.RESTORED_COLUMNS) | set(reset.CLEARED_COLUMNS), (
        f"the reset writes {sorted(written)} on demo_visitors and reset.py "
        f"declares {sorted(set(reset.RESTORED_COLUMNS) | set(reset.CLEARED_COLUMNS))}."
    )
    for column in reset.RESTORED_COLUMNS:
        assert f"{column} = b.{column}" in statement, (
            f"{column} is in RESTORED_COLUMNS and is not read out of the "
            "baseline. Restoring an editable column to NULL is not a restore: "
            "data-gen produces two of the three non-null for some customers."
        )
    for column in reset.CLEARED_COLUMNS:
        assert f"{column} = NULL" in statement


def test_every_editable_column_is_put_back() -> None:
    """The three fields a visitor may change are all restored, from the baseline.

    `schema.EDITABLE_COLUMNS` is the list PRD Q2's containment is written in
    terms of, and a reset that missed one would leave a stranger's edit on a
    persona the next visitor is handed.
    """
    assert set(schema.EDITABLE_COLUMNS) <= set(reset.RESTORED_COLUMNS)
    baseline = schema.table(reset.BASELINE_TABLE)
    for column in reset.RESTORED_COLUMNS:
        assert column in baseline.column_names(), (
            f"{column} is restored from {reset.BASELINE_TABLE} and that table "
            "does not carry it, so the restore would write NULL"
        )


def test_the_baseline_is_the_generators_own_export() -> None:
    """#47's first acceptance criterion, as a fact about which file it reads.

    "Verified against the generator's output" is only checkable if the baseline
    IS the generator's output. It is loaded from ``demo_visitors.jsonl``, in the
    same run as ``demo_visitors`` -- a baseline from a second generation would
    restore visitors to a state that never existed and nothing downstream could
    tell.
    """
    baseline = schema.table(reset.BASELINE_TABLE)
    assert baseline.source == "demo_visitors"
    assert baseline.source_name() == schema.table("demo_visitors").source_name()
    assert baseline.schema == "ACCOUNTS"


def test_the_baseline_is_protected_like_everything_else_in_accounts() -> None:
    """A per-visitor table added by #47 is one #43's policy has to cover.

    The audit view in `09_audit.sql` defaults to deny, so a new ACCOUNTS table
    is presumed visitor-scoped until somebody exempts it. This one is not
    exempted, so it must carry demo_id and a policy -- and it does, which is
    what keeps `snowflake-verify`'s coverage check green.
    """
    baseline = schema.table(reset.BASELINE_TABLE)
    assert baseline.visitor_scoped
    assert baseline.policy == schema.ISOLATION_POLICY
    assert schema.DEMO_ID in baseline.column_names()
    assert (baseline.schema, baseline.name) not in schema.EXEMPT


# ---------------------------------------------------------------------------
# The escape, and the refusal
# ---------------------------------------------------------------------------


def test_the_reset_asks_for_the_maintenance_escape(
    declared: DeclaredProcedure,
) -> None:
    """#43's policies filter DELETE and UPDATE, not only SELECT.

    An admin session that has bound no visitor deletes zero rows. `load.py` is
    the escape's first caller and this is its second, which `10_policies.sql`
    says in the file the policy body is in.
    """
    assert f"SET {schema.MAINTENANCE_VARIABLE} =" in declared.body
    assert f"UNSET {schema.MAINTENANCE_VARIABLE}" in declared.body


def test_the_reset_refuses_rather_than_running_empty(
    declared: DeclaredProcedure,
) -> None:
    """It checks that the escape took, and fails when it did not.

    This is the check the whole job stands on. Without it the failure mode is a
    task that runs every night, deletes nothing, restores nothing and reports a
    clean run while the personas drift for a month -- a green light over a job
    doing nothing, which is worse than a red one.
    """
    assert "MAINTENANCE_ESCAPE_UNAVAILABLE" in declared.body
    escape = declared.body.index(f"SET {schema.MAINTENANCE_VARIABLE} =")
    check = declared.body.index(f"GETVARIABLE('{schema.MAINTENANCE_VARIABLE}') IS NULL")
    assert escape < check, "the escape is checked before it is asked for"
    assert f"CURRENT_ROLE() <> '{account.ADMIN_ROLE}'" in declared.body, (
        "the refusal does not check the role. The policy honours the escape "
        f"only for a session whose primary role is {account.ADMIN_ROLE}, so a "
        "variable set by anybody else is a variable that does nothing."
    )


def test_the_escape_is_released_on_every_path(declared: DeclaredProcedure) -> None:
    """Including the exception path.

    An operator's next query in the same session must not be quietly
    cross-visitor because a reset failed halfway and left the door open.
    """
    handler = declared.body.index("EXCEPTION")
    assert f"UNSET {schema.MAINTENANCE_VARIABLE}" in declared.body[handler:], (
        "the exception handler does not release the maintenance escape"
    )


def test_the_rejection_codes_match_in_both_directions(
    declared: DeclaredProcedure,
) -> None:
    """A code in the SQL and not in `reset.py` is one no operator was told about.

    And a code here and not in the SQL is a promise nothing keeps. Both are the
    same class of drift and neither shows up in a diff of one file.
    """
    found = set(re.findall(r"'rejection', '(\w+)'", declared.body))
    assert found == set(reset.REJECTIONS), (
        f"{RESET_FILE} returns {sorted(found)} and reset.REJECTIONS declares "
        f"{sorted(reset.REJECTIONS)}."
    )


def test_the_reset_runs_as_its_caller(declared: DeclaredProcedure) -> None:
    """Owner's rights would read the owner's session variables, not the task's.

    The same one word `test_procedure_layout.py` holds the four write
    procedures to, and it matters here for the mirror-image reason: this
    procedure SETS a session variable, and an owner's rights procedure may not.
    """
    assert declared.rights == "CALLER"


def test_no_argument_names_a_visitor(declared: DeclaredProcedure) -> None:
    """The reset acts on whoever is aged out, never on whoever is named.

    `procedures.IDENTITY_VOCABULARY` is the same absence the tool surface and
    the write path are built on. An argument spelled like an identity would be
    a field through which one visitor's session could be ended by name.
    """
    for name, _type in declared.arguments:
        assert name.lower() not in procedures.IDENTITY_VOCABULARY, (
            f"{reset.PROCEDURE} takes {name}, which names a visitor"
        )


# ---------------------------------------------------------------------------
# The TTL, and the schedule
# ---------------------------------------------------------------------------


def test_the_ttl_outlives_the_session_cookie() -> None:
    """The whole argument for the number, as one comparison.

    A TTL at or below the cookie's life ages out visitors who can still return,
    which is #9's persistence undone by the job written to respect it.
    """
    assert reset.ttl_is_sound(), (
        f"a {reset.SESSION_TTL_DAYS}-day TTL does not outlive a "
        f"{reset.COOKIE_MAX_AGE_SECONDS}-second session cookie"
    )


def test_the_procedure_refuses_a_ttl_shorter_than_the_cookie(
    declared: DeclaredProcedure,
) -> None:
    """The floor is in the SQL too, so a hand-typed CALL cannot undercut it.

    The manual trigger takes ``--ttl-days``, and the reason an operator reaches
    for it is that a demo just went badly -- which is exactly when somebody
    types a small number.
    """
    match = re.search(r"MIN_TTL_DAYS NUMBER DEFAULT (\d+);", declared.body)
    assert match, "the procedure no longer declares a minimum TTL"
    minimum = int(match.group(1))
    assert minimum * 86_400 > reset.COOKIE_MAX_AGE_SECONDS
    assert minimum <= reset.SESSION_TTL_DAYS
    assert "TTL_TOO_SHORT" in declared.body


def test_the_task_runs_the_same_procedure_with_the_configured_ttl(
    source: str,
) -> None:
    """The nightly run and the manual run are one code path.

    A task with its own copy of the logic is a second thing to keep true, and
    the manual trigger #47 asks for is only worth having if running it exercises
    what runs at nine every morning.
    """
    flattened = flat(source)
    assert f"CREATE OR ALTER TASK {reset.TASK}" in flattened
    assert f"WAREHOUSE = {reset.TASK_WAREHOUSE}" in flattened, (
        "the task does not run on the batch warehouse, so a reset can queue in "
        "front of a conversation -- and the serving warehouse cancels anything "
        "still running after sixty seconds"
    )
    assert f"SCHEDULE = '{reset.TASK_SCHEDULE}'" in flattened
    assert (
        f"CALL {account.schema('ACCOUNTS')}.{reset.PROCEDURE}"
        f"({reset.SESSION_TTL_DAYS}, FALSE)" in flattened
    ), (
        "the task calls the reset with a TTL that is not reset.SESSION_TTL_DAYS, "
        "so the number in the code and the number in production differ"
    )


def test_the_task_is_resumed_by_the_apply(source: str) -> None:
    """A created task is SUSPENDED, so an apply without a RESUME is a no-op.

    And a silent one: the object exists, `SHOW TASKS` lists it, and it never
    fires. This is the second of the two failure modes in this file that look
    exactly like success.
    """
    assert f"ALTER TASK {reset.TASK} RESUME" in flat(source)


def test_the_role_can_actually_run_a_task(source: str) -> None:
    """EXECUTE TASK is account-level and is not granted by anything else here.

    Without it the RESUME above fails, which is at least loud -- but it fails
    during an apply of an unrelated file if the grant ever moves, so the grant
    and the task stay in one file.
    """
    assert f"GRANT EXECUTE TASK ON ACCOUNT TO ROLE {account.ADMIN_ROLE}" in flat(source)


def test_the_reset_runs_after_everything_it_touches() -> None:
    """The tables, the policies and the procedures all come first.

    The procedure body names `demo_visitor_baseline`, which `07_accounts.sql`
    creates, and the task calls the procedure. An apply that ran this file first
    would fail on the first statement rather than halfway.
    """
    order = [path.name for path in ordered_files()]
    for earlier in ("07_accounts.sql", "10_policies.sql", "12_procedures.sql"):
        assert order.index(earlier) < order.index(RESET_FILE)


# ---------------------------------------------------------------------------
# The gaps, named
# ---------------------------------------------------------------------------


def test_every_gap_this_tier_leaves_names_where_it_is_closed() -> None:
    """A gap nobody named is a gap nobody owns.

    Four of them, and the sharp one is the third: #39's publish replaces the
    three account tables wholesale every night, which erases live rows for
    visitors who have not aged out. docs/nightly-publish.md §7 routes that
    decision here and docs/demo-reset.md §6 makes it.
    """
    assert reset.ENFORCED_ELSEWHERE
    for gap, where in reset.ENFORCED_ELSEWHERE.items():
        assert gap, "a gap with no name"
        assert where, f"{gap} names no owner"
        assert len(where) > 40, f"{gap}'s answer is too short to be one"


def test_the_procedure_carries_a_comment_worth_retrieving(
    declared: DeclaredProcedure,
) -> None:
    """An operator reaching for this at the worst moment reads DESC PROCEDURE.

    The same standard `test_procedure_layout.py` holds the write path to: the
    comment has to say what the thing does and what it refuses, because the
    file it is written in is not what somebody looks at during an incident.
    """
    assert len(declared.comment) > 400
    for word in ("#47", "DRY_RUN", reset.BASELINE_TABLE):
        assert word in declared.comment, f"the comment does not mention {word}"
