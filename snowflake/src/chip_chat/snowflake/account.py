"""The account layout, as data.

`snowflake/sql/` is the only thing that creates a role, a warehouse or a schema,
and this module creates nothing. What it does is stop the rest of the tree from
retyping ``CHIP_CHAT_SERVING_WH`` as a string literal in a connection helper, a
verification script and a Databricks job, where a typo is a lane quietly running
on the wrong compute rather than an error.

`tests/test_account_layout.py` reads the SQL and asserts that what is here and
what those files declare are the same account. A warehouse renamed in one and
not the other fails ``make ci`` rather than being discovered by a conversation.

The two things worth knowing before reading the constants:

**The roles are siblings, not a ladder.** The obvious hierarchy -- write
inherits read -- is wrong for this account, because :data:`WRITE`'s read surface
is deliberately narrower than :data:`READ`'s. The ops API has no business
reading the personalization marts. :data:`GRANTS` is therefore a table rather
than a chain, and :func:`may_write` answers from it.

**Sixty seconds is a requirement, not a default.** Issue #41 says so, and
:data:`AUTO_SUSPEND_SECONDS` is what both the layout test and
``chip_chat.snowflake.verify`` hold the live account to. The trial is capped at
$400 of credits or 30 days, whichever comes first, so idle compute does not
merely cost money -- it shortens the trial.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "ADMIN_ROLE",
    "AUTO_SUSPEND_SECONDS",
    "DATABASE",
    "GRANTS",
    "LANE_ROLES",
    "PUBLISH_WAREHOUSE",
    "SCHEMAS",
    "SERVING_WAREHOUSE",
    "USERS",
    "WAREHOUSES",
    "WAREHOUSE_SIZE",
    "Access",
    "SchemaName",
    "ServiceUser",
    "Warehouse",
    "may_write",
    "readable_by",
    "schema",
    "table",
]

DATABASE: Final = "CHIP_CHAT"
"""The one database. `snowflake/sql/02_database.sql` creates it."""

SchemaName = Literal["CATALOGUE", "ACCOUNTS", "MARTS"]

SCHEMAS: Final[tuple[SchemaName, ...]] = ("CATALOGUE", "ACCOUNTS", "MARTS")
"""The three populations that must not blur.

``CATALOGUE`` is real: harvested from Chipotle's published pages, versioned,
cited. ``ACCOUNTS`` is synthetic and visitor-scoped -- every table in it carries
``demo_id``, which is what the row access policies in #43 attach to. ``MARTS``
is derived: computed overnight in the lakehouse and published in (#39), read by
the personalization lane and written by nobody else.
"""

AUTO_SUSPEND_SECONDS: Final = 60
"""Issue #41's first acceptance criterion, as a number the tests can hold."""

WAREHOUSE_SIZE: Final = "X-Small"
"""What ``SHOW WAREHOUSES`` calls ``XSMALL``. Both spellings are Snowflake's."""

SERVING_WAREHOUSE: Final = "CHIP_CHAT_SERVING_WH"
"""Every turn of every conversation."""

PUBLISH_WAREHOUSE: Final = "CHIP_CHAT_PUBLISH_WH"
"""The nightly publish, so a batch cannot queue in front of a conversation."""

ADMIN_ROLE: Final = "CHIP_CHAT_ADMIN"
"""Owns the database and everything in it. Not a runtime identity."""


@dataclass(frozen=True, slots=True)
class Access:
    """What one lane role may do to one schema.

    Attributes:
        read: Whether the role may ``SELECT`` from the schema at all.
        write: Whether the role may change rows in it.
    """

    read: bool
    write: bool


@dataclass(frozen=True, slots=True)
class Warehouse:
    """One warehouse and the timeout that suits what runs on it.

    Attributes:
        name: The warehouse name.
        statement_timeout_seconds: ``STATEMENT_TIMEOUT_IN_SECONDS``. A minute for
            serving, because a turn that has not answered in a minute has already
            failed as a conversation; an hour for the publish, because a nightly
            batch legitimately takes minutes.
    """

    name: str
    statement_timeout_seconds: int


WAREHOUSES: Final[tuple[Warehouse, ...]] = (
    Warehouse(SERVING_WAREHOUSE, statement_timeout_seconds=60),
    Warehouse(PUBLISH_WAREHOUSE, statement_timeout_seconds=3600),
)
"""Both warehouses. Both X-Small, both suspending after sixty seconds."""

LANE_ROLES: Final = ("CHIP_CHAT_READ", "CHIP_CHAT_WRITE", "CHIP_CHAT_PUBLISH")
"""The three runtime roles. :data:`ADMIN_ROLE` is not one of them, deliberately:
no service user holds it, so nothing a conversation touches can own a table."""

GRANTS: Final[dict[str, dict[SchemaName, Access]]] = {
    "CHIP_CHAT_READ": {
        "CATALOGUE": Access(read=True, write=False),
        "ACCOUNTS": Access(read=True, write=False),
        "MARTS": Access(read=True, write=False),
    },
    "CHIP_CHAT_WRITE": {
        "CATALOGUE": Access(read=True, write=False),
        "ACCOUNTS": Access(read=True, write=True),
        "MARTS": Access(read=False, write=False),
    },
    "CHIP_CHAT_PUBLISH": {
        "CATALOGUE": Access(read=True, write=True),
        "ACCOUNTS": Access(read=False, write=False),
        "MARTS": Access(read=True, write=True),
    },
}
"""The whole security boundary as a table. `snowflake/sql/03_grants.sql` is this
table spelled as privileges, and `test_account_layout.py` checks that it still
is -- in particular that no ``GRANT`` in that file gives a mutating privilege to
a role this table says may not write."""


@dataclass(frozen=True, slots=True)
class ServiceUser:
    """One service identity, and the single role it holds.

    One user per lane is what makes "only the ops API gets the write role" a
    checkable sentence rather than a convention about which ``--role`` to pass.

    Attributes:
        name: The Snowflake user.
        role: Its only granted role, and its ``DEFAULT_ROLE``.
        warehouse: Its ``DEFAULT_WAREHOUSE`` -- and the only one it holds
            ``USAGE`` on, which is what keeps the batch off the serving compute.
        runs: What actually connects as it, for the reader.
    """

    name: str
    role: str
    warehouse: str
    runs: str


USERS: Final[tuple[ServiceUser, ...]] = (
    ServiceUser(
        "CHIP_CHAT_APP",
        role="CHIP_CHAT_READ",
        warehouse=SERVING_WAREHOUSE,
        runs="the chat app and the Foundry agent",
    ),
    ServiceUser(
        "CHIP_CHAT_OPS",
        role="CHIP_CHAT_WRITE",
        warehouse=SERVING_WAREHOUSE,
        runs="the Azure Functions ops API",
    ),
    ServiceUser(
        "CHIP_CHAT_PUBLISHER",
        role="CHIP_CHAT_PUBLISH",
        warehouse=PUBLISH_WAREHOUSE,
        runs="the nightly Databricks publish",
    ),
)
"""The three service users. None of them has a credential until an operator
attaches a key pair; `snowflake/sql/04_users.sql` says why."""


def schema(name: SchemaName) -> str:
    """Return the database-qualified name of ``name``: ``CHIP_CHAT.ACCOUNTS``."""
    return f"{DATABASE}.{name}"


def table(schema_name: SchemaName, name: str) -> str:
    """Return the fully qualified name of ``name`` in ``schema_name``."""
    return f"{schema(schema_name)}.{name}"


def may_write(role: str, schema_name: SchemaName) -> bool:
    """Whether ``role`` may change rows in ``schema_name``.

    Args:
        role: One of :data:`LANE_ROLES`.
        schema_name: One of :data:`SCHEMAS`.

    Returns:
        True if the role holds mutating privileges there.

    Raises:
        KeyError: If ``role`` is not a lane role. :data:`ADMIN_ROLE` is not one,
            and asking about it is a question about ownership rather than about
            grants.
    """
    return GRANTS[role][schema_name].write


def readable_by(role: str) -> Iterator[SchemaName]:
    """Yield the schemas ``role`` may ``SELECT`` from, in :data:`SCHEMAS` order."""
    access = GRANTS[role]
    for name in SCHEMAS:
        if access[name].read:
            yield name
