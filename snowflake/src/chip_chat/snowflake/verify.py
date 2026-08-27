"""Hold the live Snowflake account to what `snowflake/sql/` says it is.

Every check here runs against the real account. None of them reads
`snowflake/sql/` -- `tests/test_account_layout.py` and
`tests/test_schema_layout.py` do that, for free, in CI, and they answer a
different question. Those tests ask whether the checked-in SQL still says what
`chip_chat.snowflake.account` and `chip_chat.snowflake.schema` say. This asks
whether the account is what the SQL says, which is the question a UI click, a
widened grant, a hand-made table or an expired trial can change without anybody
editing a file.

    #41.1  the warehouse auto-suspends within 60 seconds of going idle
    #41.2  the read role cannot write
    #41.3  the write role cannot read another visitor's rows either
    #41.4  the account is rebuildable from snowflake/ in one run
    #42    the schema is the fourteen tables the DDL declares, every one of
           them commented, and every visitor-scoped one carrying demo_id
    #43    every visitor-scoped table carries a row access policy, and an
           unbound session reads nothing at all

#41's first three are checked here. Its fourth is checked by running
`make snowflake-rebuild`, which tears the account down and builds it back before
this runs -- a claim about a rebuild is not something a query can answer.

#42's checks run first, before the fixture below is built: the probe table
lives in CHIP_CHAT.ACCOUNTS, and "nothing is in these schemas that
`snowflake/sql` did not create" would otherwise report the probe as exactly the
thing it is looking for.

#88 adds the other half of the cost story, and it is a different kind of claim:
#41 is about what one query costs, #88 is about what a day of them costs. The
resource monitors are checked here too, including the one an apply deliberately
does not create. That check fails on a freshly rebuilt account, on purpose --
`optional/trial_credit_cap.sql` needs a number read off the remaining balance,
and an uncapped trial should be a named failure rather than a quiet gap.

#41's criterion 3 needs a row access policy to exist, and it builds a throwaway
one on a throwaway table rather than borrowing #43's. That is deliberate: what is
under test there is the *role*, not the policy. #41's grants are what leave
CHIP_CHAT_WRITE without ``APPLY ROW ACCESS POLICY`` and without ownership of
anything, and that is where the absence gets demonstrated rather than asserted.

#43 is the launch gate and gets its own fixture, guarded by the **real**
``visitor_isolation`` policy. Three things are asked of the account and the
third is the one that keeps the other two honest:

**Coverage.** Every table on `09_audit.sql`'s ``visitor_scoped_tables`` carries
a policy, and the tables that carry one are exactly the tables
`chip_chat.snowflake.schema` says should. The live list is the view's rather
than Python's, because a table somebody created by hand is invisible to
`make ci` and is exactly the table this is looking for.

**Behaviour.** Two bound sessions see different rows; an unbound session sees
none; ``SELECT *`` returns only the bound visitor's rows; the write role is
bound by the same policy; and the maintenance escape is unreachable from a lane
role even with the variable set.

**That the coverage check still bites.** A table with a ``demo_id`` and no
policy is created and the check has to name it. A coverage check that has
quietly stopped seeing anything reports a protected account forever.

Four Snowflake behaviours the checks are built around, each of which cost an
hour to discover:

**Secondary roles.** ``USE ROLE CHIP_CHAT_READ`` does not give a session the
privileges of CHIP_CHAT_READ. It gives it those *plus* every other role its user
holds, because ``DEFAULT_SECONDARY_ROLES = ('ALL')`` is Snowflake's default and
an operator's user holds ACCOUNTADMIN. Run without the ``USE SECONDARY ROLES
NONE`` in :func:`_preamble` these checks pass while proving nothing, which is the
worst thing a security check can do. It is also why the service users in
`04_users.sql` are created with ``DEFAULT_SECONDARY_ROLES = ()``.

**A refusal has three shapes.** Snowflake declines to confirm that an object
exists when you may not see it, so "insufficient privileges" (003001), "does not
exist or not authorized" (002003) and "object does not exist, or operation
cannot be performed" (002043) are all refusals. The last two are the *stronger*
answer, not a weaker one. :data:`REFUSALS` accepts all three and nothing else --
a syntax error must not count, or this file becomes one that passes because it
is broken.

**Waking a warehouse is harder than it looks.** ``SELECT 1`` is answered
without resuming anything, and a query that would need compute comes back from
the result cache the second time it is asked -- which also resumes nothing. A
suspension check built on either times a warehouse it never woke and reports a
healthy number that would look just as healthy if the setting were 600. See
:data:`WAKE_QUERY` and :func:`_check_suspension_observed`.

**A resource monitor's notifications go nowhere by default.** A NOTIFY trigger
mails the users in ``NOTIFY_USERS``, plus account administrators who have both a
verified email address and notifications switched on -- and a fresh trial has
neither. Nothing in `snowflake/sql/` sets ``NOTIFY_USERS``, because re-asserting
it on every apply would revoke whoever the operator added. So
:func:`_check_resource_monitors` reports the recipients as evidence rather than
passing on the triggers alone, and the SUSPEND thresholds are what the guardrail
actually rests on.

    make snowflake-verify           # everything, about three minutes
    make snowflake-verify-fast      # everything except watching it suspend
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from chip_chat.snowflake import account, schema, snow

__all__ = ["REFUSALS", "Check", "main", "run"]

REFUSALS = (
    "003001 (42501)",
    "002003 (02000)",
    "002043 (02000)",
)
"""The three Snowflake error codes that mean "you may not".

Nothing else counts. A refusal recognised by its prose rather than its code
would be one release note away from being recognised by accident.
"""

PROBE_TABLE = account.table("ACCOUNTS", "_VERIFY_PROBE")
PROBE_POLICY = account.table("ACCOUNTS", "_VERIFY_PROBE_POLICY")
PROBE_MART = account.table("MARTS", "_VERIFY_PROBE_MART")

# #43's own fixture. Two rows and the REAL policy, so what is under test is the
# expression `sql/10_policies.sql` actually attached rather than a copy of it.
ISOLATION_PROBE = account.table("ACCOUNTS", "_VERIFY_ISOLATION_PROBE")

# A table with a demo_id and no policy. `09_audit.sql`'s view calls it
# visitor-scoped, because that view defaults to deny, so the coverage check has
# to name it -- and if it does not, every clean run of that check meant nothing.
UNPROTECTED_PROBE = account.table("ACCOUNTS", "_VERIFY_UNPROTECTED")

ISOLATION_POLICY = account.table("ACCOUNTS", schema.ISOLATION_POLICY)

# A visitor nothing is called. Bound to it, a session must see zero rows of
# every table -- including the roster, whose policy is open only while nothing
# is bound at all.
NOBODY = "verify-visitor-nobody"

# The three lane schemas, quoted, for the INFORMATION_SCHEMA predicates in
# #42's checks. Built from account.SCHEMAS so another LANE is covered by adding
# it there rather than by remembering this line.
#
# account.STAGING_SCHEMA is deliberately not in that tuple and so is not here.
# #39's loading dock holds no declared table and is empty between runs, so
# "every table in these schemas is one schema.py declares" is a question that
# does not apply to it -- and asking it anyway would make this command fail
# while a publish was running.
_SCHEMA_LIST = ", ".join(f"'{name}'" for name in account.SCHEMAS)

# Two visitors. The whole of criterion 3 is that a session which is one of them
# cannot see the other, and cannot change the other's row by naming it.
MINE = "verify-visitor-mine"
THEIRS = "verify-visitor-theirs"

# How long to wait for a suspension that should arrive after AUTO_SUSPEND_SECONDS.
# Snowflake checks for idle warehouses on its own cadence rather than on a timer
# per warehouse, so the observed figure is always a little over the setting; what
# would be a finding is a figure near 600, the default this account ships with.
SUSPEND_GRACE_SECONDS = 90
SUSPEND_POLL_SECONDS = 5

# A query that cannot be answered without a warehouse. Two ways to get this
# wrong: `SELECT 1` is answered without resuming anything, and anything asked
# twice comes back from the result cache, which also resumes nothing. Hence the
# session parameter -- without it this check measured a warehouse it never woke.
WAKE_QUERY = (
    "ALTER SESSION SET USE_CACHED_RESULT = FALSE;\n"
    "SELECT COUNT(*) FROM TABLE(GENERATOR(ROWCOUNT => 100000));"
)


@dataclass(frozen=True, slots=True)
class Check:
    """One thing that was checked against the live account.

    Attributes:
        criterion: Which of issue #41's criteria it belongs to.
        name: What was checked, in the imperative.
        passed: Whether the account behaved.
        detail: What actually happened -- a measured number, or the sentence of
            the refusal that names the missing privilege. Printed whether or not
            the check passed, because the evidence is the point and a pass with
            none behind it is a claim.
    """

    criterion: str
    name: str
    passed: bool
    detail: str


def _preamble(role: str, warehouse: str | None) -> str:
    """Return the statements that pin a session to exactly ``role``.

    ``USE SECONDARY ROLES NONE`` first, and it is not optional -- see the module
    docstring. Everything after it runs with the privileges of one role.
    """
    lines = ["USE SECONDARY ROLES NONE;", f"USE ROLE {role};"]
    if warehouse is not None:
        lines.append(f"USE WAREHOUSE {warehouse};")
    return "\n".join(lines)


def _as_role(role: str, warehouse: str | None, sql: str) -> snow.Completed:
    """Run ``sql`` in a fresh session holding only ``role``."""
    return snow.run_statements(f"{_preamble(role, warehouse)}\n{sql}")


def _refusal_line(output: str) -> str:
    """Return the sentence of a Snowflake refusal that says what was missing.

    The CLI draws errors in a box and hard-wraps the text inside it, so the
    interesting sentence arrives as three or four fragments with borders on
    either side. Un-wrapping first is the difference between evidence and
    "Insufficient privileges to operate on table '_VERIFY_PROBE'. Your primary".

    What is kept is everything after the error code and the query id -- the code
    is already the thing :data:`REFUSALS` matched on, and the query id is only
    useful in Snowsight.
    """
    unwrapped = " ".join(
        line.strip().strip("\u2502\u256d\u2570\u256e\u256f\u2500").strip()
        for line in output.splitlines()
    )
    collapsed = re.sub(r"\s+", " ", unwrapped).strip()
    match = re.search(r"\d{6} \([0-9A-Z]+\): [0-9a-f-]+: (.+)", collapsed)
    if match:
        return match.group(1).strip()[:220]
    return collapsed[-220:] if collapsed else "(no output)"


def _expect_refusal(
    criterion: str, name: str, role: str, warehouse: str | None, sql: str
) -> Check:
    """Attempt ``sql`` as ``role`` and require Snowflake to refuse it."""
    result = _as_role(role, warehouse, sql)
    if result.ok:
        return Check(
            criterion,
            name,
            passed=False,
            detail=(
                f"{role} SUCCEEDED at this. A grant in snowflake/sql/03_grants.sql "
                "has widened, or someone was granted the role by hand."
            ),
        )
    if not any(code in result.output for code in REFUSALS):
        return Check(
            criterion,
            name,
            passed=False,
            detail=(
                "failed, but not with a permission error -- that is a different "
                f"bug:\n      {_refusal_line(result.output)}"
            ),
        )
    return Check(criterion, name, passed=True, detail=_refusal_line(result.output))


def _expect_success(
    criterion: str, name: str, role: str, warehouse: str | None, sql: str
) -> Check:
    """Run ``sql`` as ``role`` and require it to work.

    Every refusal check above is only interesting if the role can do its job at
    all: a role with no access to anything refuses every write too, and would be
    a different bug wearing this file's passing output.
    """
    result = _as_role(role, warehouse, sql)
    return Check(
        criterion,
        name,
        passed=result.ok,
        detail="allowed" if result.ok else _refusal_line(result.output),
    )


# ---------------------------------------------------------------------------
# #41.1 -- the warehouse auto-suspends within 60 seconds of going idle
# ---------------------------------------------------------------------------


def _warehouse_rows() -> dict[str, dict[str, Any]]:
    """Return ``SHOW WAREHOUSES`` output keyed by warehouse name."""
    rows = snow.query("SHOW WAREHOUSES LIKE 'CHIP_CHAT_%';")[0]
    return {str(row["name"]): row for row in rows}


def _check_warehouse_settings() -> list[Check]:
    """Check both warehouses are X-Small and set to suspend after 60 seconds."""
    rows = _warehouse_rows()
    checks: list[Check] = []
    for warehouse in account.WAREHOUSES:
        row = rows.get(warehouse.name)
        if row is None:
            checks.append(
                Check(
                    "#41.1",
                    f"{warehouse.name} exists",
                    passed=False,
                    detail="not in SHOW WAREHOUSES. Run `make snowflake-apply`.",
                )
            )
            continue
        suspend = int(row["auto_suspend"] or 0)
        size = str(row["size"])
        acceleration = str(row["enable_query_acceleration"]).lower() == "true"
        ok = (
            suspend == account.AUTO_SUSPEND_SECONDS
            and size == account.WAREHOUSE_SIZE
            and not acceleration
        )
        checks.append(
            Check(
                "#41.1",
                f"{warehouse.name} is {account.WAREHOUSE_SIZE}, "
                f"auto_suspend {account.AUTO_SUSPEND_SECONDS}s, no acceleration",
                passed=ok,
                detail=(
                    f"size={size} auto_suspend={suspend}s "
                    f"query_acceleration={'on' if acceleration else 'off'}"
                ),
            )
        )
    return checks


def _suspend_now() -> bool:
    """Suspend the serving warehouse and wait until it reports SUSPENDED.

    The measurement below has to start from a known state. Without this the
    check can run while the warehouse is still up from an earlier probe, start
    its clock thirty seconds into a sixty-second countdown, and report a number
    that looks better than the setting.
    """
    # Errors if it is already suspended, which is the outcome we want anyway.
    snow.run_statements(f"ALTER WAREHOUSE {account.SERVING_WAREHOUSE} SUSPEND;")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = _warehouse_rows().get(account.SERVING_WAREHOUSE, {})
        if str(row.get("state", "")).upper() == "SUSPENDED":
            return True
        time.sleep(2)
    return False


def _check_suspension_observed() -> Check:
    """Suspend the serving warehouse, wake it, watch it suspend, report the seconds.

    This is the criterion as written -- "verified", not "configured" -- and the
    only check here that spends anything worth mentioning: one minute of X-Small,
    the smallest unit Snowflake bills.

    Two things make waking a warehouse harder than it sounds, and both of them
    produced a confident wrong number before they were understood.

    ``SELECT 1`` does not start a warehouse: Snowflake answers a constant without
    resuming anything. And a query that *would* need compute is served from the
    **result cache** the second time it is asked, which also resumes nothing --
    so a check that passed on its first run reported 24 seconds on its second,
    having timed a warehouse it never woke. :data:`WAKE_QUERY` runs with
    ``USE_CACHED_RESULT = FALSE``, and the warehouse's own ``resumed_on`` has to
    move before the clock starts. Nothing here is inferred from the query
    succeeding.
    """
    name = "the serving warehouse suspends after going idle"
    if not _suspend_now():
        return Check(
            "#41.1", name, passed=False, detail="could not get it to a suspended state"
        )
    before = str(_warehouse_rows().get(account.SERVING_WAREHOUSE, {}).get("resumed_on"))

    woken = _as_role(account.ADMIN_ROLE, account.SERVING_WAREHOUSE, WAKE_QUERY)
    if not woken.ok:
        return Check(
            "#41.1",
            name,
            passed=False,
            detail=f"could not wake it: {_refusal_line(woken.output)}",
        )
    idle_from = time.monotonic()

    row = _warehouse_rows().get(account.SERVING_WAREHOUSE, {})
    state = str(row.get("state", ""))
    resumed = str(row.get("resumed_on"))
    if state.upper() != "STARTED" or resumed == before:
        return Check(
            "#41.1",
            name,
            passed=False,
            detail=(
                f"the query did not wake it -- state {state or 'unknown'}, "
                f"resumed_on {'unchanged' if resumed == before else resumed}. "
                "Timing a suspension from here would measure a warehouse that was "
                "already idle. See WAKE_QUERY."
            ),
        )

    deadline = idle_from + account.AUTO_SUSPEND_SECONDS + SUSPEND_GRACE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(SUSPEND_POLL_SECONDS)
        row = _warehouse_rows().get(account.SERVING_WAREHOUSE, {})
        if str(row.get("state", "")).upper() == "SUSPENDED":
            elapsed = time.monotonic() - idle_from
            return Check(
                "#41.1",
                name,
                passed=elapsed >= account.AUTO_SUSPEND_SECONDS,
                detail=(
                    f"resumed at {resumed}, then observed SUSPENDED {elapsed:.0f}s "
                    f"after the waking query returned. The setting is "
                    f"{account.AUTO_SUSPEND_SECONDS}s; this account's default "
                    "warehouse ships at 600s."
                    + (
                        ""
                        if elapsed >= account.AUTO_SUSPEND_SECONDS
                        else " Sooner than the setting, which means this measured "
                        "something other than what it thinks it did."
                    )
                ),
            )
    return Check(
        "#41.1",
        name,
        passed=False,
        detail=(
            f"still running {account.AUTO_SUSPEND_SECONDS + SUSPEND_GRACE_SECONDS}s "
            "after going idle. Every idle second is trial credit."
        ),
    )


# ---------------------------------------------------------------------------
# #41.2 -- the read role cannot write
# ---------------------------------------------------------------------------


def _visitors_visible_to(role: str) -> tuple[bool, str]:
    """Return whether ``role`` could query the probe table, and which rows it saw.

    One statement, run with ``DEMO_ID`` set to one of the two visitors in the
    fixture. Both #41.2 and #41.3 start here: the read role has to be able to
    read before its refusals mean anything, and the write role has to see one row
    and not two.

    It reads the returned *value*, not the CLI's output. `snow sql` echoes each
    statement it runs, and the statement here contains the visitor's own name --
    so a check that searched the output for it would find it whether or not a
    single row came back. Which is exactly the kind of test that passes forever.
    """
    try:
        statements = snow.query(
            f"{_preamble(role, account.SERVING_WAREHOUSE)}\n"
            f"SET DEMO_ID = '{MINE}';\n"
            f"SELECT LISTAGG(demo_id, ',') AS seen FROM {PROBE_TABLE};"
        )
    except snow.SnowError as error:
        return False, _refusal_line(str(error))
    rows = statements[-1] if statements else []
    if not rows:
        return True, ""
    return True, str(rows[0].get("SEEN") or "")


def _check_read_role() -> list[Check]:
    """Check CHIP_CHAT_READ can read, and is refused every way of writing."""
    serving = account.SERVING_WAREHOUSE
    allowed, seen = _visitors_visible_to("CHIP_CHAT_READ")
    checks = [
        Check(
            "#41.2",
            "the read role can read -- otherwise the refusals below prove nothing",
            passed=allowed and MINE in seen,
            detail=(
                f"returned the row for {MINE}"
                if allowed and MINE in seen
                else f"read nothing: {_refusal_line(seen)}"
            ),
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot INSERT",
            "CHIP_CHAT_READ",
            serving,
            f"INSERT INTO {PROBE_TABLE} VALUES ('{MINE}', 'written by the read role');",
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot UPDATE",
            "CHIP_CHAT_READ",
            serving,
            f"UPDATE {PROBE_TABLE} SET note = 'rewritten' WHERE demo_id = '{MINE}';",
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot DELETE",
            "CHIP_CHAT_READ",
            serving,
            f"DELETE FROM {PROBE_TABLE} WHERE demo_id = '{MINE}';",
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot CREATE TABLE",
            "CHIP_CHAT_READ",
            serving,
            f"CREATE TABLE {account.table('ACCOUNTS', '_VERIFY_SHOULD_NOT_EXIST')} "
            "(a INT);",
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot DROP the table it can read",
            "CHIP_CHAT_READ",
            serving,
            f"DROP TABLE {PROBE_TABLE};",
        ),
        _expect_refusal(
            "#41.2",
            "the read role cannot CREATE SCHEMA",
            "CHIP_CHAT_READ",
            serving,
            f"CREATE SCHEMA {account.DATABASE}._VERIFY_SHOULD_NOT_EXIST;",
        ),
    ]
    return checks


# ---------------------------------------------------------------------------
# #41.3 -- the write role cannot read another visitor's rows either
# ---------------------------------------------------------------------------


def _check_write_role_under_policy() -> list[Check]:
    """Check CHIP_CHAT_WRITE is bound by a row access policy like everyone else."""
    serving = account.SERVING_WAREHOUSE
    checks: list[Check] = []

    # What it sees with DEMO_ID set to one visitor. There are two rows in the
    # table; a role exempt from the policy would see both.
    allowed, seen = _visitors_visible_to("CHIP_CHAT_WRITE")
    checks.append(
        Check(
            "#41.3",
            "the write role sees its own visitor's row",
            passed=allowed and MINE in seen,
            detail=f"DEMO_ID={MINE}: {'found it' if MINE in seen else 'saw nothing'}",
        )
    )
    checks.append(
        Check(
            "#41.3",
            "the write role does NOT see the other visitor's row",
            passed=allowed and THEIRS not in seen,
            detail=(
                f"{THEIRS} is absent from the result"
                if THEIRS not in seen
                else f"LEAK: the other visitor's row was returned:\n      {seen}"
            ),
        )
    )

    # Naming the other visitor's row explicitly does not reach it either. The
    # policy filters the rows an UPDATE can see, so this is zero rows changed
    # rather than an error -- which is the quieter and more important half.
    try:
        update = snow.query(
            f"{_preamble('CHIP_CHAT_WRITE', serving)}\n"
            f"SET DEMO_ID = '{MINE}';\n"
            f"UPDATE {PROBE_TABLE} SET note = 'taken' WHERE demo_id = '{THEIRS}';"
        )
        changed = str(next(iter(update[-1][0].values()))) if update[-1] else "?"
    except snow.SnowError as error:
        changed = f"the UPDATE did not run: {_refusal_line(str(error))}"
    checks.append(
        Check(
            "#41.3",
            "the write role cannot UPDATE the other visitor's row by naming it",
            passed=changed == "0",
            detail=f"rows updated: {changed}",
        )
    )

    checks.append(
        _expect_refusal(
            "#41.3",
            "the write role cannot detach the policy that binds it",
            "CHIP_CHAT_WRITE",
            serving,
            f"ALTER TABLE {PROBE_TABLE} DROP ROW ACCESS POLICY {PROBE_POLICY};",
        )
    )
    checks.append(
        _expect_refusal(
            "#41.3",
            "the write role cannot see the marts at all",
            "CHIP_CHAT_WRITE",
            serving,
            f"USE SCHEMA {account.schema('MARTS')};",
        )
    )
    return checks


# ---------------------------------------------------------------------------
# The other two boundaries #41 asks for: compute, and one role per credential
# ---------------------------------------------------------------------------


def _check_warehouse_separation() -> list[Check]:
    """Check no lane role can name the other lane's compute.

    "A heavy batch job cannot make a conversation slow" is a claim about
    privileges, not about scheduling: the publish role holds USAGE on the publish
    warehouse and on nothing else, so there is no way for it to end up on the
    warehouse a conversation is waiting on.
    """
    return [
        _expect_success(
            "#41",
            "the publish role can write a mart -- so its refusals are not vacuous",
            "CHIP_CHAT_PUBLISH",
            account.PUBLISH_WAREHOUSE,
            f"CREATE OR REPLACE TABLE {PROBE_MART} (a INT);\n"
            f"INSERT INTO {PROBE_MART} VALUES (1);\n"
            f"DROP TABLE {PROBE_MART};",
        ),
        _expect_refusal(
            "#41",
            "the publish role cannot use the serving warehouse",
            "CHIP_CHAT_PUBLISH",
            None,
            f"USE WAREHOUSE {account.SERVING_WAREHOUSE};",
        ),
        _expect_refusal(
            "#41",
            "the read role cannot use the publish warehouse",
            "CHIP_CHAT_READ",
            None,
            f"USE WAREHOUSE {account.PUBLISH_WAREHOUSE};",
        ),
        _expect_refusal(
            "#41",
            "the publish role cannot see the demo accounts",
            "CHIP_CHAT_PUBLISH",
            account.PUBLISH_WAREHOUSE,
            f"SELECT COUNT(*) FROM {PROBE_TABLE};",
        ),
    ]


def _check_service_users() -> list[Check]:
    """Check each service user holds exactly one role and no secondary roles."""
    checks: list[Check] = []
    for user in account.USERS:
        rows = snow.query(f"SHOW GRANTS TO USER {user.name};")[0]
        granted = sorted(str(row["role"]) for row in rows)
        checks.append(
            Check(
                "#41",
                f"{user.name} holds only {user.role}",
                passed=granted == [user.role],
                detail=f"granted: {', '.join(granted) or 'nothing'}",
            )
        )

        detail = snow.query(f"SHOW USERS LIKE '{user.name}';")[0]
        secondary = str(detail[0]["default_secondary_roles"]) if detail else "?"
        checks.append(
            Check(
                "#41",
                f"{user.name} has no default secondary roles",
                passed=secondary in ("[]", "()", ""),
                detail=(
                    f"default_secondary_roles={secondary}"
                    + (
                        ""
                        if secondary in ("[]", "()", "")
                        else " -- ALL would give a session every role this user "
                        "holds, which is how a read lane quietly becomes a write one"
                    )
                ),
            )
        )
    return checks


# ---------------------------------------------------------------------------
# #88 -- a day of queries has a ceiling, and so does the trial
# ---------------------------------------------------------------------------


def _percentages(value: Any) -> tuple[int, ...]:
    """Return the whole numbers in a ``SHOW RESOURCE MONITORS`` threshold column.

    Snowflake writes the trigger columns as ``50%,80%,100%``, an empty string
    when there are none, and null when the monitor has none of that kind. All
    three arrive here as a tuple of ints, so a monitor with no SUSPEND trigger
    compares unequal to one that has the wrong SUSPEND trigger rather than
    raising on the way to finding out.
    """
    return tuple(int(found) for found in re.findall(r"\d+", str(value or "")))


def _monitor_rows() -> dict[str, dict[str, Any]]:
    """Return ``SHOW RESOURCE MONITORS`` output keyed by monitor name.

    Resource monitors are ACCOUNTADMIN objects: there is no lane role that can
    see one, and the connection's default role is whatever the operator left it
    as. Hence the explicit ``USE ROLE``.
    """
    statements = snow.query("USE ROLE ACCOUNTADMIN;\nSHOW RESOURCE MONITORS;")
    rows = statements[-1] if statements else []
    return {str(row["name"]): row for row in rows}


def _check_resource_monitors() -> list[Check]:
    """Check both warehouse monitors, their assignments, and the account cap.

    `01_warehouses.sql` bounds what one query costs; this bounds what a day of
    them costs. The two are different guardrails and only the second one can
    stop a runaway from spending a 30-day trial in a weekend.
    """
    try:
        monitors = _monitor_rows()
    except snow.SnowError as error:
        return [
            Check(
                "#88",
                "the resource monitors can be read at all",
                passed=False,
                detail=(
                    "SHOW RESOURCE MONITORS was refused, so nothing below could "
                    f"be checked. It needs ACCOUNTADMIN:\n      "
                    f"{_refusal_line(str(error))}"
                ),
            )
        ]

    warehouses = _warehouse_rows()
    checks: list[Check] = []

    for monitor in account.MONITORS:
        row = monitors.get(monitor.name)
        if row is None:
            checks.append(
                Check(
                    "#88",
                    f"{monitor.name} exists",
                    passed=False,
                    detail=(
                        "not in SHOW RESOURCE MONITORS, so nothing bounds what "
                        f"{monitor.warehouse} can spend in a day. Run "
                        "`make snowflake-apply`."
                    ),
                )
            )
            continue

        quota = int(float(row.get("credit_quota") or 0))
        frequency = str(row.get("frequency") or "").upper()
        notify = _percentages(row.get("notify_at"))
        suspend = _percentages(row.get("suspend_at"))
        immediate = _percentages(row.get("suspend_immediately_at"))
        recipients = str(row.get("notify_users") or "").strip("[]() ")
        checks.append(
            Check(
                "#88",
                f"{monitor.name} caps {monitor.warehouse} at "
                f"{monitor.daily_credit_quota} credits a day",
                passed=(
                    quota == monitor.daily_credit_quota
                    and frequency == "DAILY"
                    and notify == monitor.notify_at_percent
                    and suspend == (monitor.suspend_at_percent,)
                    and immediate == (monitor.suspend_immediate_at_percent,)
                ),
                detail=(
                    f"quota={quota} frequency={frequency or 'unset'} "
                    f"notify={list(notify)} suspend={list(suspend)} "
                    f"suspend_immediate={list(immediate)} "
                    f"used={row.get('used_credits')} of them today. "
                    + (
                        f"NOTIFY reaches {recipients}"
                        if recipients
                        else "NOTIFY reaches nobody -- NOTIFY_USERS is empty and no "
                        "checked-in file sets it. The SUSPEND thresholds do not "
                        "depend on anyone reading email; the NOTIFY ones do. "
                        "docs/snowflake-account.md section 8 item 8"
                    )
                ),
            )
        )

        assigned = str(warehouses.get(monitor.warehouse, {}).get("resource_monitor"))
        checks.append(
            Check(
                "#88",
                f"{monitor.warehouse} is assigned to {monitor.name}",
                passed=assigned == monitor.name,
                detail=(
                    f"resource_monitor={assigned}"
                    + (
                        ""
                        if assigned == monitor.name
                        else ". A monitor nothing is assigned to counts nothing"
                    )
                ),
            )
        )

    # The account-wide cap. An apply does not create it -- the quota comes off
    # the remaining balance rather than off the shape of the workload -- so this
    # fails on a freshly rebuilt account, which is the intended reading: the
    # trial really is uncapped until somebody chooses a number.
    account_level = [
        row
        for row in monitors.values()
        if str(row.get("level") or "").upper() == "ACCOUNT"
    ]
    capped = next(
        (row for row in account_level if _percentages(row.get("suspend_at"))), None
    )
    checks.append(
        Check(
            "#88",
            "the trial has a total credit cap, not just a daily one",
            passed=capped is not None,
            detail=(
                f"{capped['name']} is set on the account: quota "
                f"{int(float(capped.get('credit_quota') or 0))} credits, "
                f"{capped.get('used_credits')} used, frequency "
                f"{capped.get('frequency')}, suspends at "
                f"{list(_percentages(capped.get('suspend_at')))}"
                if capped is not None
                else (
                    "no account-level resource monitor with a SUSPEND trigger. The "
                    "daily monitors above bound each CHIP_CHAT warehouse and do not "
                    "bound the total, and neither of them counts COMPUTE_WH -- which "
                    "is still the default warehouse in the `snow` connection and "
                    "still suspends after 600 seconds. Choose a quota against the "
                    "remaining balance and run\n"
                    "        make snowflake-cap QUOTA=<credits>"
                )
            ),
        )
    )
    return checks


# ---------------------------------------------------------------------------
# #42 -- the schema is what the DDL says, and every visitor-scoped table
# carries demo_id
# ---------------------------------------------------------------------------


def _live_columns() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return INFORMATION_SCHEMA's columns, keyed by ``(schema, table)``.

    Base tables only. ``INFORMATION_SCHEMA.COLUMNS`` describes views as well,
    so without the predicate the two audit views of `09_audit.sql` arrive as
    two tables `schema.py` has never heard of -- which is a true statement
    about the query and a false one about the account.

    Read as CHIP_CHAT_ADMIN, and that is not incidental: INFORMATION_SCHEMA
    shows a session only the objects its role may see. The write role cannot
    see MARTS, so the same query run as CHIP_CHAT_WRITE returns five tables
    where the admin sees eight -- and every check below would pass by not
    looking at three of them.
    """
    rows = snow.query(
        f"{_preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)}\n"
        "SELECT table_schema, table_name, column_name, ordinal_position, "
        "data_type, numeric_precision, numeric_scale, is_nullable, comment\n"
        f"FROM {account.DATABASE}.INFORMATION_SCHEMA.COLUMNS\n"
        f"WHERE table_schema IN ({_SCHEMA_LIST})\n"
        "  AND (table_schema, table_name) IN (\n"
        "    SELECT table_schema, table_name\n"
        f"    FROM {account.DATABASE}.INFORMATION_SCHEMA.TABLES\n"
        "    WHERE table_type = 'BASE TABLE')\n"
        "ORDER BY table_schema, table_name, ordinal_position;"
    )[-1]
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["TABLE_SCHEMA"]).upper(), str(row["TABLE_NAME"]).upper())
        found.setdefault(key, []).append(row)
    return found


def _live_type(row: dict[str, Any]) -> str:
    """Return one live column's type, spelled the way the DDL spells it.

    Snowflake reports ``VARCHAR`` as ``TEXT`` and every fixed-point type as
    ``NUMBER`` with the precision and scale in separate columns, so a
    comparison against the DDL has to put them back together. Anything else --
    ARRAY, BOOLEAN, FLOAT, TIMESTAMP_NTZ -- comes back as itself.
    """
    data_type = str(row["DATA_TYPE"]).upper()
    if data_type == "TEXT":
        return "VARCHAR"
    if data_type == "NUMBER":
        return f"NUMBER({row['NUMERIC_PRECISION']},{row['NUMERIC_SCALE']})"
    return data_type


def _check_schema() -> list[Check]:
    """Check every declared table exists, with the columns and comments it declares.

    `tests/test_schema_layout.py` holds the DDL to `chip_chat.snowflake.schema`
    for free. This asks the other question: whether the account is what the DDL
    says. The two diverge for exactly the reasons #41's checks do -- a table
    created by hand, a column added in Snowsight, a `CREATE OR ALTER TABLE`
    edited and never applied.

    Comments are checked as strictly as columns, because they are not
    documentation here: #45's semantic view retrieves against them, and a
    comment that has drifted still answers.
    """
    live = _live_columns()
    checks: list[Check] = []

    views = snow.query(
        f"{_preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)}\n"
        "SELECT table_name FROM "
        f"{account.DATABASE}.INFORMATION_SCHEMA.VIEWS\n"
        f"WHERE table_schema = 'ACCOUNTS';"
    )[-1]
    found_views = {str(row["TABLE_NAME"]).upper() for row in views}
    expected_views = {name.upper() for name in schema.AUDIT_VIEWS}
    checks.append(
        Check(
            "#42",
            "the two audit views exist and no others",
            passed=found_views == expected_views,
            detail=(
                ", ".join(sorted(found_views))
                if found_views == expected_views
                else f"found {sorted(found_views)}, expected "
                f"{sorted(expected_views)}. The audit is how #42's second "
                "criterion is asked of the live account"
            ),
        )
    )

    unexpected = set(live) - {
        (table.schema, table.name.upper()) for table in schema.TABLES
    }
    checks.append(
        Check(
            "#42",
            "nothing is in these schemas that snowflake/sql did not create",
            passed=not unexpected,
            detail=(
                "only the fourteen declared tables"
                if not unexpected
                else "also present: "
                + ", ".join(f"{s}.{t}" for s, t in sorted(unexpected))
                + ". A table created by hand is invisible to every test in "
                "make ci, and if it is visitor-scoped it is invisible to #43's "
                "policies too"
            ),
        )
    )

    for table in schema.TABLES:
        rows = live.get((table.schema, table.name.upper()), [])
        if not rows:
            checks.append(
                Check(
                    "#42",
                    f"{table.qualified()} exists",
                    passed=False,
                    detail="not in INFORMATION_SCHEMA. Run `make snowflake-apply`.",
                )
            )
            continue

        found = [
            (str(row["COLUMN_NAME"]).upper(), _live_type(row), row["IS_NULLABLE"] == "NO")
            for row in rows
        ]
        expected = [
            (column.name.upper(), column.sql_type, column.required)
            for column in table.columns
        ]
        differences = [
            f"{was[0]}: {was[1:]} not {should[1:]}"
            for was, should in zip(found, expected, strict=False)
            if was != should
        ]
        if len(found) != len(expected):
            differences.append(f"{len(found)} columns, not {len(expected)}")
        checks.append(
            Check(
                "#42",
                f"{table.qualified()} has the columns and types it declares",
                passed=not differences,
                detail=(
                    f"{len(expected)} columns, in order"
                    if not differences
                    else "; ".join(differences[:4])
                ),
            )
        )

        uncommented = [
            str(row["COLUMN_NAME"])
            for row in rows
            if not str(row["COMMENT"] or "").strip()
        ]
        checks.append(
            Check(
                "#42",
                f"{table.qualified()} carries a comment on every column",
                passed=not uncommented,
                detail=(
                    "every column describes itself"
                    if not uncommented
                    else "no comment on: " + ", ".join(uncommented)
                ),
            )
        )
    return checks


def _check_demo_id_audit() -> list[Check]:
    """Run issue #42's second acceptance criterion against the live account.

    The criterion asks for "a query that fails if [a visitor-scoped table] is
    added without [demo_id]", and `sql/09_audit.sql` is that query. Two checks,
    and the second is the one that matters:

    **The audit is empty.** No visitor-scoped table is missing the column.

    **The audit bites.** A table with no demo_id is created in ACCOUNTS and the
    audit is asked again; it has to name it. Without this, an audit that had
    silently stopped seeing anything -- a view rewritten, a schema renamed,
    INFORMATION_SCHEMA read as too narrow a role -- would report a clean
    account forever, which is the same failure mode as a security check that
    passes because it is broken.
    """
    audit = account.table("ACCOUNTS", "tables_missing_demo_id")
    canary = account.table("ACCOUNTS", "_VERIFY_NO_DEMO_ID")
    preamble = _preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)

    def offenders() -> tuple[bool, list[str]]:
        try:
            rows = snow.query(f"{preamble}\nSELECT * FROM {audit};")[-1]
        except snow.SnowError as error:
            return False, [_refusal_line(str(error))]
        return True, [f"{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}" for row in rows]

    ran, named = offenders()
    checks = [
        Check(
            "#42",
            "every visitor-scoped table carries demo_id",
            passed=ran and not named,
            detail=(
                "the audit returns no rows"
                if ran and not named
                else f"missing demo_id: {', '.join(named)}"
                if ran
                else f"the audit did not run: {named[0]}"
            ),
        )
    ]

    snow.run_statements(
        f"{preamble}\nCREATE OR REPLACE TABLE {canary} (order_id STRING, note STRING);"
    )
    try:
        ran, named = offenders()
        caught = ran and any(name.endswith("_VERIFY_NO_DEMO_ID") for name in named)
        checks.append(
            Check(
                "#42",
                "the audit names a visitor-scoped table that has no demo_id",
                passed=caught,
                detail=(
                    "created a table without demo_id and the audit reported it"
                    if caught
                    else "created a table without demo_id and the audit stayed "
                    "empty. It is not looking at anything, and every clean run "
                    "of the check above meant nothing"
                ),
            )
        )
    finally:
        snow.run_statements(f"{preamble}\nDROP TABLE IF EXISTS {canary};")
    return checks


def _check_the_serving_joins_answer() -> list[Check]:
    """Ask the two questions the schema was shaped around, and report the counts.

    Issue #42's third criterion is that the schema is "loaded with published
    data and queryable", and queryable is a thing to demonstrate rather than
    assert. Both queries succeed against empty tables, so the row counts are
    reported either way -- zero is a load that has not happened yet
    (`make snowflake-load-sample`, or #39's nightly publish), not a broken
    schema.

    Both of them count across every visitor, which #43's policy denies to
    every role by default. They therefore run with the maintenance variable
    set, which is what makes them a question about the schema rather than a
    second and much worse test of the policy.
    """
    catalogue = account.table("CATALOGUE", "menu_items")
    prices = account.table("CATALOGUE", "item_prices")
    orders = account.table("ACCOUNTS", "orders")
    lines = account.table("ACCOUNTS", "order_items")
    # Both joins cross visitors, which is exactly what #43's policy denies by
    # default -- to every role, the owner included. Counting rows across the
    # whole population is a maintenance question, so it is asked as one; without
    # the variable these two probes report an empty database forever.
    preamble = _session(account.ADMIN_ROLE, {schema.MAINTENANCE_VARIABLE: "yes"})

    probes = (
        (
            "an order line resolves to a catalogue item",
            f"SELECT COUNT(*) AS n FROM {lines} l JOIN {catalogue} m "
            "ON m.item_id = l.item_id;",
        ),
        (
            "an order prices against the restaurant that published the price",
            f"SELECT COUNT(*) AS n FROM {orders} o JOIN {lines} l "
            "ON l.order_id = o.order_id "
            f"JOIN {prices} p ON p.item_id = l.item_id "
            "AND p.restaurant_id = o.priced_restaurant_id;",
        ),
    )
    checks: list[Check] = []
    for name, sql in probes:
        try:
            rows = snow.query(f"{preamble}\n{sql}")[-1]
            count = int(next(iter(rows[0].values()))) if rows else 0
            checks.append(
                Check(
                    "#42",
                    name,
                    passed=True,
                    detail=(
                        f"{count} rows"
                        if count
                        else "the join runs and matches nothing -- these tables "
                        "are empty. `make snowflake-load-sample` fills them"
                    ),
                )
            )
        except snow.SnowError as error:
            checks.append(
                Check("#42", name, passed=False, detail=_refusal_line(str(error)))
            )
    return checks


# ---------------------------------------------------------------------------
# #43 -- the isolation mechanism. RFC-001 §05, and one of the two launch gates
# ---------------------------------------------------------------------------


def _session(role: str, variables: dict[str, str] | None = None) -> str:
    """Return a preamble pinned to ``role`` with ``variables`` set.

    Absence is the interesting case throughout this section, so a caller that
    passes nothing gets a session that has bound no visitor -- which is what a
    connection the pool forgot to bind looks like, and what every table must
    answer with nothing.
    """
    lines = [_preamble(role, account.SERVING_WAREHOUSE)]
    lines += [f"SET {name} = '{value}';" for name, value in (variables or {}).items()]
    return "\n".join(lines)


def _visitors_seen(
    role: str, variables: dict[str, str] | None, table: str
) -> tuple[bool, str]:
    """Return whether ``table`` could be read, and which visitors came back.

    ``LISTAGG`` of the values rather than a row count, and it reads the returned
    *value* rather than the CLI's output for the reason
    :func:`_visitors_visible_to` gives: `snow sql` echoes the statements it runs,
    and those contain the visitor names, so a check that searched the output
    would find them whether or not a single row came back.
    """
    try:
        statements = snow.query(
            f"{_session(role, variables)}\n"
            f"SELECT LISTAGG(demo_id, ',') AS seen FROM {table};"
        )
    except snow.SnowError as error:
        return False, _refusal_line(str(error))
    rows = statements[-1] if statements else []
    if not rows:
        return True, ""
    return True, str(rows[0].get("SEEN") or "")


def _live_attachments() -> dict[tuple[str, str], str]:
    """Return ``(schema, table) -> policy`` for every table a policy guards.

    ``POLICY_REFERENCES`` is asked once per declared policy rather than once per
    table, and as CHIP_CHAT_ADMIN: the function shows a session only what its
    role may see, and the write role cannot see MARTS at all -- so the same
    query run as a lane role reports three unprotected marts as protected by
    not looking at them.
    """
    found: dict[tuple[str, str], str] = {}
    for policy in schema.POLICIES:
        qualified = account.table("ACCOUNTS", policy.name).upper()
        rows = snow.query(
            f"{_preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)}\n"
            "SELECT ref_schema_name, ref_entity_name FROM TABLE(\n"
            f"  {account.DATABASE}.INFORMATION_SCHEMA.POLICY_REFERENCES(\n"
            f"    POLICY_NAME => '{qualified}'))\n"
            "WHERE ref_entity_domain = 'TABLE';"
        )[-1]
        for row in rows:
            key = (
                str(row["REF_SCHEMA_NAME"]).upper(),
                str(row["REF_ENTITY_NAME"]).upper(),
            )
            found[key] = policy.name.upper()
    return found


def _unprotected() -> tuple[bool, list[str]]:
    """Return the visitor-scoped tables the live account leaves unguarded.

    The list of what *must* be protected is `09_audit.sql`'s
    ``visitor_scoped_tables`` rather than `chip_chat.snowflake.schema`, and the
    difference is the whole point of asking a live account at all: that view
    defaults to deny, so a table somebody created in Snowsight at four in the
    afternoon is on it, and a table `make ci` has never heard of is exactly the
    table with no policy on it.
    """
    view = account.table("ACCOUNTS", "visitor_scoped_tables")
    try:
        rows = snow.query(
            f"{_preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)}\n"
            f"SELECT table_schema, table_name FROM {view};"
        )[-1]
        attached = _live_attachments()
    except snow.SnowError as error:
        return False, [_refusal_line(str(error))]
    return True, sorted(
        f"{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}"
        for row in rows
        if (str(row["TABLE_SCHEMA"]).upper(), str(row["TABLE_NAME"]).upper())
        not in attached
    )


def _check_policy_coverage() -> list[Check]:
    """Every visitor-scoped table carries a policy, and the check still bites.

    Three checks, and the third is the one that matters. The first asks the
    account. The second asks whether the account agrees with the DDL about
    *which* policy guards what, which is how the roster's inversion is kept to
    the one table it was argued for. The third creates a table with a demo_id
    and no policy and requires the first to name it -- because a coverage check
    that has stopped seeing anything passes forever, and this one is the whole
    of issue #43's fourth acceptance criterion.
    """
    preamble = _preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)
    ran, named = _unprotected()
    checks = [
        Check(
            "#43",
            "every visitor-scoped table carries a row access policy",
            passed=ran and not named,
            detail=(
                "every table on ACCOUNTS.visitor_scoped_tables is guarded"
                if ran and not named
                else f"UNGUARDED: {', '.join(named)}. Every row of each is "
                "readable by every visitor. Run `make snowflake-apply`"
                if ran
                else f"the coverage query did not run: {named[0]}"
            ),
        )
    ]

    try:
        attached = _live_attachments()
    except snow.SnowError as error:
        attached = {}
        checks.append(
            Check(
                "#43",
                "the account agrees with the DDL about which policy guards what",
                passed=False,
                detail=f"POLICY_REFERENCES did not run: {_refusal_line(str(error))}",
            )
        )
    else:
        declared = {
            (table.schema, table.name.upper()): table.policy.upper()
            for table in schema.visitor_scoped()
        }
        differences = [
            f"{s}.{t}: {attached.get((s, t), 'no policy')} not {want}"
            for (s, t), want in declared.items()
            if attached.get((s, t)) != want
        ]
        differences += [
            f"{s}.{t}: guarded by {name}, and nothing declares it visitor-scoped"
            for (s, t), name in attached.items()
            if (s, t) not in declared
        ]
        checks.append(
            Check(
                "#43",
                "the account agrees with the DDL about which policy guards what",
                passed=not differences,
                detail=(
                    f"{len(declared)} tables, each carrying the policy it declares"
                    if not differences
                    else "; ".join(differences[:4])
                ),
            )
        )

    snow.run_statements(
        f"{preamble}\nCREATE OR REPLACE TABLE {UNPROTECTED_PROBE} "
        "(demo_id VARCHAR, note VARCHAR);"
    )
    try:
        ran, named = _unprotected()
        caught = ran and any(name.endswith("_VERIFY_UNPROTECTED") for name in named)
        checks.append(
            Check(
                "#43",
                "the coverage check names a visitor-scoped table with no policy",
                passed=caught,
                detail=(
                    "created a table with demo_id and no policy, and the check "
                    "reported it"
                    if caught
                    else "created a table with demo_id and no policy and the "
                    "check stayed clean. It is not looking at anything, and "
                    "every pass above meant nothing"
                ),
            )
        )
    finally:
        snow.run_statements(f"{preamble}\nDROP TABLE IF EXISTS {UNPROTECTED_PROBE};")
    return checks


def _check_default_deny_on_the_real_tables() -> list[Check]:
    """An unbound read lane sees nothing, asked of the tables themselves.

    Read-only and it costs one query, so it runs against whatever the account
    actually holds rather than against a fixture. This is issue #43's second
    acceptance criterion in the place it matters: the fixture below proves the
    policy expression denies an unbound session, and this proves the expression
    is the one attached to `orders`.

    ``persona_fixtures`` is excluded and is checked the other way round in
    :func:`_check_the_roster_inversion`: its policy is open while nothing is
    bound, which is the single deliberate exception in the mechanism.
    """
    guarded = [
        table
        for table in schema.visitor_scoped()
        if table.policy == schema.ISOLATION_POLICY
    ]
    counts = "\nUNION ALL ".join(
        f"SELECT '{table.qualified()}' AS t, COUNT(*) AS n FROM {table.qualified()}"
        for table in guarded
    )
    try:
        rows = snow.query(f"{_session('CHIP_CHAT_READ')}\n{counts};")[-1]
    except snow.SnowError as error:
        return [
            Check(
                "#43",
                "an unbound read lane sees no rows of any visitor-scoped table",
                passed=False,
                detail=f"the query did not run: {_refusal_line(str(error))}",
            )
        ]
    leaking = [str(row["T"]) for row in rows if int(row["N"]) != 0]
    return [
        Check(
            "#43",
            "an unbound read lane sees no rows of any visitor-scoped table",
            passed=len(rows) == len(guarded) and not leaking,
            detail=(
                f"{len(guarded)} tables, all of them zero rows with "
                f"{schema.SESSION_VARIABLE} unset"
                if len(rows) == len(guarded) and not leaking
                else f"LEAK: {', '.join(leaking)} returned rows to a session "
                "that had bound no visitor. This is the failure that turns a "
                "connection the pool forgot to clear into a disclosure"
            ),
        )
    ]


def _check_the_roster_inversion() -> list[Check]:
    """The one open policy is open only while nothing is bound.

    ``persona_fixtures`` is the roster the entry flow chooses a visitor's
    synthetic customer from, so it is read before any visitor exists. Two
    checks, and they are the two halves of the argument for inverting it rather
    than leaving the table unprotected: the roster is readable when nothing is
    bound, and it is *not* readable across visitors once something is.
    """
    roster = schema.table("persona_fixtures").qualified()
    checks: list[Check] = []

    try:
        theirs = snow.query(
            f"{_preamble(account.ADMIN_ROLE, account.SERVING_WAREHOUSE)}\n"
            f"SELECT COUNT(*) AS n FROM {roster};"
        )[-1]
        expected = int(next(iter(theirs[0].values()))) if theirs else 0
        mine = snow.query(
            f"{_session('CHIP_CHAT_READ')}\nSELECT COUNT(*) AS n FROM {roster};"
        )[-1]
        found = int(next(iter(mine[0].values()))) if mine else 0
    except snow.SnowError as error:
        checks.append(
            Check(
                "#43",
                "the roster is readable by a session that has bound nobody",
                passed=False,
                detail=f"the query did not run: {_refusal_line(str(error))}",
            )
        )
    else:
        checks.append(
            Check(
                "#43",
                "the roster is readable by a session that has bound nobody",
                passed=found == expected,
                detail=(
                    f"{found} fixtures, the same number the owner sees. Entry "
                    "picks a visitor's customer from this before there is a "
                    "visitor to bind"
                    if found == expected
                    else f"the read lane sees {found} of {expected} fixtures. "
                    "Entry has no roster to choose from, and the opening "
                    "message #67 needs cannot be built"
                ),
            )
        )

    ran, seen = _visitors_seen(
        "CHIP_CHAT_READ", {schema.SESSION_VARIABLE: NOBODY}, roster
    )
    checks.append(
        Check(
            "#43",
            "a bound session sees no other visitor's fixture",
            passed=ran and not seen,
            detail=(
                f"bound to {NOBODY}, which owns no fixture, and the roster "
                "returned nothing"
                if ran and not seen
                else f"LEAK: {seen}"
                if ran
                else f"the query did not run: {seen}"
            ),
        )
    )
    return checks


def _check_isolation() -> list[Check]:
    """Two visitors, one table, the real policy. Issue #43's first three criteria.

    The fixture is a throwaway table carrying the policy
    `sql/10_policies.sql` attached to `orders`, which is what makes this a test
    of the expression rather than of a copy of it. Every check runs as a lane
    role with ``USE SECONDARY ROLES NONE``, so nothing an operator's own
    privileges could do is what let a row through.
    """
    checks: list[Check] = []

    for visitor, other in ((MINE, THEIRS), (THEIRS, MINE)):
        ran, seen = _visitors_seen(
            "CHIP_CHAT_READ", {schema.SESSION_VARIABLE: visitor}, ISOLATION_PROBE
        )
        correct = ran and seen == visitor
        checks.append(
            Check(
                "#43",
                f"a session bound to {visitor} sees its own row and only its own",
                passed=correct,
                detail=(
                    f"returned {visitor} and nothing else"
                    if correct
                    else f"LEAK: {other} came back too:\n      {seen}"
                    if other in seen
                    else f"returned {seen or 'nothing'}, expected {visitor}"
                ),
            )
        )

    # The over-broad query the account lane will genuinely generate. SELECT *
    # is not a query anybody has to be tricked into writing -- it is what a
    # text-to-SQL system produces for "show me my orders" often enough.
    ran, seen = _visitors_seen(
        "CHIP_CHAT_READ",
        {schema.SESSION_VARIABLE: MINE},
        f"(SELECT * FROM {ISOLATION_PROBE}) AS unfiltered",
    )
    checks.append(
        Check(
            "#43",
            "SELECT * returns only the bound visitor's rows",
            passed=ran and seen == MINE,
            detail=(
                "a query with no WHERE clause came back filtered"
                if ran and seen == MINE
                else f"LEAK: {seen}"
            ),
        )
    )

    ran, seen = _visitors_seen("CHIP_CHAT_READ", None, ISOLATION_PROBE)
    checks.append(
        Check(
            "#43",
            f"an unset {schema.SESSION_VARIABLE} returns zero rows, not every row",
            passed=ran and not seen,
            detail=(
                "nothing came back"
                if ran and not seen
                else f"LEAK: an unbound session read {seen}. A policy that "
                "opens when the variable is missing is worse than no policy, "
                "because the pool clears that variable on every checkin"
            ),
        )
    )

    # The maintenance escape, from the wrong side. A lane role that sets
    # ALL_VISITORS must get exactly what it got without it: nothing.
    ran, seen = _visitors_seen(
        "CHIP_CHAT_READ", {schema.MAINTENANCE_VARIABLE: "yes"}, ISOLATION_PROBE
    )
    checks.append(
        Check(
            "#43",
            "a lane role cannot reach the escape by setting "
            f"{schema.MAINTENANCE_VARIABLE}",
            passed=ran and not seen,
            detail=(
                "the variable is inert without the owner role"
                if ran and not seen
                else f"LEAK: {seen}. The escape is reachable by anything that "
                "can run SET, which includes Cortex Analyst"
            ),
        )
    )

    # And from the right side, so that the check above is not vacuous. If the
    # escape does not work, `make snowflake-load-sample` reports zero rows for
    # every visitor-scoped table it has just filled.
    ran, seen = _visitors_seen(
        account.ADMIN_ROLE, {schema.MAINTENANCE_VARIABLE: "yes"}, ISOLATION_PROBE
    )
    both = ran and MINE in seen and THEIRS in seen
    checks.append(
        Check(
            "#43",
            f"the owner role with {schema.MAINTENANCE_VARIABLE} set reads every visitor",
            passed=both,
            detail=(
                "both visitors, which is what a load count and #47's nightly "
                "reset need and what makes the check above mean something"
                if both
                else f"returned {seen or 'nothing'}. Every cross-visitor "
                "maintenance query in this repository now returns zero rows"
            ),
        )
    )

    # The ops API is not exempt. #41.3 makes the same point against a throwaway
    # policy, to test the role; this makes it against the real one.
    ran, seen = _visitors_seen(
        "CHIP_CHAT_WRITE", {schema.SESSION_VARIABLE: MINE}, ISOLATION_PROBE
    )
    checks.append(
        Check(
            "#43",
            "the write role is bound by the real policy exactly as the read role is",
            passed=ran and seen == MINE,
            detail=(
                f"the ops API's role saw {MINE} and not {THEIRS}"
                if ran and seen == MINE
                else f"saw {seen or 'nothing'}, expected {MINE}"
            ),
        )
    )
    return checks


def _build_isolation_fixture() -> None:
    """Create the two-visitor probe and attach the real policy to it.

    Raises:
        snow.SnowError: If the fixture cannot be built, which usually means
            `sql/10_policies.sql` has not been applied.
    """
    result = snow.run_statements(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"USE WAREHOUSE {account.SERVING_WAREHOUSE};\n"
        f"CREATE OR REPLACE TABLE {ISOLATION_PROBE} "
        "(demo_id VARCHAR, note VARCHAR);\n"
        f"INSERT INTO {ISOLATION_PROBE} VALUES "
        f"('{MINE}', 'belongs to one visitor'), "
        f"('{THEIRS}', 'belongs to another');\n"
        f"ALTER TABLE {ISOLATION_PROBE} ADD ROW ACCESS POLICY {ISOLATION_POLICY} "
        "ON (demo_id);"
    )
    if not result.ok:
        raise snow.SnowError(
            "could not build #43's isolation fixture -- has `make snowflake-apply` "
            "run since sql/10_policies.sql landed?",
            result.output,
        )


def _drop_isolation_fixture() -> None:
    """Remove the probe. The policy itself is the account's and stays."""
    snow.run_statements(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"DROP TABLE IF EXISTS {ISOLATION_PROBE};\n"
        f"DROP TABLE IF EXISTS {UNPROTECTED_PROBE};"
    )


# ---------------------------------------------------------------------------
# The fixture, and the run
# ---------------------------------------------------------------------------


def _build_fixture() -> None:
    """Create the probe table and the throwaway policy that guards it.

    Two rows, two visitors, one row access policy keyed to the ``DEMO_ID``
    session variable -- the same mechanism RFC-001 §05 describes and #43 will
    apply to the real tables. Created by CHIP_CHAT_ADMIN, so the lane roles get
    their privileges on it from the future grants in `03_grants.sql` rather than
    from anything this file does.

    Raises:
        snow.SnowError: If the fixture cannot be built, which usually means
            `make snowflake-apply` has not been run.
    """
    snow.run_statements(f"USE ROLE {account.ADMIN_ROLE};")
    result = snow.run_statements(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"USE WAREHOUSE {account.SERVING_WAREHOUSE};\n"
        f"CREATE OR REPLACE TABLE {PROBE_TABLE} (demo_id STRING, note STRING);\n"
        f"INSERT INTO {PROBE_TABLE} VALUES "
        f"('{MINE}', 'belongs to one visitor'), "
        f"('{THEIRS}', 'belongs to another');\n"
        f"CREATE OR REPLACE ROW ACCESS POLICY {PROBE_POLICY}\n"
        "  AS (row_demo_id STRING) RETURNS BOOLEAN ->\n"
        "  row_demo_id = GETVARIABLE('DEMO_ID');\n"
        f"ALTER TABLE {PROBE_TABLE} ADD ROW ACCESS POLICY {PROBE_POLICY} "
        "ON (demo_id);"
    )
    if not result.ok:
        raise snow.SnowError("could not build the verification fixture", result.output)


def _drop_fixture() -> None:
    """Remove the probe table and policy. Leaves the account as it was found."""
    snow.run_statements(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"DROP TABLE IF EXISTS {PROBE_TABLE};\n"
        f"DROP ROW ACCESS POLICY IF EXISTS {PROBE_POLICY};"
    )
    # The publish role's positive control creates its own table and drops it. If
    # that run failed part-way the table is still there, owned by CHIP_CHAT_PUBLISH
    # rather than by the admin role -- so it is dropped as its owner.
    snow.run_statements(
        "USE SECONDARY ROLES NONE;\n"
        "USE ROLE CHIP_CHAT_PUBLISH;\n"
        f"DROP TABLE IF EXISTS {PROBE_MART};"
    )


def run(*, watch_suspend: bool = True) -> list[Check]:
    """Run every check and return the results in report order.

    The #88 monitor checks run inside the fixture's ``try`` with everything
    else, and cost nothing: they are three ``SHOW`` statements, which Snowflake
    answers without resuming a warehouse.

    Args:
        watch_suspend: Whether to include the minute spent watching the serving
            warehouse actually suspend. The settings check runs either way; this
            is the difference between "configured for 60 seconds" and "seen to
            suspend in 63".
    """
    checks = _check_warehouse_settings()
    # Before either fixture, deliberately: both probe tables live in ACCOUNTS,
    # and "nothing is in these schemas that snowflake/sql did not create" would
    # report a probe as the thing it is looking for.
    checks += _check_schema()
    checks += _check_demo_id_audit()
    checks += _check_policy_coverage()
    checks += _check_default_deny_on_the_real_tables()
    checks += _check_the_roster_inversion()
    checks += _check_the_serving_joins_answer()
    _build_isolation_fixture()
    try:
        checks += _check_isolation()
    finally:
        _drop_isolation_fixture()
    _build_fixture()
    try:
        checks += _check_read_role()
        checks += _check_write_role_under_policy()
        checks += _check_warehouse_separation()
        checks += _check_service_users()
        checks += _check_resource_monitors()
    finally:
        _drop_fixture()
    # Last, and only after the fixture is gone: everything above resumes the
    # serving warehouse, and a warehouse cannot be watched going idle while
    # something keeps waking it.
    if watch_suspend:
        checks.append(_check_suspension_observed())
    return checks


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.snowflake.verify",
        description=(
            "Check the live Snowflake account against issues #41, #42, #43 and #88."
        ),
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help=(
            "skip the minute spent watching the serving warehouse suspend. "
            "The setting is still checked; the behaviour is not"
        ),
    )
    arguments = parser.parse_args(argv)

    try:
        checks = run(watch_suspend=not arguments.no_watch)
    except snow.SnowError as error:
        print(error, file=sys.stderr)
        return 1

    criterion = ""
    for check in checks:
        if check.criterion != criterion:
            criterion = check.criterion
            print(f"\n{criterion}")
        print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name}")
        print(f"        {check.detail}")

    failed = [check for check in checks if not check.passed]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("failed:", file=sys.stderr)
        for check in failed:
            print(f"  {check.criterion} {check.name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
