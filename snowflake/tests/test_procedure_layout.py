"""The four write procedures, `chip_chat.snowflake.procedures` and §7 agree.

Three descriptions of one write path, and this is where they are held together.
`sql/12_procedures.sql` and `sql/13_cancel_order.sql` are what Snowflake runs,
`procedures.py` is what the rest of the tree reads, and docs/action-surface.md §7
is what issue #46 says both of them implement. Free, offline, and in `make ci`;
whether the live account actually has these procedures and whether they behave is
`chip_chat.snowflake.verify`'s question and it needs a trial and a credential.

The test with the most value per line is
:func:`test_every_procedure_runs_as_its_caller`, and it is one word. Snowflake's
default is owner's rights: a procedure without ``EXECUTE AS CALLER`` reads
``GETVARIABLE('DEMO_ID')`` from the owner's session and is filtered by #43's row
access policies as the owner rather than as the visitor. Every test that opens
one session passes either way, which is exactly the shape of mistake this file
exists for.

Two of these fail on an *addition* rather than on a loss, which is the unusual
direction: :func:`test_no_procedure_takes_a_visitor_identifier` refuses a new
argument spelled like an identity, and
:func:`test_the_invented_procedure_is_alone_in_its_file` refuses a second
procedure moving in beside ``cancel_order``. Both are about the write path
growing quietly rather than about it being wrong today.
"""

import re

import pytest
from sql_text import DeclaredProcedure, declared_procedures, flat

from chip_chat.snowflake import procedures, schema
from chip_chat.snowflake.apply import ordered_files

PROCEDURE_FILES = ("12_procedures.sql", "13_cancel_order.sql")
"""The two files that create procedures. `11_` holds exactly one, on purpose."""


@pytest.fixture(scope="module")
def sql() -> dict[str, str]:
    """Return every numbered SQL file's text, keyed by filename."""
    return {path.name: path.read_text() for path in ordered_files()}


@pytest.fixture(scope="module")
def declared(sql: dict[str, str]) -> dict[str, DeclaredProcedure]:
    """Return every procedure the SQL declares, keyed by name."""
    found: dict[str, DeclaredProcedure] = {}
    for name in PROCEDURE_FILES:
        for found_procedure in declared_procedures(sql[name]):
            found[found_procedure.name] = found_procedure
    return found


def test_the_procedure_parser_still_reads_the_files(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """A parser that matches nothing would make every test below vacuous."""
    assert len(declared) == len(procedures.PROCEDURES), (
        f"parsed {sorted(declared)} out of {list(PROCEDURE_FILES)} and "
        f"chip_chat.snowflake.procedures declares "
        f"{[p.name for p in procedures.PROCEDURES]}. Either a procedure was "
        "added to one and not the other, or the SQL has been reformatted into "
        "a shape sql_text.declared_procedures no longer understands -- in "
        "which case the checks below are proving nothing."
    )


# ---------------------------------------------------------------------------
# The two absences the launch gates rest on
# ---------------------------------------------------------------------------


def test_every_procedure_runs_as_its_caller(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """``EXECUTE AS CALLER``, on all four, and the default is the dangerous one.

    An owner's-rights procedure executes as CHIP_CHAT_ADMIN. Two things follow
    and both are fatal: ``GETVARIABLE('DEMO_ID')`` reads the owner's session
    rather than the caller's, so every visitor would look like the same one; and
    #43's row access policies are evaluated against the owner, so the filter
    that keeps visitors apart is applied to a role that is not any of them.
    """
    for declaration in procedures.PROCEDURES:
        found = declared[declaration.name]
        assert declaration.rights == "CALLER", (
            f"procedures.py declares {declaration.name} as {declaration.rights}"
            " rights. There is no other permitted value here."
        )
        assert found.rights == "CALLER", (
            f"{declaration.file} creates {declaration.name} with "
            f"{found.rights}'s rights. Owner's rights is Snowflake's default "
            "and it silently reads the OWNER's session variable and applies "
            "#43's policies to the OWNER -- which is RFC-001 §05 undone from "
            "inside the write path, with no failing query anywhere."
        )


def test_no_procedure_takes_a_visitor_identifier(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """The absence `chip_chat.agent.surface` is built on, one tier down.

    A row access policy filters what a session may SEE. An INSERT naming another
    visitor is a write, so an argument through which a caller could name one
    would not be caught by #43 at all.
    """
    for declaration in procedures.PROCEDURES:
        found = declared[declaration.name]
        spelled = {name.lower() for name, _ in found.arguments}
        overlap = spelled & set(procedures.IDENTITY_VOCABULARY)
        assert not overlap, (
            f"{declaration.name} takes {sorted(overlap)}. Identity is bound to "
            "the session and enforced underneath; an argument for it is a "
            "field for a compromised caller to fill in with somebody else's."
        )
        spelled_out = tuple(
            (argument.name, argument.sql_type) for argument in declaration.arguments
        )
        assert spelled_out
        assert spelled_out == found.arguments, (
            f"{declaration.name}'s arguments disagree between {declaration.file}"
            " and procedures.py. The order is part of the comparison: it is the "
            "order the ops API passes them in."
        )


def test_every_argument_is_one_the_database_could_not_look_up() -> None:
    """Every argument carries the reason the database needs to be told it.

    The test a proposed fifth argument has to pass. An argument the procedure
    could derive for itself is one a caller can get wrong, and a caller getting
    it wrong is a write nobody validated.
    """
    for declaration in procedures.PROCEDURES:
        for argument in declaration.arguments:
            assert len(argument.why) > 40, (
                f"{declaration.name}.{argument.name}'s reason is a placeholder"
            )


def test_every_procedure_reads_the_session_variable(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """And it is the one #43's policies compare against, spelled the same way."""
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        assert f"GETVARIABLE('{procedures.IDENTITY_VARIABLE}')" in body, (
            f"{declaration.name} never reads {procedures.IDENTITY_VARIABLE}. A "
            "write procedure that does not know whose row it is writing is one "
            "that will write somebody's."
        )
        assert "SESSION_NOT_BOUND" in body, (
            f"{declaration.name} does not reject an unbound session. An unset "
            "variable reads as null, and a null demo_id on a written row is a "
            "row no policy can decide about -- invisible to everybody, "
            "including whoever it belongs to."
        )


# ---------------------------------------------------------------------------
# Transactional, and idempotent -- issue #46's own words
# ---------------------------------------------------------------------------


def test_every_procedure_writes_inside_one_transaction(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """#46: when the ops API is unavailable, nothing is half-written.

    An order with lines and no accrual, or an accrual with no order, is worse
    than a rejection: it is a wrong balance that looks like a right one.
    """
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        assert "BEGIN TRANSACTION" in body, f"{declaration.name} never opens one"
        assert "COMMIT" in body, f"{declaration.name} never commits"
        assert "ROLLBACK" in body, (
            f"{declaration.name} has no ROLLBACK, so a failure part-way through "
            "leaves whatever it had written"
        )
        handled = "EXCEPTION" in body and "WHEN OTHER THEN" in body
        assert handled, (
            f"{declaration.name} has no exception handler, so an unpredicted "
            "failure escapes as a raw Snowflake error with the transaction "
            "open behind it"
        )


def test_every_procedure_claims_its_retry_key_with_a_merge(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """#46: double-submitting the same retry key produces one order.

    The word MERGE is the assertion, not the table. Snowflake's INSERT does not
    conflict with a concurrent INSERT and its SELECT takes no lock, so a
    SELECT-then-INSERT claim reads identically in review and does not serialise:
    two retries of one order both look, both find nothing, and both write. A
    MERGE locks the target for the rest of the transaction.
    """
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        claim = rf"MERGE INTO CHIP_CHAT\.ACCOUNTS\.{procedures.RECEIPT_TABLE}\b"
        assert re.search(claim, body), (
            f"{declaration.name} does not claim its retry key with a MERGE into "
            f"{procedures.RECEIPT_TABLE}. A check-then-insert is a race, and "
            "the failure it produces is a second real order."
        )
        assert "'replayed'" in body, (
            f"{declaration.name} never marks a replayed receipt, so a retry and "
            "a first call are indistinguishable to whoever renders them"
        )


def test_no_procedure_writes_a_demo_id_it_did_not_read_from_the_session(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """ROW ACCESS POLICIES DO NOT FILTER INSERT. This is what covers that.

    Found on #43 (cc-73k) and it names this ticket: the policies attached to
    ACCOUNTS filter SELECT, UPDATE and DELETE, and an INSERT is not filtered at
    all. So a write path that accepted a visitor identifier could attribute a
    row to somebody else and the isolation layer would not object -- isolation
    would look correct in every read path and in review.

    The fix is by construction rather than by care:
    :func:`test_no_procedure_takes_a_visitor_identifier` means the caller cannot
    express the wrong thing, and this means the body does not either. Every
    ``demo_id`` a procedure writes is the local variable it read
    ``GETVARIABLE('DEMO_ID')`` into, so there is no expression anywhere in the
    write path through which another visitor could be named.
    """
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        assert re.search(r"visitor := GETVARIABLE\('DEMO_ID'\);", body), (
            f"{declaration.name} does not read the session variable into the "
            "one local variable every write below uses"
        )
        for statement in re.findall(r"INSERT INTO [\w.]+ \([^)]*\)\n(?:.|\n)*?;", body):
            if schema.DEMO_ID not in statement.lower():
                continue
            assert ":visitor" in statement, (
                f"{declaration.name} inserts a {schema.DEMO_ID} that does not "
                "come from the session variable:\n" + statement
            )
        for statement in re.findall(r"MERGE INTO (?:.|\n)*?;", body):
            assert ":visitor AS demo_id" in statement, (
                f"{declaration.name}'s retry-key claim does not key on the "
                "session's visitor"
            )


def test_the_receipt_table_is_a_declared_visitor_scoped_table() -> None:
    """It holds one visitor's spent keys, so #43 has to be able to reach it."""
    receipts = schema.table(procedures.RECEIPT_TABLE)
    assert receipts.visitor_scoped
    assert schema.DEMO_ID in receipts.column_names()
    assert receipts.key[0] == schema.DEMO_ID, (
        "a retry key is a fact about one visitor's attempt. Keyed on the key "
        "alone, one visitor's retry could collide with another's -- a "
        "cross-visitor effect in the table added to make writes safe"
    )


def test_every_table_a_procedure_writes_is_a_declared_table() -> None:
    """No procedure writes something `schema.py` has never heard of."""
    for declaration in procedures.PROCEDURES:
        for name in declaration.writes:
            written = schema.table(name)
            assert written.schema == "ACCOUNTS", (
                f"{declaration.name} writes {written.qualified()}, outside "
                "ACCOUNTS. CHIP_CHAT_WRITE holds no mutating privilege on "
                "CATALOGUE or MARTS, and account.GRANTS is where that is fixed"
            )


# ---------------------------------------------------------------------------
# Rejections -- typed, and the same set on both sides
# ---------------------------------------------------------------------------


def test_the_rejection_codes_match_in_both_directions(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """A code in the SQL and not here is one the ops API cannot render.

    And a code here and not in the SQL is a promise nothing keeps. Both are
    failures, which is why the assertion is an equality rather than a subset.
    """
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        # Two shapes. Most codes are written straight into the returned object;
        # the two procedures that pick the first failing rule out of a query
        # name every candidate as a literal in a UNION ALL and return the
        # column, so both shapes have to be collected or the equality below
        # would be an equality between two partial lists.
        in_sql = set(re.findall(r"'rejection', '([A-Z_]+)'", body))
        in_sql |= set(re.findall(r"SELECT '([A-Z_]+)'(?: AS reason)?,", body))
        assert in_sql == set(declaration.all_rejections()), (
            f"{declaration.name} returns {sorted(in_sql)} and procedures.py "
            f"declares {sorted(declaration.all_rejections())}. "
            "docs/action-surface.md §" + declaration.surface + " fixes the list."
        )


def test_no_procedure_repairs_what_it_could_reject(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """§7: every failure is a typed rejection, never a repaired call.

    A rejection is a returned object rather than a raised error, so that the ops
    API renders a reason rather than a stack trace -- but ``ok`` has to be there
    and has to be false, or a caller checking the wrong field reads a rejection
    as a receipt.
    """
    for declaration in procedures.PROCEDURES:
        body = declared[declaration.name].body
        for match in re.finditer(r"'rejection', ", body):
            window = body[max(0, match.start() - 200) : match.start()]
            assert "'ok', FALSE" in window, (
                f"{declaration.name} returns a rejection whose object does not "
                "carry ok false within the same constructor"
            )


# ---------------------------------------------------------------------------
# The one invented procedure, and what keeps its exit cheap
# ---------------------------------------------------------------------------


def test_the_invented_procedure_is_alone_in_its_file(
    sql: dict[str, str],
) -> None:
    """cancel_order models something the published record refuses. §3.

    docs/action-surface.md §10 row 1 records the exit: a PRD change dropping
    T1's cancellation clause, and then the tool goes too. That exit is cheap
    only while removing the procedure is deleting a file. A second procedure
    moving in here would make it an edit, and an edit is where the other three
    start depending on it.
    """
    invented = list(procedures.separable())
    assert [declaration.name for declaration in invented] == ["cancel_order"], (
        "the set of invented procedures changed. Every one of them needs a file "
        "to itself and an entry in docs/action-surface.md §10."
    )
    for declaration in invented:
        others = [
            found.name
            for found in declared_procedures(sql[declaration.file])
            if found.name != declaration.name
        ]
        assert not others, (
            f"{declaration.file} also creates {others}. It exists so that "
            f"deleting {declaration.name} is deleting a file."
        )
        argued = declaration.invention is not None and len(declaration.invention) > 200
        assert argued, (
            f"{declaration.name} is flagged invented without the argument. An "
            "operator deciding whether to keep it is deciding against this text"
        )


def test_the_cancellation_window_is_one_number_in_two_places(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """The invented constant, and the drift that would make it unremovable.

    Two copies of an invented number is how an invention stops being one thing
    somebody can delete.
    """
    body = declared["cancel_order"].body
    match = re.search(
        r"CANCELLATION_WINDOW_MINUTES NUMBER DEFAULT (?P<minutes>\d+)", body
    )
    assert match, "cancel_order no longer declares its window as a named constant"
    assert int(match.group("minutes")) == procedures.CANCELLATION_WINDOW_MINUTES


def test_the_receipt_says_what_the_real_product_does(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """§7.2: the sentence on the receipt is *required, not optional*.

    Both sentences, in fact. The FAQ refuses cancellation outright for a pickup
    order and routes a delivery order to a human who may charge a fee, and a
    demo that cancels cleanly and for free teaches a visitor something about the
    business that is not true.
    """
    body = declared["cancel_order"].body
    assert "cannot normally cancel" in body, (
        "the receipt no longer says that Chipotle does not normally allow this"
    )
    real_path = "Customer Service" in body and "cancelation fee" in body
    assert real_path, (
        "the receipt drops the real delivery path -- a human, and a possible "
        "fee. Dropping the fee is the easy half to miss, because the invention "
        "table frames the invention around timing rather than money"
    )
    for declaration in procedures.PROCEDURES:
        assert "'simulation'" in declared[declaration.name].body, (
            f"{declaration.name}'s receipt does not say the action was "
            "simulated. PRD T5 requires it on every one of them"
        )


def test_redemption_says_it_cannot_be_undone(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """§7.3: the terms say redeemed points are gone, so the receipt says it too."""
    body = declared["redeem_points"].body
    assert "cannot be undone" in body
    assert "one reward can be used per order" in body, (
        "the published rewards FAQ limits an order to one reward, and a receipt "
        "that does not say so implies a visitor can stack them"
    )


def test_a_preference_is_not_an_allergen_answer(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """§4 and PRD K3, in the one place a visitor could confuse the two."""
    body = declared["update_preferences"].body
    assert "not an allergen check" in body, (
        "the acknowledgement no longer says that a stated preference is not an "
        "allergen answer. A no-dairy preference filters a candidate set; it "
        "does not consult item_allergens and it is not a safety guarantee"
    )
    for stance in procedures.STANCES:
        assert f"'{stance}'" in body, f"the stance {stance!r} is no longer accepted"
    assert str(procedures.DISPLAY_NAME_MAX_LENGTH) in body
    assert str(procedures.MAX_STATED_PREFERENCES) in body


# ---------------------------------------------------------------------------
# Where the numbers a procedure must not guess come from
# ---------------------------------------------------------------------------


def test_the_earn_rate_is_read_and_never_typed(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """place_order accrues at a published rate or it does not accrue.

    data-gen deliberately took ``points_per_dollar`` out of its own
    configuration file so that the generated ledger reads it off the harvested
    terms. A literal here would be a second, silent opinion about a published
    figure, and the two diverging is not something anybody would notice.
    """
    body = declared["place_order"].body
    assert "rewards_terms WHERE rule = 'points_per_dollar'" in body
    assert "EARN_RATE_NOT_PUBLISHED" in body, (
        "place_order no longer refuses to accrue when the rate is not loaded. "
        "A balance accrued at a guessed rate cannot be un-guessed later"
    )
    assert not re.search(r"\*\s*10\b", body), (
        "an earn rate looks hard-coded in place_order"
    )


def test_every_gap_this_tier_leaves_names_where_it_is_closed() -> None:
    """The absence is written down, because it reads as an oversight otherwise."""
    assert len(procedures.ENFORCED_ELSEWHERE) >= 5
    for rule, where in procedures.ENFORCED_ELSEWHERE:
        assert "§" in rule, f"{rule!r} does not cite the section it comes from"
        assert len(where) > 60, f"{rule!r} says where it is enforced only vaguely"


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def test_the_procedures_run_after_everything_they_touch() -> None:
    """Numeric prefixes are load-bearing, and this is the ordering they encode."""
    order = [path.name for path in ordered_files()]
    for name in PROCEDURE_FILES:
        assert name in order, f"{name} is not part of an apply"
        assert order.index("03_grants.sql") < order.index(name), (
            "the grants that let CHIP_CHAT_WRITE call a procedure are FUTURE "
            "grants, and a future grant is not retroactive"
        )
        assert order.index("07_accounts.sql") < order.index(name), (
            "a procedure body compiles against tables that have to exist"
        )


def test_the_sequences_exist_and_are_never_replaced(sql: dict[str, str]) -> None:
    """Replacing a sequence resets it, and a reset sequence re-mints live ids.

    The one way an apply of `07_accounts.sql` could corrupt rather than converge.
    """
    source = flat(sql["07_accounts.sql"])
    for name in procedures.SEQUENCES:
        assert f"CREATE SEQUENCE IF NOT EXISTS {name}" in source, (
            f"{name} is not created, or is created in a form that would reset it"
        )
        assert f"CREATE OR REPLACE SEQUENCE {name}" not in source
    assert f"START = {procedures.LIVE_ID_BAND}" in source, (
        "the live identifier band no longer starts where procedures.py says. "
        "Generated history is numbered from one, and the gap is what makes an "
        "order a visitor placed tellable from one the demo was seeded with"
    )


def test_every_procedure_carries_a_comment_worth_retrieving(
    declared: dict[str, DeclaredProcedure],
) -> None:
    """The same standard the tables are held to, for the same reason.

    #45's semantic view reads object comments, and a procedure Cortex Analyst
    has no description of is one it will describe from its name.
    """
    for declaration in procedures.PROCEDURES:
        comment = declared[declaration.name].comment
        assert len(comment) > 200, (
            f"{declaration.name}'s comment is a label rather than a description "
            "of what calling it does"
        )
        assert "no visitor identifier" in comment or "Takes no visitor" in comment, (
            f"{declaration.name}'s comment does not say that it takes no "
            "visitor identifier, which is the property the whole surface rests "
            "on and the one a reader of the account should meet first"
        )
