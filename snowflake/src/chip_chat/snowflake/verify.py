"""Hold the live Snowflake account to issue #41's four acceptance criteria.

Every check here runs against the real account. None of them reads
`snowflake/sql/` -- `tests/test_account_layout.py` does that, for free, in CI,
and it answers a different question. That test asks whether the checked-in SQL
still says what `chip_chat.snowflake.account` says. This asks whether the
account is what the SQL says, which is the question a UI click, a widened grant
or an expired trial can change without anybody editing a file.

    #41.1  the warehouse auto-suspends within 60 seconds of going idle
    #41.2  the read role cannot write
    #41.3  the write role cannot read another visitor's rows either
    #41.4  the account is rebuildable from snowflake/ in one run

The first three are checked here. The fourth is checked by running
`make snowflake-rebuild`, which tears the account down and builds it back before
this runs -- a claim about a rebuild is not something a query can answer.

Criterion 3 needs a row access policy to exist, and the real ones are #43's.
This builds a throwaway one on a throwaway table, proves the write role is
subject to it, and drops both. That is deliberately not a stand-in for #43: what
is under test is the *role*, not the policy. #41's grants are what leave
CHIP_CHAT_WRITE without ``APPLY ROW ACCESS POLICY`` and without ownership of
anything, and this is where that absence gets demonstrated rather than asserted.

Three Snowflake behaviours the checks are built around, each of which cost an
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

from chip_chat.snowflake import account, snow

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

    Args:
        watch_suspend: Whether to include the minute spent watching the serving
            warehouse actually suspend. The settings check runs either way; this
            is the difference between "configured for 60 seconds" and "seen to
            suspend in 63".
    """
    checks = _check_warehouse_settings()
    _build_fixture()
    try:
        checks += _check_read_role()
        checks += _check_write_role_under_policy()
        checks += _check_warehouse_separation()
        checks += _check_service_users()
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
        description="Check the live Snowflake account against issue #41.",
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
