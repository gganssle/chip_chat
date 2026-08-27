"""The account and personalization lanes: four tools, and four ways to decline.

RFC-001 §06 backs four of the six read tools with Snowflake, and this module is
those four as the agent's tool layer calls them:

============================ ==================================================
Tool                         What runs here
============================ ==================================================
``ask_account_question``     Cortex Analyst writes SQL, ``analyst.decide``
                             judges it, and the admitted statement executes on
                             the bound connection. One ``db.cortex_analyst``
                             span holds all three.
``get_points_balance``       One sum over the ledger and the published reward
                             catalogue beside it. No child span: the tool span
                             is the whole of it.
``get_usual_order``          The habit mart, with its confidence and its
                             ``derived_at``.
``get_recommendations``      The ranked mart, with the rationale each row was
                             scored with.
============================ ==================================================

**No method here takes a visitor identifier, and there is nowhere to put one.**
Every one takes a ``session_id`` and hands it to
:data:`~chip_chat.snowflake.reads.SessionCheckout`, which is #44's pool:
identity is resolved from the app's server-side session store and bound to the
connection before the lane sees it. RFC-001 §05's trusted path, written as four
signatures.

**Nothing here raises into a turn.** RFC-001 §10 gives each lane a blast radius
of one row -- *Cortex Analyst timeout or low confidence → "I can't answer that
reliably"; other lanes unaffected* -- so every method returns a result carrying
its own :attr:`~AccountAnswer.declined`, marks the span failed where there is
one, and lets the conversation continue. The same division
:class:`chip_chat.search.lane.KnowledgeLane` draws, and for the same reason.

**And never a hand-written fallback query.** PRD A4 and RFC-001 §10 both say it:
a question Cortex Analyst would not answer is a question this lane does not
answer either. There is no second query in this file to fall back to, which is
the only form of that promise a reviewer can check in one sitting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from chip_chat.otel import cortex_analyst_query
from chip_chat.snowflake import analyst, reads
from chip_chat.snowflake.analyst import REFUSAL, Decision, Path, Thresholds
from chip_chat.snowflake.cortex import AnalystError, AnalystTransport
from chip_chat.snowflake.reads import (
    Connection,
    PointsBalance,
    ReadError,
    Recommendations,
    SessionCheckout,
    UsualOrder,
)

__all__ = [
    "ACCOUNT_UNAVAILABLE",
    "MAX_ROWS",
    "PERSONALIZATION_UNAVAILABLE",
    "AccountAnswer",
    "AccountLane",
    "PersonalizationLane",
    "PointsResult",
    "RecommendationsResult",
    "UsualOrderResult",
]

ACCOUNT_UNAVAILABLE: Final = (
    "I can't get to this visitor's account right now, so I can't answer "
    "anything about their orders, spend or points on this turn."
)
"""What the model is told when the *connection* is the problem.

Distinct from :data:`~chip_chat.snowflake.analyst.REFUSAL`, which is what a
visitor is told when the lane reached Snowflake and would not stand behind the
answer. Same outcome for the visitor and two different findings for an operator:
one is an outage, the other is the system working as PRD A4 requires.
"""

PERSONALIZATION_UNAVAILABLE: Final = (
    "I can't reach the personalization marts right now, so I don't have this "
    "visitor's usual order or their suggestions on this turn."
)
"""What the model is told when a mart read fails. A sentence it can say."""

MAX_ROWS: Final = 50
"""Rows returned to the model from a generated statement.

An aggregate answers in one row and a listing answers in a handful; fifty is the
point past which the model is being handed a table to summarise rather than an
answer to speak, and a tool result that large is prompt tokens spent on
something no reply will contain. The count on the span is the *real* count, so
the truncation is visible rather than mistaken for the answer.
"""


@dataclass(frozen=True, slots=True)
class AccountAnswer:
    """What ``ask_account_question`` made of one question.

    Attributes:
        question: What was asked, unchanged.
        answered: Whether there are rows. ``False`` means the visitor is told
            :data:`~chip_chat.snowflake.analyst.REFUSAL` and nothing was run.
        rows: The result, as a list of column-keyed mappings, at most
            :data:`MAX_ROWS`.
        row_count: How many rows the statement actually produced, before the
            truncation above.
        sql: The statement that ran, or empty on a refusal.
        path: Which route produced it. See
            :class:`~chip_chat.snowflake.analyst.Path`.
        confidence: What that path is worth.
        verified_query: The verified query that matched, or empty.
        interpretation: Analyst's restatement of the question, where it gave one.
        suggestions: Questions Analyst said it could answer instead.
        declined: The reason it could not answer, or ``None``. This is for the
            span and for whoever reads it later; the visitor gets
            :attr:`message`.
    """

    question: str
    answered: bool = False
    rows: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    row_count: int = 0
    sql: str = ""
    path: Path = Path.UNAVAILABLE
    confidence: float = 0.0
    verified_query: str = ""
    interpretation: str = ""
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    declined: str | None = None
    message: str = ""

    def as_tool_result(self) -> dict[str, Any]:
        """Return what ``ask_account_question`` hands back to the model.

        A refusal is a *result* and not an exception, so the model can say the
        sentence and carry on. The generated SQL travels with the answer because
        RFC-001 §06's return column says it does -- an answer a reviewer cannot
        see the query behind is an answer nobody can dispute.
        """
        if not self.answered:
            return {
                "declined": "ACCOUNT_LANE_DECLINED",
                "say": self.message or REFUSAL,
                "reason": self.declined or "",
                "path": self.path.value,
                "suggestions": list(self.suggestions),
                "never": (
                    "Do not answer this from memory, from another tool, or with "
                    "an estimate. Say you cannot answer it reliably."
                ),
            }
        return {
            "rows": [dict(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.row_count > len(self.rows),
            "sql": self.sql,
            "confidence": self.confidence,
            "path": self.path.value,
            "verified_query": self.verified_query,
            "interpretation": self.interpretation,
        }


@dataclass(frozen=True, slots=True)
class PointsResult:
    """What ``get_points_balance`` found, or why it could not look.

    Attributes:
        balance: The balance and the reward catalogue, or ``None`` on a decline.
        declined: Why the lane could not answer, or ``None``.
    """

    balance: PointsBalance | None = None
    declined: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        """Return what ``get_points_balance`` hands back to the model."""
        if self.balance is None:
            return {
                "declined": "ACCOUNT_LANE_UNAVAILABLE",
                "say": ACCOUNT_UNAVAILABLE,
                "reason": self.declined or "",
            }
        balance = self.balance
        result: dict[str, Any] = {
            "points_balance": balance.points_balance,
            "movements": balance.movements,
            "last_movement_at": balance.last_movement_at or None,
            "rewards": [reward.as_dict() for reward in balance.rewards],
            "affordable_now": [reward.name for reward in balance.affordable],
        }
        if not balance.catalogue_loaded:
            result["note"] = (
                "The published reward catalogue is not loaded on this account, "
                "so the balance is real and the list of what it affords is "
                "empty rather than short. Say the balance and say you cannot "
                "list rewards right now; do not name one from memory."
            )
        return result


@dataclass(frozen=True, slots=True)
class UsualOrderResult:
    """What ``get_usual_order`` found, or why it could not look."""

    usual: UsualOrder | None = None
    declined: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        """Return what ``get_usual_order`` hands back to the model.

        The confidence is passed through and never rounded up, and the note
        beside it says in words what the number means -- because the tool
        description promises the confidence is real and sometimes low, and a
        bare ``0.21`` reaches a model as a number rather than as a hedge.
        """
        if self.usual is None:
            return {
                "declined": "PERSONALIZATION_LANE_UNAVAILABLE",
                "say": PERSONALIZATION_UNAVAILABLE,
                "reason": self.declined or "",
            }
        usual = self.usual
        if not usual.has_a_usual:
            return {
                "has_a_usual": False,
                "items": [],
                "confidence": None,
                "mart": usual.mart.as_dict(),
                "say": (
                    "This visitor has no usual order -- the mart found no habit "
                    "in their history. Say so plainly and offer to suggest "
                    "something instead; do not present their most recent order "
                    "as a habit."
                ),
            }
        return {
            "has_a_usual": True,
            "items": [line.as_dict() for line in usual.lines],
            "confidence": usual.confidence,
            "confidence_note": _confidence_note(usual.confidence),
            "mart": usual.mart.as_dict(),
            "next_step": (
                "Call propose_order with these item_ids, adjusted for anything "
                "the visitor asked to change, and show them the card."
            ),
        }


@dataclass(frozen=True, slots=True)
class RecommendationsResult:
    """What ``get_recommendations`` found, or why it could not look."""

    recommendations: Recommendations | None = None
    declined: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        """Return what ``get_recommendations`` hands back to the model."""
        if self.recommendations is None:
            return {
                "declined": "PERSONALIZATION_LANE_UNAVAILABLE",
                "say": PERSONALIZATION_UNAVAILABLE,
                "reason": self.declined or "",
            }
        found = self.recommendations
        if not found.items:
            return {
                "items": [],
                "mart": found.mart.as_dict(),
                "say": (
                    "There is nothing the recommender will stand behind for "
                    "this visitor yet. Say so and ask what they are in the mood "
                    "for; do not suggest something popular instead."
                ),
            }
        return {
            "items": [item.as_dict() for item in found.items],
            "mart": found.mart.as_dict(),
            "how_to_use_it": (
                "Each item carries the sentence it was earned by. Give one of "
                "them, in your own words but with the same reason -- the reason "
                "is what makes this a suggestion rather than an advert."
            ),
        }


class AccountLane:
    """The account lane: Cortex Analyst, and the one fixed question beside it.

    Holds the transport and the pool's checkout for the life of the process --
    both are connection pools, and rebuilding either per turn is the mistake
    :func:`chip_chat.search.client.pooled_client` measures the cost of.
    """

    __slots__ = ("_analyst", "_checkout", "_max_rows", "_thresholds")

    def __init__(
        self,
        checkout: SessionCheckout,
        transport: AnalystTransport,
        *,
        thresholds: Thresholds | None = None,
        max_rows: int = MAX_ROWS,
    ) -> None:
        """Assemble the lane.

        Args:
            checkout: #44's pool checkout. Takes a session id and yields a
                connection with one visitor already bound to it.
            transport: How a question reaches Cortex Analyst.
            thresholds: The deadline and the confidence floor.
                :meth:`~chip_chat.snowflake.analyst.Thresholds.from_env` where
                omitted, read **once** here rather than per call, so a lane is
                configured at startup rather than re-reading the environment on
                the conversational path.
            max_rows: Rows handed to the model. See :data:`MAX_ROWS`.
        """
        self._checkout = checkout
        self._analyst = transport
        self._thresholds = Thresholds.from_env() if thresholds is None else thresholds
        self._max_rows = max(1, max_rows)

    def ask(self, question: str, *, session_id: str) -> AccountAnswer:
        """Answer one question about this visitor's account, or say it cannot.

        Opens ``db.cortex_analyst`` around the whole of it -- the Analyst call,
        the decision and the execution -- because the span's two attributes are
        *the SQL* and *how many rows it returned*, and those come from opposite
        ends of the sequence.

        Must be called inside ``tool.ask_account_question``; the span helpers
        refuse otherwise, which is how RFC-001 §09's tree is enforced rather
        than described.

        Args:
            question: The visitor's words, unchanged.
            session_id: The bound conversation. Never a ``demo_id``, and never
                read out of anything the model produced.

        Returns:
            The answer, or a refusal carrying the reason. Does not raise.
        """
        with cortex_analyst_query(question=question) as span:
            try:
                response, elapsed = self._analyst.ask(question)
            except AnalystError as error:
                # The transport is written not to raise, so this is a
                # `TokenSource` that could not mint. Same outcome, and worth
                # keeping distinguishable in the reason.
                decision = Decision(
                    answered=False,
                    path=Path.UNAVAILABLE,
                    confidence=0.0,
                    reason=str(error),
                )
            else:
                decision = analyst.decide(
                    response, elapsed_seconds=elapsed, thresholds=self._thresholds
                )
            if not decision.answered:
                span.record_declined(decision.reason or "Cortex Analyst did not answer")
                return _refused(question, decision, REFUSAL)
            try:
                rows, columns = self._execute(decision.sql, session_id=session_id)
            except ReadError as error:
                # The statement was admitted and then would not run. Not a
                # refusal by PRD A4's ladder -- the lane reached Snowflake and
                # Snowflake said no -- so the reason names the statement's
                # failure rather than the model's confidence.
                span.set_metadata(sql=decision.sql, executed=False)
                span.record_declined(f"the admitted statement did not execute: {error}")
                return _refused(question, decision, ACCOUNT_UNAVAILABLE, str(error))
            span.record_query(sql=decision.sql, row_count=len(rows))
            span.set_metadata(
                path=decision.path.value,
                confidence=decision.confidence,
                verified_query=decision.verified_query,
                elapsed_seconds=round(elapsed, 3),
                truncated=len(rows) > self._max_rows,
            )
            return AccountAnswer(
                question=question,
                answered=True,
                rows=tuple(_as_mappings(rows[: self._max_rows], columns)),
                row_count=len(rows),
                sql=decision.sql,
                path=decision.path,
                confidence=decision.confidence,
                verified_query=decision.verified_query,
                interpretation=decision.interpretation,
                suggestions=decision.suggestions,
            )

    def points_balance(self, *, session_id: str) -> PointsResult:
        """Read this visitor's balance and what it affords.

        No child span. RFC-001 §09 gives ``db.cortex_analyst`` to the generated
        query and nothing to a fixed one, so the ``tool.get_points_balance``
        span the agent already opened is the whole of this call in the trace.

        Args:
            session_id: The bound conversation.

        Returns:
            The balance, or a decline. Does not raise.
        """
        try:
            with self._checkout(session_id) as connection:
                return PointsResult(balance=reads.points_balance(connection))
        except ReadError as error:
            return PointsResult(declined=str(error))
        except Exception as error:
            return PointsResult(declined=_checkout_failed(error))

    def _execute(
        self, sql: str, *, session_id: str
    ) -> tuple[Sequence[Sequence[object]], tuple[str, ...]]:
        """Run an admitted statement on the visitor's own connection.

        The statement carries no predicate on the visitor -- Cortex Analyst is
        instructed not to write one and
        :func:`~chip_chat.snowflake.analyst.reads_only_the_view` refuses one that
        does -- so this connection being bound is the entire reason the answer is
        about the right person.
        """
        try:
            with self._checkout(session_id) as connection:
                rows = _query(connection, sql)
        except ReadError:
            raise
        except Exception as error:
            raise ReadError(_checkout_failed(error)) from error
        return rows, _column_names(sql, rows)


class PersonalizationLane:
    """The personalization lane: two mart reads, both of which may be empty.

    Empty is a real answer here and is not the same as an outage. The Explorer
    persona has no usual order and a visitor with one settled order has nothing
    the recommender will stand behind; both come back as results saying so, so
    that the model tells the visitor rather than declining a lane that worked.
    """

    __slots__ = ("_checkout", "_limit", "_threshold")

    def __init__(
        self,
        checkout: SessionCheckout,
        *,
        limit: int = reads.MAX_RECOMMENDATIONS,
        stale_after_hours: float | None = None,
    ) -> None:
        """Assemble the lane.

        Args:
            checkout: #44's pool checkout.
            limit: How many recommendations to return.
            stale_after_hours: The staleness threshold.
                :func:`~chip_chat.snowflake.reads.stale_after_hours` where
                omitted, read once at startup for the reason
                :class:`AccountLane` reads its thresholds once.
        """
        self._checkout = checkout
        self._limit = max(1, limit)
        self._threshold = (
            reads.stale_after_hours() if stale_after_hours is None else stale_after_hours
        )

    def usual_order(
        self, *, session_id: str, now: datetime | None = None
    ) -> UsualOrderResult:
        """Read the habit mart. No child span; see :meth:`AccountLane.points_balance`.

        Args:
            session_id: The bound conversation.
            now: The moment staleness is measured against.

        Returns:
            The habit, or a decline. Does not raise.
        """
        try:
            with self._checkout(session_id) as connection:
                return UsualOrderResult(
                    usual=reads.usual_order(
                        connection, now=now, threshold=self._threshold
                    )
                )
        except ReadError as error:
            return UsualOrderResult(declined=str(error))
        except Exception as error:
            return UsualOrderResult(declined=_checkout_failed(error))

    def recommendations(
        self, *, session_id: str, now: datetime | None = None
    ) -> RecommendationsResult:
        """Read the ranked mart. No child span; see :meth:`AccountLane.points_balance`.

        Args:
            session_id: The bound conversation.
            now: The moment staleness is measured against.

        Returns:
            The suggestions, or a decline. Does not raise.
        """
        try:
            with self._checkout(session_id) as connection:
                return RecommendationsResult(
                    recommendations=reads.recommendations(
                        connection, limit=self._limit, now=now, threshold=self._threshold
                    )
                )
        except ReadError as error:
            return RecommendationsResult(declined=str(error))
        except Exception as error:
            return RecommendationsResult(declined=_checkout_failed(error))


def _refused(
    question: str, decision: Decision, message: str, detail: str = ""
) -> AccountAnswer:
    """Build a refusal. Never carries SQL, whatever the decision held.

    RFC-001 §10 forbids reaching for a query the lane declined, and the surest
    way not to reach for it is not to hand it up.
    """
    return AccountAnswer(
        question=question,
        answered=False,
        path=decision.path,
        confidence=decision.confidence,
        verified_query=decision.verified_query,
        interpretation=decision.interpretation,
        suggestions=decision.suggestions,
        declined=detail or decision.reason,
        message=message,
    )


def _confidence_note(confidence: float | None) -> str:
    """Say in words what the mart's number means, for a model that reads numbers.

    Three bands and no arithmetic beyond a comparison. The tool description
    promises the confidence is real and sometimes low; this is what makes the
    promise reach the reply rather than stopping at a float in a JSON blob.
    """
    if confidence is None:
        return (
            "The mart recorded no confidence for this habit. Describe it as "
            "what they have ordered before, not as their usual."
        )
    if confidence < 0.4:
        return (
            "Low confidence. Offer this as a guess and ask whether it is right "
            "-- do not call it their usual."
        )
    if confidence < 0.7:
        return "Moderate confidence. Offer it as their usual but invite a correction."
    return "High confidence. Safe to call this their usual."


def _checkout_failed(error: Exception) -> str:
    """Describe a checkout that did not produce a connection.

    Broad on purpose. The pool raises four of its own types and the driver
    underneath it raises anything at all, and this module may not import the
    pool to name them -- so the boundary is the catch, and the type name is
    carried into the reason so a trace still says which one it was.
    """
    return f"the pool did not produce a bound connection: {type(error).__name__}: {error}"


def _query(connection: Connection, sql: str) -> Sequence[Sequence[object]]:
    """Run one statement, turning anything it raises into :class:`ReadError`."""
    try:
        return connection.execute(sql)
    except Exception as error:
        raise ReadError(f"{type(error).__name__}: {error}") from error


def _column_names(sql: str, rows: Sequence[Sequence[object]]) -> tuple[str, ...]:
    """Return names for the columns of a generated statement.

    The :class:`~chip_chat.snowflake.reads.Connection` protocol returns rows and
    not a cursor description, deliberately: it is the narrowest thing that
    serves every read in this package, and widening it for one caller would
    widen what #44's pool has to hand out.

    So the names are read off the statement, and only where the statement says
    them outright. Cortex Analyst aliases every projected column -- ``SUM(...)
    AS total_spend`` -- so the common case is answered exactly. Anything less
    than *every* top-level item carrying an explicit ``AS`` falls back to
    ``column_1``, ``column_2``, for the whole result rather than for the items
    that were unclear: a half-named row is one a model reads as fully named.

    The SQL travels with the answer either way (RFC-001 §06's return column), so
    a positional name is a legible answer rather than a lost one -- and a name
    this function guessed at would be one nobody could correct.

    Args:
        sql: The admitted statement.
        rows: What it returned, for the width.

    Returns:
        One name per column.
    """
    width = max((len(row) for row in rows), default=0)
    aliases = _aliases(sql)
    if len(aliases) == width and width:
        return aliases
    return tuple(f"column_{index + 1}" for index in range(width))


_SELECT = re.compile(
    r"\bSELECT\b(?!.*\bSELECT\b)(.*?)\bFROM\b", re.IGNORECASE | re.DOTALL
)
_ALIAS = re.compile(r"\bAS\s+([A-Za-z_][\w$]*)\s*\Z", re.IGNORECASE)


def _aliases(sql: str) -> tuple[str, ...]:
    """Return the explicit ``AS`` alias of every top-level projected column.

    Empty unless *every* item has one, which is what makes the fallback in
    :func:`_column_names` all-or-nothing. Splitting is done at bracket depth
    zero so that a comma inside ``COALESCE(a, b)`` is not a column boundary.
    """
    match = _SELECT.search(" ".join(sql.split()))
    if match is None:
        return ()
    names: list[str] = []
    for item in _split_at_depth_zero(match.group(1)):
        alias = _ALIAS.search(item.strip())
        if alias is None:
            return ()
        names.append(alias.group(1).strip('"'))
    return tuple(names)


def _split_at_depth_zero(projection: str) -> list[str]:
    """Split a select list on the commas that separate columns and no others."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for character in projection:
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        if character == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(character)
    items.append("".join(current))
    return [item for item in items if item.strip()]


def _as_mappings(
    rows: Sequence[Sequence[object]], columns: Sequence[str]
) -> list[Mapping[str, Any]]:
    """Turn rows into JSON-ready mappings, one key per column."""
    return [
        {column: _jsonable(value) for column, value in zip(columns, row, strict=False)}
        for row in rows
    ]


def _jsonable(value: object) -> Any:
    """Return ``value`` in a form the tool result can carry.

    Anything the encoder would refuse becomes its string form rather than
    failing the turn: a ``Decimal`` total and a ``date`` are both answers, and a
    lane that declined because a driver returned a type nobody anticipated would
    be declining for a reason no visitor can see.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value
