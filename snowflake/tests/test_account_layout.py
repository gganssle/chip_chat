"""The SQL and `chip_chat.snowflake.account` describe the same account.

`chip_chat.snowflake.verify` asks whether the live account is what the SQL says.
This asks the cheaper question underneath it: whether the SQL still says what the
Python thinks it says. It calls Snowflake not at all, costs no credits, and runs
in `make ci`, which is where a renamed warehouse or a widened grant should be
caught -- not by a conversation, and not by a trial that expired before anybody
ran `make snowflake-verify` again.

The parsing is deliberately crude, and fails loudly rather than quietly: a
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

from chip_chat.snowflake import account
from chip_chat.snowflake.apply import SQL_DIRECTORY, ordered_files

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


def _statements(source: str) -> list[str]:
    """Return ``source``'s statements, comments stripped and whitespace collapsed.

    Comments are most of these files by volume and all of the words "GRANT" that
    are not grants, so removing them first is what keeps the grant parser from
    reading the prose above it.
    """
    uncommented = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )
    return [
        re.sub(r"\s+", " ", statement).strip()
        for statement in uncommented.split(";")
        if statement.strip()
    ]


def _flat(source: str) -> str:
    """Return ``source``'s statements as one string, whitespace collapsed.

    The SQL is column-aligned so that the grant table reads as a table, which
    means ``GRANT ROLE CHIP_CHAT_WRITE   TO USER`` has three spaces in it and a
    naive substring check misses. Every existence assertion below runs against
    this rather than against the file.
    """
    return " ; ".join(_statements(source))


def _privileges(clause: str) -> set[str]:
    """Split the privilege list of a GRANT into its parts."""
    return {part.strip().upper() for part in clause.split(",") if part.strip()}


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
# The rebuild
# ---------------------------------------------------------------------------


def test_an_apply_never_destroys(sql: dict[str, str]) -> None:
    """No numbered file replaces or drops anything holding data or a credential.

    An apply may create and may tighten. Making it able to destroy would mean a
    routine re-run could take the demo data, or revoke the key pairs an operator
    attached, and neither failure announces itself. The one exception is
    ``DROP SCHEMA CHIP_CHAT.PUBLIC``, which removes a schema Snowflake created
    and nothing here ever writes to.
    """
    allowed = {f"DROP SCHEMA IF EXISTS {account.DATABASE}.PUBLIC"}
    for name, source in sql.items():
        for statement in _statements(source):
            if statement.startswith("DROP") and statement not in allowed:
                pytest.fail(f"{name} drops something on a routine apply: {statement}")
            if re.match(
                r"CREATE OR REPLACE (DATABASE|SCHEMA|WAREHOUSE|USER|ROLE)", statement
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
    assert f"DROP DATABASE IF EXISTS {account.DATABASE}" in reset_sql

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
    assert (SQL_DIRECTORY / "optional" / "network_policy.sql").exists()


def test_the_access_table_covers_every_lane_and_schema() -> None:
    """No lane role's access to a schema is left unstated."""
    assert set(account.GRANTS) == set(account.LANE_ROLES)
    for role, access in account.GRANTS.items():
        assert set(access) == set(account.SCHEMAS), f"{role} does not mention them all"
