"""The SQL and `chip_chat.snowflake.account` describe the same account.

`chip_chat.snowflake.verify` asks whether the live account is what the SQL says.
This asks the cheaper question underneath it: whether the SQL still says what the
Python thinks it says. It calls Snowflake not at all, costs no credits, and runs
in `make ci`, which is where a renamed warehouse or a widened grant should be
caught -- not by a conversation, and not by a trial that expired before anybody
ran `make snowflake-verify` again.

The parsing lives in `sql_text.py` beside this file, shared with
`test_schema_layout.py`. It is deliberately crude and fails loudly rather than
quietly: a
regular expression that stops matching because a file was reformatted produces
an assertion about a missing grant, which is a bad afternoon; a test that
silently matches nothing produces a green tick over an account nobody checked,
which is worse.

The test with the most value per line is
:func:`test_no_grant_contradicts_the_access_table`. `account.GRANTS` is the
security boundary written as a table three roles wide and three schemas deep,
and `03_grants.sql` is that table spelled as privileges. That test reads every
GRANT in the file and refuses any that the table does not allow -- so widening
the ops API's reach to the personalization marts is a failing test rather than a
line nobody re-reads.
"""

import re

import pytest
from sql_text import flat as _flat
from sql_text import privileges as _privileges
from sql_text import statements as _statements

from chip_chat.snowflake import account
from chip_chat.snowflake.apply import CAP_FILE, SQL_DIRECTORY, ordered_files

# Privileges that change something. CREATE is here because a role that may
# create a table in a schema may write to the table it created.
MUTATING = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "MODIFY",
        "OWNERSHIP",
        "CREATE TABLE",
        "CREATE VIEW",
        "CREATE STAGE",
        "CREATE SCHEMA",
        "APPLY ROW ACCESS POLICY",
        "APPLY MASKING POLICY",
    }
)

# Privileges that let a role reach the data at all.
READING = frozenset({"SELECT", "USAGE", "REFERENCES"})


@pytest.fixture(scope="module")
def sql() -> dict[str, str]:
    """Return every numbered SQL file's text, keyed by filename."""
    files = ordered_files()
    assert files, f"no SQL files under {SQL_DIRECTORY}"
    return {path.name: path.read_text() for path in files}


@pytest.fixture(scope="module")
def all_sql(sql: dict[str, str]) -> str:
    """Return the numbered files concatenated, for existence questions."""
    return "\n".join(sql.values())


@pytest.fixture(scope="module")
def reset_sql() -> str:
    """Return `sql/optional/reset.sql`, which the rebuild claim depends on."""
    path = SQL_DIRECTORY / "optional" / "reset.sql"
    assert path.exists(), f"{path} is missing -- `make snowflake-rebuild` needs it"
    return path.read_text()


# ---------------------------------------------------------------------------
# The objects
# ---------------------------------------------------------------------------


def test_every_warehouse_is_created_and_sized(sql: dict[str, str]) -> None:
    source = _flat(sql["01_warehouses.sql"])
    for warehouse in account.WAREHOUSES:
        assert f"CREATE WAREHOUSE IF NOT EXISTS {warehouse.name}" in source, (
            f"{warehouse.name} is in account.py but nothing creates it"
        )
        settings = "\n".join(
            statement
            for statement in _statements(sql["01_warehouses.sql"])
            if statement.startswith(f"ALTER WAREHOUSE {warehouse.name} SET")
        )
        assert settings, f"nothing re-asserts {warehouse.name}'s settings on a re-run"
        assert f"AUTO_SUSPEND = {account.AUTO_SUSPEND_SECONDS}" in settings, (
            f"{warehouse.name} does not set the 60-second auto-suspend #41 requires"
        )
        assert "WAREHOUSE_SIZE = XSMALL" in settings, f"{warehouse.name} is not X-Small"
        assert "ENABLE_QUERY_ACCELERATION = FALSE" in settings, (
            f"{warehouse.name} leaves query acceleration on, which bills separately"
        )
        assert (
            f"STATEMENT_TIMEOUT_IN_SECONDS = {warehouse.statement_timeout_seconds}"
            in settings
        ), f"{warehouse.name}'s statement timeout disagrees with account.py"


def test_every_schema_is_created_with_managed_access(sql: dict[str, str]) -> None:
    source = _flat(sql["02_database.sql"])
    assert f"CREATE DATABASE IF NOT EXISTS {account.DATABASE}" in source
    for name in account.SCHEMAS:
        qualified = account.schema(name)
        assert f"CREATE SCHEMA IF NOT EXISTS {qualified}" in source, (
            f"{qualified} is in account.py but nothing creates it"
        )
        assert f"ALTER SCHEMA {qualified} ENABLE MANAGED ACCESS" in source, (
            f"{qualified} does not re-assert managed access, so a schema someone "
            "turned it off on would stay that way"
        )
    assert f"DROP SCHEMA IF EXISTS {account.DATABASE}.PUBLIC" in source, (
        "PUBLIC is still there. Snowflake grants the PUBLIC role usage on it."
    )


def test_every_role_is_created(sql: dict[str, str]) -> None:
    source = _flat(sql["00_roles.sql"])
    for role in (account.ADMIN_ROLE, *account.LANE_ROLES):
        assert f"CREATE ROLE IF NOT EXISTS {role}" in source, (
            f"{role} is in account.py but nothing creates it"
        )


def test_every_service_user_gets_one_role_and_no_secondary_roles(
    sql: dict[str, str],
) -> None:
    source = _flat(sql["04_users.sql"])
    statements = _statements(sql["04_users.sql"])
    for user in account.USERS:
        assert f"CREATE USER IF NOT EXISTS {user.name}" in source
        assert f"GRANT ROLE {user.role} TO USER {user.name}" in source

        granted = {
            match.group(1)
            for statement in statements
            if (
                match := re.fullmatch(rf"GRANT ROLE (\w+) TO USER {user.name}", statement)
            )
        }
        assert granted == {user.role}, (
            f"{user.name} is granted {sorted(granted)}. One role per credential is "
            "what makes 'only the ops API gets the write role' checkable."
        )

        alter = next(
            (s for s in statements if s.startswith(f"ALTER USER {user.name} SET")), ""
        )
        assert "DEFAULT_SECONDARY_ROLES = ()" in alter, (
            f"{user.name} does not pin DEFAULT_SECONDARY_ROLES to empty. Snowflake's "
            "default is ALL, which gives a session every role its user holds."
        )
        assert f"DEFAULT_ROLE = {user.role}" in alter
        assert f"DEFAULT_WAREHOUSE = {user.warehouse}" in alter

    assert "RSA_PUBLIC_KEY" not in source, (
        "a public key in the checked-in SQL is a credential in every clone, and an "
        "apply would revoke whatever key the operator attached"
    )


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def _schema_grants(source: str) -> list[tuple[str, str, set[str]]]:
    """Return every schema-scoped GRANT as ``(role, schema, privileges)``."""
    patterns = (
        r"GRANT (?P<privileges>.+?) ON (?:ALL|FUTURE) \w+ IN SCHEMA "
        rf"{account.DATABASE}\.(?P<schema>\w+) TO ROLE (?P<role>\w+)",
        rf"GRANT (?P<privileges>.+?) ON SCHEMA {account.DATABASE}\.(?P<schema>\w+) "
        r"TO ROLE (?P<role>\w+)",
    )
    found = []
    for statement in _statements(source):
        for pattern in patterns:
            match = re.fullmatch(pattern, statement)
            if match:
                found.append(
                    (
                        match.group("role"),
                        match.group("schema"),
                        _privileges(match.group("privileges")),
                    )
                )
                break
    return found


def test_the_grant_parser_still_reads_the_file(sql: dict[str, str]) -> None:
    """A parser that matches nothing would make every test below vacuous."""
    grants = _schema_grants(sql["03_grants.sql"])
    assert len(grants) > 20, (
        f"only {len(grants)} schema grants parsed out of 03_grants.sql. The file "
        "has been reformatted into a shape this test no longer understands, and "
        "the checks below are now proving nothing."
    )


def test_no_grant_contradicts_the_access_table(sql: dict[str, str]) -> None:
    """Every GRANT in the file is one `account.GRANTS` permits."""
    for role, schema_name, privileges in _schema_grants(sql["03_grants.sql"]):
        if role not in account.GRANTS:
            continue  # CHIP_CHAT_ADMIN owns the schemas; it is not a lane.
        access = account.GRANTS[role][schema_name]  # type: ignore[index]

        mutating = privileges & MUTATING
        assert not mutating or access.write, (
            f"03_grants.sql gives {role} {sorted(mutating)} on {schema_name}, which "
            f"account.GRANTS says it may not write to. One of the two is wrong, and "
            f"if it is the table then this is a widened boundary."
        )
        reading = privileges & READING
        assert not reading or access.read, (
            f"03_grants.sql gives {role} {sorted(reading)} on {schema_name}, which "
            f"account.GRANTS says it may not read at all."
        )


def test_every_access_the_table_claims_is_actually_granted(sql: dict[str, str]) -> None:
    """And the other direction: the table does not promise what the SQL withholds."""
    grants = _schema_grants(sql["03_grants.sql"])
    for role, access_by_schema in account.GRANTS.items():
        for schema_name, access in access_by_schema.items():
            privileges: set[str] = set()
            for granted_role, granted_schema, granted in grants:
                if granted_role == role and granted_schema == schema_name:
                    privileges |= granted
            if access.read:
                assert "USAGE" in privileges, (
                    f"account.GRANTS says {role} reads {schema_name}, but nothing "
                    "grants it USAGE on the schema, so it cannot reach a table in it"
                )
                assert "SELECT" in privileges
            if access.write:
                assert privileges & MUTATING, (
                    f"account.GRANTS says {role} writes {schema_name}, but no "
                    "mutating privilege is granted there"
                )
            if not access.read:
                assert not privileges, (
                    f"account.GRANTS says {role} cannot see {schema_name} at all, "
                    f"but it is granted {sorted(privileges)} on it"
                )


def test_no_lane_role_can_touch_a_policy_or_own_anything(sql: dict[str, str]) -> None:
    """The two privileges whose absence holds #43's row access policies on.

    A role that can apply a policy can detach one, and a role that owns a table
    can drop it -- and a dropped table has no policy. Neither is granted to a
    lane role anywhere in `snowflake/sql/`.
    """
    for name, source in sql.items():
        for statement in _statements(source):
            if not statement.startswith("GRANT"):
                continue
            for role in account.LANE_ROLES:
                if not statement.endswith(f"TO ROLE {role}"):
                    continue
                for forbidden in ("APPLY ROW ACCESS POLICY", "APPLY MASKING POLICY"):
                    assert forbidden not in statement, (
                        f"{name} grants {forbidden} to {role}. A role that can apply "
                        "a policy can detach the one that keeps visitors apart."
                    )
                assert "OWNERSHIP" not in statement, (
                    f"{name} transfers ownership to {role}. An owner can drop what "
                    "it owns, and #43's policies live on tables."
                )


def test_the_warehouse_split_is_enforced_by_grants(sql: dict[str, str]) -> None:
    """No lane role holds USAGE on both warehouses.

    "A heavy batch job cannot make a conversation slow" is a claim about
    privileges. A role that cannot name a warehouse cannot run on it.
    """
    source = sql["03_grants.sql"]
    holders: dict[str, set[str]] = {}
    for statement in _statements(source):
        match = re.fullmatch(
            r"GRANT (?P<privileges>.+?) ON WAREHOUSE (?P<warehouse>\w+) "
            r"TO ROLE (?P<role>\w+)",
            statement,
        )
        if match and "USAGE" in _privileges(match.group("privileges")):
            holders.setdefault(match.group("role"), set()).add(match.group("warehouse"))

    for role in account.LANE_ROLES:
        assert len(holders.get(role, set())) == 1, (
            f"{role} holds USAGE on {sorted(holders.get(role, set()))}. Exactly one "
            "warehouse per lane role is what keeps the batch off the serving compute."
        )
    for user in account.USERS:
        assert holders[user.role] == {user.warehouse}, (
            f"{user.name} defaults to {user.warehouse} but its role "
            f"{user.role} holds {sorted(holders[user.role])}"
        )


# ---------------------------------------------------------------------------
# The credit ceiling
# ---------------------------------------------------------------------------


def _triggers(statement: str) -> dict[str, tuple[int, ...]]:
    """Return a resource monitor statement's triggers, keyed by action.

    ``ON 80 PERCENT DO NOTIFY ON 300 PERCENT DO SUSPEND`` becomes
    ``{"NOTIFY": (80,), "SUSPEND": (300,)}``. Thresholds come back in the order
    the file writes them, which is what lets a test say the notifications come
    before the suspension rather than merely that both exist.
    """
    found: dict[str, tuple[int, ...]] = {}
    for threshold, action in re.findall(
        r"ON (\d+) PERCENT DO (SUSPEND_IMMEDIATE|SUSPEND|NOTIFY)", statement
    ):
        found[action] = (*found.get(action, ()), int(threshold))
    return found


def test_every_resource_monitor_is_created_and_re_asserted(sql: dict[str, str]) -> None:
    """`05_resource_monitors.sql` is `account.MONITORS` spelled as SQL.

    Both the CREATE and the ALTER are checked against the same record. The ALTER
    is what narrows a quota somebody raised in the UI on the next apply, so a
    file that created the right monitor and re-asserted the wrong one would be a
    guardrail that quietly stops matching the account it is named after.
    """
    statements = _statements(sql["05_resource_monitors.sql"])
    for monitor in account.MONITORS:
        created = next(
            (
                statement
                for statement in statements
                if statement.startswith(
                    f"CREATE RESOURCE MONITOR IF NOT EXISTS {monitor.name}"
                )
            ),
            "",
        )
        assert created, f"{monitor.name} is in account.py but nothing creates it"
        assert "FREQUENCY = DAILY" in created, (
            f"{monitor.name} does not reset daily, so a suspension lasts for the "
            "rest of the trial rather than until tomorrow"
        )

        altered = next(
            (
                statement
                for statement in statements
                if statement.startswith(f"ALTER RESOURCE MONITOR {monitor.name} SET")
            ),
            "",
        )
        assert altered, (
            f"nothing re-asserts {monitor.name}'s quota and triggers on a re-run, "
            "so a quota somebody raised in the UI would stay raised"
        )

        for kind, statement in (("created", created), ("re-asserted", altered)):
            assert f"CREDIT_QUOTA = {monitor.daily_credit_quota}" in statement, (
                f"{monitor.name} as {kind} disagrees with account.py's quota"
            )
            triggers = _triggers(statement)
            assert triggers.get("NOTIFY", ()) == monitor.notify_at_percent, (
                f"{monitor.name} as {kind} notifies at {triggers.get('NOTIFY')}, "
                f"account.py says {monitor.notify_at_percent}"
            )
            assert triggers.get("SUSPEND", ()) == (monitor.suspend_at_percent,), (
                f"{monitor.name} as {kind} suspends at {triggers.get('SUSPEND')}, "
                f"account.py says {monitor.suspend_at_percent}%"
            )
            assert triggers.get("SUSPEND_IMMEDIATE", ()) == (
                monitor.suspend_immediate_at_percent,
            ), f"{monitor.name} as {kind} kills statements at the wrong threshold"


def test_each_monitor_is_assigned_to_exactly_its_own_warehouse(
    sql: dict[str, str],
) -> None:
    """A monitor nothing is assigned to counts nothing.

    And one monitor shared between both warehouses would let the publish spend
    the serving lane's quota, which is the failure two separate warehouses were
    bought to avoid.
    """
    assignments = {
        match.group("warehouse"): match.group("monitor")
        for statement in _statements(sql["05_resource_monitors.sql"])
        if (
            match := re.fullmatch(
                r"ALTER WAREHOUSE (?P<warehouse>\w+) SET "
                r"RESOURCE_MONITOR = (?P<monitor>\w+)",
                statement,
            )
        )
    }
    assert assignments == {m.warehouse: m.name for m in account.MONITORS}


def test_the_frequency_is_set_once_and_never_re_asserted(sql: dict[str, str]) -> None:
    """Re-asserting START_TIMESTAMP would zero the counter on every apply.

    FREQUENCY may only be set together with START_TIMESTAMP, and setting
    START_TIMESTAMP restarts the counting period. An ALTER that re-asserted
    either would hand a runaway a fresh quota every time somebody ran `make
    snowflake-apply` -- which makes this the one property in these files that
    cannot be made idempotent the way `01_warehouses.sql` is. The CREATE owns it;
    `chip_chat.snowflake.verify` is what holds the live account to it instead.
    """
    sources = {
        "05_resource_monitors.sql": sql["05_resource_monitors.sql"],
        CAP_FILE.name: CAP_FILE.read_text(),
    }
    for name, source in sources.items():
        for statement in _statements(source):
            if not statement.startswith("ALTER RESOURCE MONITOR"):
                continue
            for property_name in ("FREQUENCY", "START_TIMESTAMP"):
                assert property_name not in statement, (
                    f"{name} re-asserts {property_name} on an existing monitor, "
                    "which restarts the counting period and zeroes used_credits"
                )


def test_no_monitor_suspends_the_serving_lane_on_a_busy_day(sql: dict[str, str]) -> None:
    """The asymmetry between the two monitors, as an assertion.

    A suspended publish costs a stale mart until tomorrow. A suspended serving
    warehouse costs the demo, mid-conversation. So the serving lane may only be
    suspended well past its quota -- at a number no demo reaches on a warehouse
    where every statement times out after sixty seconds -- while the publish lane
    is suspended at its quota.
    """
    by_warehouse = {monitor.warehouse: monitor for monitor in account.MONITORS}
    serving = by_warehouse[account.SERVING_WAREHOUSE]
    publish = by_warehouse[account.PUBLISH_WAREHOUSE]

    assert serving.suspend_at_percent >= 200, (
        "the serving warehouse is suspended at "
        f"{serving.suspend_at_percent}% of a quota a genuinely busy demo day can "
        "reach, which ends a conversation over an ordinary afternoon"
    )
    assert publish.suspend_at_percent <= 100, (
        "the publish warehouse is not suspended at its quota, so the runaway "
        "nightly job this monitor exists for runs on"
    )
    for monitor in account.MONITORS:
        assert monitor.notify_at_percent, f"{monitor.name} warns nobody first"
        assert max(monitor.notify_at_percent) <= monitor.suspend_at_percent, (
            f"{monitor.name} suspends before its last notification, so the first "
            "anybody hears of the ceiling is the demo stopping"
        )
        assert monitor.suspend_at_percent < monitor.suspend_immediate_at_percent, (
            f"{monitor.name} kills running statements before it stops taking new ones"
        )


def test_the_total_cap_is_opt_in_and_has_no_default_quota() -> None:
    """The one number that has to come from an operator is not guessed here.

    A daily quota is arithmetic anybody can redo. The cap on the whole trial is
    read off the remaining balance: too low suspends the demo mid-conversation,
    too high does nothing while looking handled. So the file that sets it takes
    its quota as a variable, and lives where an apply will not run it.
    """
    source = CAP_FILE.read_text()
    assert CAP_FILE.name not in {path.name for path in ordered_files()}, (
        "an apply would set an account-wide credit cap from a number no file here "
        "is in a position to know"
    )
    assert "<% trial_credit_quota %>" in source, (
        "the quota is hard-coded, which is the guess this file exists to refuse"
    )
    assert re.search(r"CREDIT_QUOTA = \d", source) is None
    assert f"ALTER ACCOUNT SET RESOURCE_MONITOR = {account.TRIAL_MONITOR}" in _flat(
        source
    ), (
        "the monitor is created and never set on the account, so it counts "
        "nothing -- and the account level is the only one that sees COMPUTE_WH"
    )
    assert _triggers(_flat(source)).get("SUSPEND"), (
        "the total cap only notifies. A notification does not stop a runaway, and "
        "on a trial account nobody has switched notifications on yet"
    )


# ---------------------------------------------------------------------------
# The rebuild
# ---------------------------------------------------------------------------


def test_an_apply_never_destroys(sql: dict[str, str]) -> None:
    """No numbered file replaces or drops anything holding data or a credential.

    An apply may create and may tighten. Making it able to destroy would mean a
    routine re-run could take the demo data, or revoke the key pairs an operator
    attached, and neither failure announces itself. The one exception is
    ``DROP SCHEMA CHIP_CHAT.PUBLIC``, which removes a schema Snowflake created
    and nothing here ever writes to.

    RESOURCE MONITOR is in the replace list for a reason of its own: replacing a
    monitor is how you accidentally reset ``used_credits`` to zero, so a
    ``CREATE OR REPLACE`` here would be an apply that hands a runaway a fresh
    quota rather than an apply that destroys something.

    TABLE joins the list from #42 onward. The tables are written as ``CREATE OR
    ALTER TABLE``, which converges an existing table to the declaration and
    keeps its rows; the one-word edit to ``OR REPLACE`` would empty every one of
    them on the next apply while reading almost identically in a diff. VIEW is
    deliberately absent -- a view holds nothing, and `09_audit.sql` replaces two
    of them precisely so that a reworded definition reaches the account.
    """
    allowed = {f"DROP SCHEMA IF EXISTS {account.DATABASE}.PUBLIC"}
    for name, source in sql.items():
        for statement in _statements(source):
            if statement.startswith("DROP") and statement not in allowed:
                pytest.fail(f"{name} drops something on a routine apply: {statement}")
            if re.match(
                r"CREATE OR REPLACE "
                r"(DATABASE|SCHEMA|WAREHOUSE|USER|ROLE|TABLE|RESOURCE MONITOR)",
                statement,
            ):
                pytest.fail(
                    f"{name} replaces a live object on a routine apply: {statement}"
                )


def test_reset_drops_everything_the_apply_creates(reset_sql: str) -> None:
    """Otherwise "rebuildable in one run" is a claim nobody can test."""
    for role in (account.ADMIN_ROLE, *account.LANE_ROLES):
        assert f"DROP ROLE IF EXISTS {role}" in reset_sql
    for warehouse in account.WAREHOUSES:
        assert f"DROP WAREHOUSE IF EXISTS {warehouse.name}" in reset_sql
    for user in account.USERS:
        assert f"DROP USER IF EXISTS {user.name}" in reset_sql
    for monitor in account.MONITORS:
        assert f"DROP RESOURCE MONITOR IF EXISTS {monitor.name}" in reset_sql
    assert f"DROP DATABASE IF EXISTS {account.DATABASE}" in reset_sql

    # The account-wide cap too, even though no apply creates it -- the same
    # treatment the network policies get, and for the same reason: an operator
    # may have attached it, so a teardown has to be able to detach it.
    assert f"DROP RESOURCE MONITOR IF EXISTS {account.TRIAL_MONITOR}" in reset_sql
    unset = reset_sql.index("ALTER ACCOUNT UNSET RESOURCE_MONITOR")
    assert unset < reset_sql.index(
        f"DROP RESOURCE MONITOR IF EXISTS {account.TRIAL_MONITOR}"
    ), (
        "the monitor is dropped while it is still set on the account, which "
        "Snowflake refuses -- and the refusal stops the rest of the teardown"
    )

    database = reset_sql.index(f"DROP DATABASE IF EXISTS {account.DATABASE}")
    admin = reset_sql.index(f"DROP ROLE IF EXISTS {account.ADMIN_ROLE}")
    assert database < admin, (
        "the database is dropped after the role that owns it, which leaves an "
        "orphaned database no lane role can reach"
    )


def test_the_optional_files_are_not_part_of_an_apply() -> None:
    """`optional/` is where the two files that need a decision live."""
    names = {path.name for path in ordered_files()}
    assert "reset.sql" not in names, "an apply would tear the account down"
    assert "network_policy.sql" not in names, (
        "an apply would attach a network policy built from placeholder addresses"
    )
    assert CAP_FILE.name not in names, (
        "an apply would cap the account from a number no file here can know"
    )
    assert (SQL_DIRECTORY / "optional" / "network_policy.sql").exists()
    assert CAP_FILE.exists()


def test_the_access_table_covers_every_lane_and_schema() -> None:
    """No lane role's access to a schema is left unstated."""
    assert set(account.GRANTS) == set(account.LANE_ROLES)
    for role, access in account.GRANTS.items():
        assert set(access) == set(account.SCHEMAS), f"{role} does not mention them all"
