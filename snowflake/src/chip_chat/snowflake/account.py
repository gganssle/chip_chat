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

**Two of the numbers here are guardrails and one of them is deliberately
missing.** :data:`MONITORS` caps what each warehouse may spend in a day, off
arithmetic anybody can redo. :data:`TRIAL_MONITOR` caps what the account may
spend in total, off the remaining balance -- which is why it is a name here and
a file in ``sql/optional/`` rather than a number.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "ADMIN_ROLE",
    "ALL_SCHEMAS",
    "AUTO_SUSPEND_SECONDS",
    "DATABASE",
    "GRANTS",
    "LANE_ROLES",
    "MONITORS",
    "PUBLISHED_ACCOUNT_TABLES",
    "PUBLISH_WAREHOUSE",
    "SCHEMAS",
    "SERVING_WAREHOUSE",
    "STAGING_ACCESS",
    "STAGING_SCHEMA",
    "TRIAL_MONITOR",
    "USERS",
    "WAREHOUSES",
    "WAREHOUSE_SIZE",
    "Access",
    "ResourceMonitor",
    "SchemaName",
    "ServiceUser",
    "Warehouse",
    "access",
    "may_write",
    "may_write_table",
    "readable_by",
    "schema",
    "table",
]

DATABASE: Final = "CHIP_CHAT"
"""The one database. `snowflake/sql/02_database.sql` creates it."""

SchemaName = Literal["CATALOGUE", "ACCOUNTS", "MARTS", "STAGING"]

SCHEMAS: Final[tuple[SchemaName, ...]] = ("CATALOGUE", "ACCOUNTS", "MARTS")
"""The three populations that must not blur.

``CATALOGUE`` is real: harvested from Chipotle's published pages, versioned,
cited. ``ACCOUNTS`` is synthetic and visitor-scoped -- every table in it carries
``demo_id``, which is what the row access policies in #43 attach to. ``MARTS``
is derived: computed overnight in the lakehouse and published in (#39), read by
the personalization lane and written by nobody else.

:data:`STAGING_SCHEMA` is deliberately not one of them. These three are the
lanes a conversation reads; that one is a loading dock, and every check built
over this tuple -- the grants table, the schema audit, `verify`'s
INFORMATION_SCHEMA predicate -- is about the lanes.
"""

STAGING_SCHEMA: Final = "STAGING"
"""The fourth schema. Not a population: the loading dock #39 writes into.

The nightly publish lands each incoming generation here and then makes it live
with one ``INSERT OVERWRITE`` into the serving table. It cannot land it beside
the target, because :data:`GRANTS` gives ``CHIP_CHAT_READ`` ``SELECT ON FUTURE
TABLES`` in all three lanes -- so an ``orders_incoming`` in ACCOUNTS would be a
complete unscoped copy of the population, readable by the identity the agent
runs as and covered by no row access policy, since #43 attaches policies to
tables by name.

So this schema is granted to ``CHIP_CHAT_PUBLISH`` and to nobody else, holds no
declared table, and is empty between runs: a staging table is dropped when its
swap succeeds, which makes one that is still there the evidence of a run that
stopped. :data:`STAGING_ACCESS` is the whole of its security boundary and
`databricks/src/chip_chat/databricks/publish.py` is the job that uses it.
"""

ALL_SCHEMAS: Final[tuple[SchemaName, ...]] = (*SCHEMAS, STAGING_SCHEMA)
"""Every schema ``sql/02_database.sql`` creates, lanes first.

What an existence check iterates. :data:`SCHEMAS` is what a check about the
serving boundary iterates, and the difference between the two tuples is the
distinction the paragraph above draws.
"""

PUBLISHED_ACCOUNT_TABLES: Final = ("orders", "order_items", "loyalty_ledger")
"""The account tables the nightly publish is trusted with, and no others.

These are exactly ``chip_chat.snowflake.schema.MART_INPUTS`` -- the tables the
gold marts are computed from -- and `test_account_layout.py` asserts the two
lists are one list. The publisher writes what the marts were derived from and
nothing else.

The three that are missing are the point. ``demo_visitors`` holds all three
columns a visitor may edit and is the one account table a visitor writes to, so
a nightly overwrite would delete every edit made that day; RFC-001 §04 also
rests its answer to PRD Q2 on the publisher physically not being able to read
it. ``personas`` and ``persona_fixtures`` are reference rows the generator emits
once. All three reach Snowflake through ``chip_chat.snowflake.load``, run by an
operator as :data:`ADMIN_ROLE`.
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
        read: Whether the role may ``SELECT`` from the schema AT LARGE -- every
            table in it, including the ones a later issue adds.
        write: Whether the role may change rows in it at large.
        tables: Individual tables the role may read and write where the schema
            itself is closed to it. Empty for every access decided at the schema
            level, which is all of them but one. A named exception rather than a
            widened schema is the difference between "the publisher writes the
            three tables the marts came from" and "the publisher can see the
            demo accounts", and only the second of those is a hole.
        why: Why the exception exists. Empty where there is none, and required
            where there is: a table-level grant with no argument beside it is a
            boundary somebody moved and nobody has to defend.
    """

    read: bool
    write: bool
    tables: tuple[str, ...] = ()
    why: str = ""


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


@dataclass(frozen=True, slots=True)
class ResourceMonitor:
    """One warehouse's daily credit ceiling, and what happens at each threshold.

    :data:`WAREHOUSES` bounds what one query costs. This bounds what a day of
    them costs, which is the only thing that stops a runaway from spending the
    trial before the trial ends. `snowflake/sql/05_resource_monitors.sql` is
    these two records spelled as SQL.

    Attributes:
        name: The resource monitor.
        warehouse: The warehouse it is assigned to. One monitor per warehouse
            rather than one shared between them, so the publish lane cannot
            spend the serving lane's quota and suspend a conversation for a
            batch job's mistake.
        daily_credit_quota: ``CREDIT_QUOTA``, reset ``DAILY``. Read off the
            trial's own arithmetic -- roughly 130 credits over 30 days is about
            4.4 a day -- and off what the workload can plausibly cost. Not off
            the remaining balance, which changes daily and which no checked-in
            file can know: that number is :data:`TRIAL_MONITOR`'s, and it
            belongs to the operator.
        notify_at_percent: Thresholds that send email and nothing else.
        suspend_at_percent: Where the warehouse is suspended, letting running
            statements finish.
        suspend_immediate_at_percent: Where they are killed instead.
    """

    name: str
    warehouse: str
    daily_credit_quota: int
    notify_at_percent: tuple[int, ...]
    suspend_at_percent: int
    suspend_immediate_at_percent: int


MONITORS: Final[tuple[ResourceMonitor, ...]] = (
    ResourceMonitor(
        "CHIP_CHAT_SERVING_MONITOR",
        warehouse=SERVING_WAREHOUSE,
        daily_credit_quota=4,
        notify_at_percent=(50, 80, 100),
        suspend_at_percent=300,
        suspend_immediate_at_percent=400,
    ),
    ResourceMonitor(
        "CHIP_CHAT_PUBLISH_MONITOR",
        warehouse=PUBLISH_WAREHOUSE,
        daily_credit_quota=2,
        notify_at_percent=(80,),
        suspend_at_percent=100,
        suspend_immediate_at_percent=120,
    ),
)
"""One monitor per warehouse, and the asymmetry between them is the design.

A suspended publish costs a stale mart until tomorrow, so the publish warehouse
is suspended *at* its quota. A suspended serving warehouse costs the demo,
mid-conversation, in front of whoever was being shown it -- so it is suspended
only at three times its quota, twelve credits in a day, which no demo reaches on
a warehouse where every statement times out after sixty seconds. Between the two
the serving monitor only notifies, which is the honest action for a number a
genuinely busy day can reach.
"""

TRIAL_MONITOR: Final = "CHIP_CHAT_TRIAL_MONITOR"
"""The account-wide cap, and the only object here an apply does not create.

:data:`MONITORS` bounds a day off the shape of the workload. This bounds the
trial off the remaining balance, which is a number that comes from the bill --
too low suspends the demo mid-conversation, too high does nothing at all while
looking handled. So it lives in `snowflake/sql/optional/trial_credit_cap.sql`
with the network policy, gets applied by ``make snowflake-cap QUOTA=<credits>``,
and its absence is a named failure in ``chip_chat.snowflake.verify`` rather than
a quiet gap. It is also the only monitor that counts ``COMPUTE_WH``, which
`snowflake/sql/` does not manage and a Snowsight worksheet wakes.
"""

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
        "ACCOUNTS": Access(
            read=False,
            write=False,
            tables=PUBLISHED_ACCOUNT_TABLES,
            why=(
                "#39 publishes the synthetic account tables on the same "
                "schedule as the marts, and these three are the ones the marts "
                "are computed from. Granted table by table so that the "
                "publisher still cannot see demo_visitors, where every "
                "visitor-editable column lives -- which is the containment "
                "RFC-001 §04 rests its answer to PRD Q2 on, and which a "
                "schema-level grant would have thrown away to move three tables"
            ),
        ),
        "MARTS": Access(read=True, write=True),
    },
}
"""The whole security boundary as a table. `snowflake/sql/03_grants.sql` is this
table spelled as privileges, and `test_account_layout.py` checks that it still
is -- in particular that no ``GRANT`` in that file gives a mutating privilege to
a role this table says may not write.

Read the ACCOUNTS row of CHIP_CHAT_PUBLISH as the exception it is. The schema is
closed to the publisher and three tables in it are open, by name. Everything
about that arrangement is in :attr:`Access.tables` and its ``why``.
"""

STAGING_ACCESS: Final[dict[str, Access]] = {
    "CHIP_CHAT_READ": Access(read=False, write=False),
    "CHIP_CHAT_WRITE": Access(read=False, write=False),
    "CHIP_CHAT_PUBLISH": Access(read=True, write=True),
}
"""Who may reach :data:`STAGING_SCHEMA`. One role, and the other two by name.

Spelled as a full row rather than as a single grant, because the value of this
declaration is the two ``False`` entries: an incoming generation is a complete
unscoped copy of a published table, and the whole reason the schema exists is
that neither of the lanes a conversation runs on can read one.
"""


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


def access(role: str, schema_name: SchemaName) -> Access:
    """Return what ``role`` may do to ``schema_name``, lanes and staging alike.

    :data:`GRANTS` answers for the three serving schemas and
    :data:`STAGING_ACCESS` for the fourth. This is the one lookup that covers
    both, so a check over every schema does not have to know which table it is
    reading from.

    Args:
        role: One of :data:`LANE_ROLES`.
        schema_name: One of :data:`ALL_SCHEMAS`.

    Returns:
        The access.

    Raises:
        KeyError: If ``role`` is not a lane role, or ``schema_name`` is not a
            schema this account has.
    """
    if schema_name == STAGING_SCHEMA:
        return STAGING_ACCESS[role]
    return GRANTS[role][schema_name]


def may_write_table(role: str, schema_name: SchemaName, name: str) -> bool:
    """Whether ``role`` may change the rows of one named table.

    Schema-level access answers for almost everything; this is what answers when
    a schema is closed to a role and a table in it is not. See
    :attr:`Access.tables`.

    Args:
        role: One of :data:`LANE_ROLES`.
        schema_name: One of :data:`ALL_SCHEMAS`.
        name: An unqualified table name, in any case.

    Returns:
        True if the role holds mutating privileges on that table, whether it got
        them from the schema or by name.

    Raises:
        KeyError: If ``role`` or ``schema_name`` is unknown.
    """
    granted = access(role, schema_name)
    if granted.write:
        return True
    return name.upper() in {table_name.upper() for table_name in granted.tables}


def may_write(role: str, schema_name: SchemaName) -> bool:
    """Whether ``role`` may change rows in ``schema_name``.

    Args:
        role: One of :data:`LANE_ROLES`.
        schema_name: One of :data:`SCHEMAS`.

    Returns:
        True if the role holds mutating privileges on the schema AT LARGE. False
        for a role that may write named tables in it and nothing else --
        :func:`may_write_table` is the question to ask about one of those.

    Raises:
        KeyError: If ``role`` is not a lane role. :data:`ADMIN_ROLE` is not one,
            and asking about it is a question about ownership rather than about
            grants.
    """
    return access(role, schema_name).write


def readable_by(role: str) -> Iterator[SchemaName]:
    """Yield the schemas ``role`` may ``SELECT`` from, in :data:`ALL_SCHEMAS` order.

    At large. A schema the role reaches only through a named table is not one it
    may ``SELECT`` from, and saying otherwise here would make the publisher look
    like it can read the demo accounts.
    """
    for name in ALL_SCHEMAS:
        if access(role, name).read:
            yield name
