"""The visitor-scoped reads, written as though the visitor were the only row.

Three of the six read tools of RFC-001 §06 are a query and nothing else --
``get_points_balance``, ``get_usual_order``, ``get_recommendations`` -- and this
module is those queries, their result types, and the one rule that governs how
they are written.

**Not one of them carries a predicate on**
:data:`~chip_chat.snowflake.schema.DEMO_ID`.
That is not an omission and it is not a convenience. ``sql/11_semantic_view.sql``
already states the reason for the account lane and it is the same reason here::

    every query below is written as though the visitor were the only person in
    the database. `SELECT SUM(delta) FROM loyalty_ledger` with no predicate is
    this visitor's balance, because the session cannot see another visitor's
    rows.

#43's row access policies are what make that true, and #44's pool is what makes
the policies true. Between them there is nothing left for a query to assert, so
a query that asserted it would need a visitor identifier to put in the
predicate -- and RFC-001 §06's whole design is that no signature has one. A
``WHERE demo_id = ...`` added here would be a second opinion about identity
sitting *underneath* the one that is enforced, which is the shape every
cross-tenant bug has.

**Identity reaches a query the way it reaches everything else: through the
checkout.** :data:`SessionCheckout` is
:meth:`~chip_chat.api.pool.VisitorPool.for_session` -- a session id in, a
connection with one visitor already bound out. It takes a
*session* id rather than a ``demo_id`` because resolving one to the other is the
app's job and not this module's, and because a function that accepted a
``demo_id`` would be a function somebody could call with the wrong one.

**Staleness is reported, never hidden.** RFC-001 §10 requires a stale mart to be
served *with* its ``derived_at`` rather than silently as fresh, so both mart
reads return :attr:`Mart.derived_at` on every path and :attr:`Mart.stale` beside
it. The threshold is :data:`STALE_AFTER_HOURS`, and it is a property of the
publish schedule rather than of the data -- see that constant.

**A declining lane is not a failing turn.** Every function here raises
:class:`ReadError` and nothing else. :mod:`chip_chat.snowflake.lane` is what
turns that into a result the model can read, because RFC-001 §10 gives a lane a
blast radius of one row and an exception escaping into the turn is a blast
radius of the conversation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from chip_chat.snowflake import schema

__all__ = [
    "DEFAULT_STALE_AFTER_HOURS",
    "MAX_RECOMMENDATIONS",
    "STALE_AFTER_HOURS_VARIABLE",
    "Connection",
    "Mart",
    "PointsBalance",
    "ReadError",
    "Recommendation",
    "Recommendations",
    "Reward",
    "SessionCheckout",
    "UsualOrder",
    "UsualOrderLine",
    "points_balance",
    "recommendations",
    "stale_after_hours",
    "usual_order",
]


class Connection(Protocol):
    """The slice of a bound connection a read needs, and no more.

    Structurally what :class:`~chip_chat.api.pool.VisitorConnection` already is.
    A protocol rather than that class because the pool lives in ``api/`` and
    this package may not import it -- and because the narrower the surface, the
    less there is here that could be handed to something that would keep it.
    """

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run one statement and return its rows, each a sequence of columns."""
        ...


SessionCheckout = Callable[[str], AbstractContextManager[Connection]]
"""Check out a connection already bound to whoever holds this session.

:meth:`chip_chat.api.pool.VisitorPool.for_session` satisfies this exactly, which
is the point: the lane holds the pool's checkout and never a visitor identifier,
so there is no variable in the agent's process that a confused model could
influence and no signature it could be passed through.
"""


class ReadError(RuntimeError):
    """A visitor-scoped read could not be completed.

    Raised for a connection that would not check out, a statement the warehouse
    refused, and a row shaped in a way this module will not guess about. Caught
    by :mod:`chip_chat.snowflake.lane`, which is where it becomes the decline
    the model is handed.
    """


DEFAULT_STALE_AFTER_HOURS: Final = 36.0
"""How old a mart row may be before the tools say so.

Thirty-six hours, from the publish schedule rather than from the data: the
nightly publish runs at 07:00 UTC (``infra/terraform/variables.tf``,
``databricks_publish_cron``), so a row derived more than a day and a half ago
means at least one run did not land. Wide enough that a late run is not reported
as an outage, narrow enough that two missed nights cannot pass as fresh.

It is a *threshold on a sentence the model is told*, never a refusal: a stale
mart is served with its timestamp (RFC-001 §10) and the visitor is told when it
was computed. Refusing would replace a slightly old answer with none.
"""

STALE_AFTER_HOURS_VARIABLE: Final = "CHIP_CHAT_MART_STALE_AFTER_HOURS"
"""Environment override for :data:`DEFAULT_STALE_AFTER_HOURS`.

Configurable because the publish cron is, and a threshold that could not follow
it would be a threshold tuned against a schedule nobody is running.
"""

MAX_RECOMMENDATIONS: Final = 3
"""How many suggestions come back at most.

The mart is published at up to ``recommender.TOP_K`` per visitor and the tool
takes the strongest few, because a reply that lists ten items is a reply nobody
reads out loud. Ranked, so the truncation is of the tail rather than arbitrary.
"""

_POINTS_TABLE: Final = schema.table("loyalty_ledger").qualified()
_REWARDS_TABLE: Final = schema.table("rewards").qualified()
_USUAL_ORDER_MART: Final = schema.table("usual_order").qualified()
_MENU_ITEMS: Final = schema.table("menu_items").qualified()

RECOMMENDATIONS_MART: Final = "CHIP_CHAT.MARTS.recommendations"
"""Where ``get_recommendations`` reads from, and the one table here that
:data:`chip_chat.snowflake.schema.TABLES` does not declare.

RFC-001 §04 prints ``recommendations`` in the data model, #37 batch-scores it
into ``gold_synthetic.recommendations``, and nothing publishes it into
``CHIP_CHAT.MARTS`` -- because §04 also fixes four serving marts and this would
be a fifth. Bead ``cc-afo5`` is that decision and it is deliberately not taken
here: a tool ticket that quietly added a serving table would be making a schema
decision in a file nobody reviewing the schema reads.

So the name is spelled here, once, next to the argument for why it is spelled
here rather than declared with the rest. Until the table exists the read raises
:class:`ReadError` and the lane declines, which is the honest state and is
visible in a trace as one.
"""

_POINTS_BALANCE_SQL: Final = f"""\
SELECT COALESCE(SUM(delta), 0) AS points_balance,
       COUNT(*)                AS movements,
       MAX(created_at)         AS last_movement_at
  FROM {_POINTS_TABLE}
"""
"""This visitor's balance: every movement of the ledger, added up.

A sum and never a stored number -- ``sql/07_accounts.sql`` says so on the column
itself, and ``redeem_points`` re-reads it the same way. No predicate, for the
reason in the module docstring.
"""

_AFFORDABLE_REWARDS_SQL: Final = f"""\
SELECT reward_id, name, point_cost, source_url, harvested_at
  FROM {_REWARDS_TABLE}
 ORDER BY point_cost ASC, position ASC
"""
"""The published reward catalogue, cheapest first.

Not visitor-scoped -- it is what the restaurant publishes -- so it carries no
policy and this read is the same for everybody. Which of them the balance
affords is arithmetic done here rather than a predicate, because a visitor who
is two hundred points short is owed *how far off they are* and a ``WHERE
point_cost <= :balance`` would return silence instead.
"""

_USUAL_ORDER_SQL: Final = f"""\
SELECT u.item_id,
       u.modifiers,
       u.confidence,
       u.derived_at,
       m.name
  FROM {_USUAL_ORDER_MART} AS u
  LEFT JOIN {_MENU_ITEMS} AS m ON m.item_id = u.item_id
"""
"""The habit mart, with the catalogue joined on for the name.

A ``LEFT`` join: an item that has left the published menu since the mart was
computed still has to come back, because the honest answer is *you usually order
this and it is not on the menu any more* rather than an empty result that reads
as *you have no usual*.
"""

_RECOMMENDATIONS_SQL: Final = f"""\
SELECT r.rank,
       r.item_id,
       r.rationale,
       r.score,
       r.model_version,
       r.derived_at,
       m.name
  FROM {RECOMMENDATIONS_MART} AS r
  LEFT JOIN {_MENU_ITEMS} AS m ON m.item_id = r.item_id
 ORDER BY r.rank ASC
"""
"""The ranked suggestions and the sentence each was earned by.

``rationale`` is a column rather than something composed here: #37 renders it
from the seed item's published name and the visitor's own order share, and a
sentence re-written at serving time would be a second, unversioned opinion about
what the model found.
"""


def stale_after_hours(env: dict[str, str] | None = None) -> float:
    """Return the staleness threshold, from the environment or the default.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        Hours after which a mart row is reported as stale.

    Raises:
        ValueError: If the variable is set and does not parse as a positive
            number. Failing loudly, because a misspelled threshold that quietly
            kept the default is a lane tuned against a number nobody set.
    """
    source = os.environ if env is None else env
    raw = source.get(STALE_AFTER_HOURS_VARIABLE, "").strip()
    if not raw:
        return DEFAULT_STALE_AFTER_HOURS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{STALE_AFTER_HOURS_VARIABLE}={raw!r} is not a number"
        ) from None
    if value <= 0:
        raise ValueError(
            f"{STALE_AFTER_HOURS_VARIABLE}={raw!r} would report every mart as stale"
        )
    return value


@dataclass(frozen=True, slots=True)
class Mart:
    """When a mart row was computed, and whether that is long enough ago to say.

    Attributes:
        derived_at: The gold pipeline's own timestamp for the row, ISO-8601, or
            the empty string where the row carried none. Empty is *worse* than
            old: a mart row that cannot date itself cannot be served as fresh
            either, so it is reported stale.
        stale: Whether :attr:`derived_at` is further back than
            :func:`stale_after_hours`.
        age_hours: How old the row is, or ``None`` where it does not say.
    """

    derived_at: str = ""
    stale: bool = True
    age_hours: float | None = None

    @classmethod
    def of(
        cls, value: object, *, now: datetime | None = None, threshold: float | None = None
    ) -> Mart:
        """Judge one ``derived_at`` value.

        Args:
            value: What the driver returned for the column.
            now: The moment to measure against. Injected so a test is not a
                race against the clock.
            threshold: Hours; :func:`stale_after_hours` where omitted.

        Returns:
            The judgement. Never raises -- an unreadable timestamp is reported
            as an undated row rather than as an outage, because the rows
            themselves arrived.
        """
        moment = _timestamp(value)
        if moment is None:
            return cls()
        hours = stale_after_hours() if threshold is None else threshold
        reference = (now or datetime.now(UTC)).astimezone(UTC)
        age = (reference - moment).total_seconds() / 3600.0
        return cls(
            derived_at=moment.isoformat(),
            stale=age > hours,
            age_hours=round(age, 2),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the three fields as the model is shown them."""
        return {
            "derived_at": self.derived_at or None,
            "stale": self.stale,
            "age_hours": self.age_hours,
        }


@dataclass(frozen=True, slots=True)
class Reward:
    """One published reward, and whether this balance reaches it.

    Attributes:
        reward_id: The invented-and-labelled slug of ``sql/06_catalogue.sql``.
        name: The reward as published, verbatim.
        point_cost: What it costs, as published.
        affordable: Whether the balance covers it right now.
        points_short: How many points are missing, or ``0`` when it is
            affordable. The number a visitor is actually owed.
        source_url: The page it was read from.
        harvested_at: When that page was fetched.
    """

    reward_id: str
    name: str
    point_cost: int
    affordable: bool
    points_short: int
    source_url: str = ""
    harvested_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the reward as the model is shown it."""
        return {
            "reward_id": self.reward_id,
            "name": self.name,
            "point_cost": self.point_cost,
            "affordable": self.affordable,
            "points_short": self.points_short,
            "source_url": self.source_url,
            "harvested_at": self.harvested_at,
        }


@dataclass(frozen=True, slots=True)
class PointsBalance:
    """A balance, and what it can be spent on.

    Attributes:
        points_balance: The sum of the ledger.
        movements: How many rows that sum is over. Zero means an account with no
            ledger at all, which is a different thing from a balance of zero and
            is worth the model being able to tell apart.
        last_movement_at: When the account last moved, ISO-8601, or empty.
        rewards: The published catalogue, cheapest first, each marked.
        catalogue_loaded: Whether any reward rows came back. ``False`` is the
            state ``cc-99cn`` describes -- nothing publishes ``rewards`` yet --
            and it is reported rather than rendered as an empty line-up.
    """

    points_balance: int
    movements: int = 0
    last_movement_at: str = ""
    rewards: tuple[Reward, ...] = field(default_factory=tuple)
    catalogue_loaded: bool = True

    @property
    def affordable(self) -> tuple[Reward, ...]:
        """The rewards this balance reaches, cheapest first."""
        return tuple(reward for reward in self.rewards if reward.affordable)


@dataclass(frozen=True, slots=True)
class UsualOrderLine:
    """One item of the habit, named from the catalogue.

    Attributes:
        item_id: The catalogue id, which is what ``propose_order`` takes.
        name: The published name, or empty where the item has left the menu.
        modifiers: The modifier ids the mart recorded with it.
        on_the_menu: Whether the catalogue still publishes this item.
    """

    item_id: str
    name: str = ""
    modifiers: tuple[str, ...] = field(default_factory=tuple)
    on_the_menu: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return the line as the model is shown it."""
        return {
            "item_id": self.item_id,
            "name": self.name,
            "modifiers": list(self.modifiers),
            "on_the_menu": self.on_the_menu,
        }


@dataclass(frozen=True, slots=True)
class UsualOrder:
    """What the mart says this visitor habitually orders.

    Attributes:
        lines: The items, empty where the mart holds no row for this visitor.
        confidence: The mart's own number in ``[0, 1]``, or ``None`` where the
            row carried none. Never invented: the Explorer persona genuinely has
            no usual order, and a confidence made up here to fill the field is
            exactly the guess-presented-as-a-habit the tool description warns
            against.
        mart: When the row was computed, and whether that is stale.
    """

    lines: tuple[UsualOrderLine, ...] = field(default_factory=tuple)
    confidence: float | None = None
    mart: Mart = field(default_factory=Mart)

    @property
    def has_a_usual(self) -> bool:
        """Whether the mart produced a habit at all."""
        return bool(self.lines)


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One ranked suggestion and the sentence it was earned by.

    Attributes:
        rank: 1 for the strongest.
        item_id: The catalogue id.
        name: The published name, or empty where the item has left the menu.
        rationale: #37's own sentence, rendered when the row was scored.
        score: The model's score. Comparable within a visitor and not across.
        model_version: The Unity Catalog model version that produced the row.
        on_the_menu: Whether the catalogue still publishes this item.
    """

    rank: int
    item_id: str
    name: str = ""
    rationale: str = ""
    score: float | None = None
    model_version: str = ""
    on_the_menu: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return the suggestion as the model is shown it."""
        return {
            "rank": self.rank,
            "item_id": self.item_id,
            "name": self.name,
            "rationale": self.rationale,
            "score": self.score,
            "model_version": self.model_version,
            "on_the_menu": self.on_the_menu,
        }


@dataclass(frozen=True, slots=True)
class Recommendations:
    """The ranked suggestions for one visitor.

    Attributes:
        items: Up to :data:`MAX_RECOMMENDATIONS`, strongest first.
        mart: When the rows were computed, and whether that is stale.
    """

    items: tuple[Recommendation, ...] = field(default_factory=tuple)
    mart: Mart = field(default_factory=Mart)


def points_balance(
    connection: Connection, *, at: datetime | None = None
) -> PointsBalance:
    """Read this visitor's balance and what it affords.

    Args:
        connection: A connection with one visitor already bound to it.
        at: Unused by the arithmetic and present for symmetry with the mart
            reads; the balance is a live sum and is never stale.

    Returns:
        The balance, and the published catalogue marked against it.

    Raises:
        ReadError: If either statement failed, or the balance row was shaped in
            a way this function will not guess about.
    """
    del at
    rows = _execute(connection, _POINTS_BALANCE_SQL)
    if not rows:
        # `SUM` over no rows still returns one row, so an empty result is the
        # statement not having run rather than an account with no ledger.
        raise ReadError("the points balance query returned no row at all")
    balance = _integer(rows[0], 0, "points_balance")
    movements = _integer(rows[0], 1, "movements")
    catalogue = _execute(connection, _AFFORDABLE_REWARDS_SQL)
    return PointsBalance(
        points_balance=balance,
        movements=movements,
        last_movement_at=_iso(_column(rows[0], 2)),
        rewards=tuple(_reward(row, balance) for row in catalogue),
        catalogue_loaded=bool(catalogue),
    )


def usual_order(
    connection: Connection,
    *,
    now: datetime | None = None,
    threshold: float | None = None,
) -> UsualOrder:
    """Read the habit mart for this visitor.

    Args:
        connection: A connection with one visitor already bound to it.
        now: The moment staleness is measured against.
        threshold: Staleness threshold in hours.

    Returns:
        The habit, which may legitimately be empty.

    Raises:
        ReadError: If the statement failed.
    """
    rows = _execute(connection, _USUAL_ORDER_SQL)
    if not rows:
        return UsualOrder()
    lines = tuple(
        UsualOrderLine(
            item_id=_text(_column(row, 0)),
            name=_text(_column(row, 4)),
            modifiers=_string_array(_column(row, 1)),
            on_the_menu=bool(_text(_column(row, 4))),
        )
        for row in rows
        if _text(_column(row, 0))
    )
    return UsualOrder(
        lines=lines,
        confidence=_fraction(_column(rows[0], 2)),
        mart=Mart.of(_column(rows[0], 3), now=now, threshold=threshold),
    )


def recommendations(
    connection: Connection,
    *,
    limit: int = MAX_RECOMMENDATIONS,
    now: datetime | None = None,
    threshold: float | None = None,
) -> Recommendations:
    """Read the ranked suggestions for this visitor.

    Args:
        connection: A connection with one visitor already bound to it.
        limit: How many to return. The mart is ranked, so this drops the tail.
        now: The moment staleness is measured against.
        threshold: Staleness threshold in hours.

    Returns:
        The suggestions, which may legitimately be empty -- a visitor whose
        whole history is one item has nothing the recommender would stand behind.

    Raises:
        ReadError: If the statement failed, which today includes the serving
            table not existing at all. See :data:`RECOMMENDATIONS_MART`.
    """
    rows = _execute(connection, _RECOMMENDATIONS_SQL)
    if not rows:
        return Recommendations()
    items = tuple(
        Recommendation(
            rank=_integer(row, 0, "rank"),
            item_id=_text(_column(row, 1)),
            name=_text(_column(row, 6)),
            rationale=_text(_column(row, 2)),
            score=_optional_float(_column(row, 3)),
            model_version=_text(_column(row, 4)),
            on_the_menu=bool(_text(_column(row, 6))),
        )
        for row in rows[:limit]
    )
    return Recommendations(
        items=items,
        mart=Mart.of(_column(rows[0], 5), now=now, threshold=threshold),
    )


# ---------------------------------------------------------------------------
# Reading what a driver hands back. Deliberately unforgiving about shape and
# deliberately forgiving about type: a column may arrive as Decimal, int or str
# depending on the driver, and none of those is a reason to fail a turn.
# ---------------------------------------------------------------------------


def _execute(connection: Connection, sql: str) -> Sequence[Sequence[object]]:
    """Run one statement, turning anything it raises into :class:`ReadError`."""
    try:
        return connection.execute(sql)
    except ReadError:
        raise
    except Exception as error:
        raise ReadError(f"{type(error).__name__}: {error}") from error


def _column(row: Sequence[object], index: int) -> object:
    """Return one column, or ``None`` where the row is shorter than expected."""
    return row[index] if index < len(row) else None


def _integer(row: Sequence[object], index: int, name: str) -> int:
    """Return a column as an int, or raise saying which column was wrong."""
    value = _column(row, index)
    if isinstance(value, bool):
        raise ReadError(f"{name} came back as a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(Decimal(value))
        except (InvalidOperation, ValueError):
            raise ReadError(
                f"{name} came back as {value!r}, which is not a number"
            ) from None
    if value is None:
        return 0
    raise ReadError(f"{name} came back as {type(value).__name__}")


def _optional_float(value: object) -> float | None:
    """Return a column as a float, or ``None`` where it was absent or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _fraction(value: object) -> float | None:
    """Return a confidence in ``[0, 1]``, or ``None``.

    Out-of-range is ``None`` rather than clamped: a confidence of 1.4 is a mart
    that computed something other than a confidence, and reporting it as *no
    number* is truthful where reporting it as *certain* is not.
    """
    number = _optional_float(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _text(value: object) -> str:
    """Return a column as a string, with ``None`` becoming the empty string."""
    return "" if value is None else str(value)


def _iso(value: object) -> str:
    """Return a timestamp column as ISO-8601, or the empty string."""
    moment = _timestamp(value)
    return "" if moment is None else moment.isoformat()


def _timestamp(value: object) -> datetime | None:
    """Return a column as an aware UTC datetime, or ``None`` if it is not one.

    ``TIMESTAMP_NTZ`` arrives naive and is UTC by the schema's own convention
    (``sql/07_accounts.sql`` says so on every timestamp column), so a naive
    value is stamped rather than guessed at.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _string_array(value: object) -> tuple[str, ...]:
    """Return an ``ARRAY`` column as a tuple of strings.

    A driver may hand this back as a list or as the JSON text of one. Anything
    else is dropped rather than guessed at -- a modifier list this function
    invented would reach ``propose_order`` as a real selection.
    """
    if isinstance(value, list | tuple):
        return tuple(str(entry) for entry in value if entry is not None)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return ()
        if isinstance(parsed, list):
            return tuple(str(entry) for entry in parsed if entry is not None)
    return ()


def _reward(row: Sequence[object], balance: int) -> Reward:
    """Build one :class:`Reward` from a catalogue row, marked against ``balance``."""
    cost = _integer(row, 2, "point_cost")
    return Reward(
        reward_id=_text(_column(row, 0)),
        name=_text(_column(row, 1)),
        point_cost=cost,
        affordable=balance >= cost,
        points_short=max(0, cost - balance),
        source_url=_text(_column(row, 3)),
        harvested_at=_iso(_column(row, 4)),
    )
