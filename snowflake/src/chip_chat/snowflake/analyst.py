"""When the account lane answers, and when it says it cannot. Issue #45, and PRD A4.

RFC-001 §10 gives the account lane one failure behaviour and no other: *a Cortex
Analyst timeout or low confidence returns "I can't answer that reliably" and
never a fallback hand-written query.* This module is that sentence as a
function. It makes no network call and holds no credential -- it takes a
response somebody else fetched and returns :class:`Decision`, which either
carries SQL worth executing or carries the reason it will not.

The separation is deliberate. #61's ``ask_account_question`` owns the HTTP call
and the ``db.cortex_analyst`` span; this owns the judgement, so the judgement is
testable without a trial account and identical wherever it is asked.

**Confidence here is a ladder, not a score.** Cortex Analyst returns no
probability. What it returns is which path produced the answer, and those are
worth different amounts:

    verified (1.0)     a query from :data:`~chip_chat.snowflake.semantic.VERIFIED_QUERIES`
                       matched. A person wrote this SQL and a test covers it.
    generated (0.5)    SQL written for this question against the semantic view.
                       Bounded -- it can only name declared elements -- but new.
    suggested (0.0)    no SQL at all. Analyst offered questions it could answer
                       instead, which is it telling you it could not answer this.
    unavailable (0.0)  a timeout, a transport failure, an HTTP error.

:data:`Thresholds.min_confidence` is where the floor sits, and it defaults to
``generated``: refusing everything unverified would refuse most of PRD A2, which
is aggregates and time ranges nobody can enumerate in advance. Raising it to
``1.0`` is a supported and deliberately available position -- verified queries
only -- and is what a demo in front of an audience might want.

**Three structural guards run before the floor**, and they are not about
confidence at all. They are the things a bounded model is supposed to make
impossible, checked anyway, because "impossible" here rests on a service
generating SQL:

    out of bounds   the statement names a table the semantic view does not.
    identified      the statement mentions ``demo_id``. No tool signature
                    carries a visitor identifier (RFC-001 §06) and the model is
                    told never to filter on one, so a query that does is either
                    a hallucinated literal or an attempt at somebody else's rows.
    not a read      the statement is not a single ``SELECT``/``WITH``.

Each is a refusal rather than an exception: a lane may decline, the conversation
may not fail (RFC-001 §10).

**Warnings are advisory and are not read here.** A successful response can carry
a ``warnings`` array describing an SQL error Cortex Analyst already recovered
from -- observed on this account: the model first emitted a filter naming a
physical column inside ``SEMANTIC_VIEW(...)``, the service caught the
compilation error and fell back to the verified query, and the right answer
arrived with the failed attempt attached. Treating that array as a failure
signal would decline a good answer. The decision is made on the content and the
elapsed time, and #61 records the warnings on the span for whoever is reading
traces.

**``question_category`` is Analyst's own verdict**, and both values seen here
agree with the decision below rather than needing to be read: an answerable
question comes back ``CLEAR_SQL`` with a ``sql`` part, and one this model does
not cover comes back ``REJECT`` with no ``sql`` part at all. The absence of SQL
is what is branched on, because it is the thing that cannot be wrong.

**The timeout is not measured.** :data:`DEFAULT_TIMEOUT_SECONDS` is fifteen
seconds, chosen from two numbers rather than from a distribution: the serving
warehouse kills any statement at sixty (`account.WAREHOUSES`), and a turn that
has not answered inside a minute has already failed as a conversation. What was
measured, on this account on 2026-08-27, is the Analyst call itself -- 2.3s on
the verified path and 4.1-5.0s on the generated path, round trip, from a
machine in the same country. That is the *account* lane's first hop only, with
cross-region inference in it, and it is already past the PRD's 2s median for a
whole turn. docs/snowflake-semantic-view.md §4 has the numbers and what they do
not yet include.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from chip_chat.snowflake import semantic

__all__ = [
    "CONFIDENCE",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_CONFIDENCE_VARIABLE",
    "REFUSAL",
    "TIMEOUT_VARIABLE",
    "Decision",
    "Path",
    "Thresholds",
    "decide",
    "reads_only_the_view",
]

REFUSAL: Final = "I can't answer that reliably"
"""What the visitor is told, in RFC-001 §10's own words.

The same sentence whatever declined and why -- the reason is for the
``db.cortex_analyst`` span and for whoever reads it later, not for the visitor,
who is owed an honest no rather than a diagnosis. #61 hands this back as a tool
*result* the model can speak, so the account lane declines and the conversation
carries on.
"""


class Path(StrEnum):
    """Which route produced the response, which is the whole of confidence here."""

    VERIFIED = "verified"
    """A verified query matched. ``confidence.verified_query_used`` names it."""

    GENERATED = "generated"
    """SQL written for this question, bounded by the semantic view."""

    SUGGESTED = "suggested"
    """No SQL. Analyst offered questions it could answer instead."""

    UNAVAILABLE = "unavailable"
    """Nothing usable came back: a timeout, an HTTP error, a malformed body."""


CONFIDENCE: Final[dict[Path, float]] = {
    Path.VERIFIED: 1.0,
    Path.GENERATED: 0.5,
    Path.SUGGESTED: 0.0,
    Path.UNAVAILABLE: 0.0,
}
"""What each path is worth. A ladder with four rungs and no arithmetic in it:
the numbers exist so that :attr:`Thresholds.min_confidence` can be one value
rather than a set of paths, and so that raising the floor is a change to an
environment variable rather than to a branch."""

DEFAULT_TIMEOUT_SECONDS: Final = 15.0
"""How long the account lane waits before it declines.

Under the serving warehouse's sixty-second ``STATEMENT_TIMEOUT_IN_SECONDS``,
because this bounds the Analyst call and the warehouse bounds the SQL that
follows it -- two hops, and the turn has to fit both. See the module docstring
for what was measured and what was not.
"""

DEFAULT_MIN_CONFIDENCE: Final = CONFIDENCE[Path.GENERATED]
"""Generated SQL is answered by default; nothing below it is."""

TIMEOUT_VARIABLE: Final = "CHIP_CHAT_ANALYST_TIMEOUT_SECONDS"
MIN_CONFIDENCE_VARIABLE: Final = "CHIP_CHAT_ANALYST_MIN_CONFIDENCE"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The two numbers #45's fourth acceptance criterion asks to be configurable.

    Attributes:
        timeout_seconds: How long to wait for Analyst before declining.
        min_confidence: The lowest :data:`CONFIDENCE` an answer may be returned
            at. ``1.0`` is verified queries only.
    """

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Thresholds:
        """Build the thresholds from the environment.

        Reads :data:`TIMEOUT_VARIABLE` and :data:`MIN_CONFIDENCE_VARIABLE`.
        Both are optional and both fall back to the argued defaults, so an
        unset environment is the design rather than an unthresholded lane.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configured thresholds.

        Raises:
            ValueError: If either does not parse, if the timeout is not
                positive, or if the floor is outside ``[0, 1]``. Failing at
                startup is the point: a misspelled threshold that quietly kept
                the default is a lane tuned against a number nobody set.
        """
        source = os.environ if env is None else env
        return cls(
            timeout_seconds=_positive(source, TIMEOUT_VARIABLE, DEFAULT_TIMEOUT_SECONDS),
            min_confidence=_fraction(
                source, MIN_CONFIDENCE_VARIABLE, DEFAULT_MIN_CONFIDENCE
            ),
        )

    def admits(self, path: Path) -> bool:
        """Whether an answer reached by ``path`` clears the floor."""
        return CONFIDENCE[path] >= self.min_confidence


def _positive(source: Mapping[str, str], name: str, fallback: float) -> float:
    """Read a positive number, or raise saying which variable was wrong."""
    raw = source.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if value <= 0:
        raise ValueError(f"{name}={raw!r} would decline every question before asking")
    return value


def _fraction(source: Mapping[str, str], name: str, fallback: float) -> float:
    """Read a number in [0, 1], or raise saying which variable was wrong."""
    raw = source.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not 0.0 <= value <= 1.0:
        rungs = ", ".join(f"{path.value} {score:g}" for path, score in CONFIDENCE.items())
        raise ValueError(f"{name}={raw!r} is outside [0, 1]. The rungs are: {rungs}")
    return value


@dataclass(frozen=True, slots=True)
class Decision:
    """What the account lane does with one Analyst response.

    Attributes:
        answered: Whether there is SQL to execute. False means the visitor is
            told :data:`REFUSAL` and nothing is run.
        path: Which route produced it, and therefore what it was worth.
        confidence: :data:`CONFIDENCE` for that path.
        sql: The statement, where there is one and it was admitted. Empty on a
            refusal -- including a refusal of SQL that came back, because
            RFC-001 §10 forbids reaching for a query the lane declined.
        verified_query: The name of the verified query that matched, or empty.
        reason: Why it declined, for the ``db.cortex_analyst`` span. Empty when
            it answered.
        suggestions: The questions Analyst offered instead, where it offered
            any. Worth showing; they are the model saying what it does cover.
        interpretation: Analyst's restatement of the question, where it gave
            one. Evidence for a reviewer reading a trace.
    """

    answered: bool
    path: Path
    confidence: float
    sql: str = ""
    verified_query: str = ""
    reason: str = ""
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    interpretation: str = ""


_ALLOWED_TABLES: Final[frozenset[str]] = frozenset(
    f"chip_chat.{table.schema}.{table.table}".lower() for table in semantic.LOGICAL_TABLES
)
"""The five physical tables a generated statement may name, lower-cased.

Off the verified path Cortex Analyst emits ``SELECT * FROM SEMANTIC_VIEW(...)``
and names no table at all; on it, and on the CTE form, it names these. Anything
else is a query that got out of the model.
"""

_SOURCE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$.]*)", re.IGNORECASE)
_CTE = re.compile(r"(?:\bWITH\b|,)\s*([A-Za-z_][\w$]*)\s+AS\s*\(", re.IGNORECASE)
_SEMANTIC_VIEW = re.compile(r"\bSEMANTIC_VIEW\s*\(\s*([A-Za-z_][\w$.]*)", re.IGNORECASE)
_READ = re.compile(r"^\s*(?:WITH|SELECT)\b", re.IGNORECASE)
_COMMENT = re.compile(r"--[^\n]*")


def reads_only_the_view(sql: str) -> str:
    """Return why ``sql`` is out of bounds, or the empty string if it is not.

    Three questions, in the order a reviewer would ask them: is it a read, does
    it stay inside the semantic view, and does it mention the one column that
    must never appear in a generated query.

    Args:
        sql: The statement Cortex Analyst returned.

    Returns:
        A reason suitable for a span attribute, or ``""`` when the statement is
        a single read that names only this view and its five tables.
    """
    stripped = _COMMENT.sub(" ", sql).strip().rstrip(";")
    if not _READ.match(stripped):
        return "the statement is not a single SELECT"
    if ";" in stripped:
        return "the statement carries a second statement after a semicolon"
    if re.search(r"\bdemo_id\b", stripped, re.IGNORECASE):
        return (
            "the statement names demo_id. No tool signature carries a visitor "
            "identifier, so this is either an invented literal or a reach at "
            "another visitor's rows"
        )

    ours = {semantic.qualified().lower(), semantic.VIEW_NAME.lower()}
    for view in _SEMANTIC_VIEW.findall(stripped):
        if view.lower() not in ours:
            return f"the statement queries semantic view {view}, not the account lane"

    defined = {name.lower() for name in _CTE.findall(stripped)}
    for source in _SOURCE.findall(stripped):
        name = source.lower()
        if name in defined or name.startswith("semantic_view"):
            continue
        if name not in _ALLOWED_TABLES:
            return f"the statement reads {source}, which the account lane does not model"
    return ""


def decide(
    response: Mapping[str, Any] | None,
    *,
    elapsed_seconds: float,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Answer or decline, given one Cortex Analyst response.

    Args:
        response: The decoded ``/api/v2/cortex/analyst/message`` body, or
            ``None`` where the call failed or timed out.
        elapsed_seconds: How long the call took, measured by the caller. Past
            :attr:`Thresholds.timeout_seconds` the response is discarded even
            if it arrived, because a turn that took too long has failed as a
            conversation whatever it eventually said.
        thresholds: The floor and the deadline. :meth:`Thresholds.from_env`
            where omitted -- so a caller that does nothing gets the argued
            defaults rather than none.

    Returns:
        The decision. :attr:`Decision.answered` is the only field #61 has to
        branch on; the rest is what the span records.
    """
    rules = Thresholds.from_env() if thresholds is None else thresholds

    if elapsed_seconds > rules.timeout_seconds:
        return _declined(
            Path.UNAVAILABLE,
            f"Cortex Analyst took {elapsed_seconds:.1f}s, past the "
            f"{rules.timeout_seconds:g}s deadline",
        )
    if not response or "message" not in response:
        return _declined(Path.UNAVAILABLE, "Cortex Analyst returned no message")

    content = response["message"].get("content") or []
    statement = ""
    verified = ""
    suggestions: tuple[str, ...] = ()
    interpretation = ""
    for part in content:
        kind = part.get("type")
        if kind == "sql":
            statement = part.get("statement", "")
            used = (part.get("confidence") or {}).get("verified_query_used") or {}
            verified = used.get("name", "")
        elif kind == "suggestions":
            suggestions = tuple(part.get("suggestions") or ())
        elif kind == "text":
            interpretation = part.get("text", "")

    if not statement.strip():
        return _declined(
            Path.SUGGESTED,
            "Cortex Analyst returned no SQL for the question",
            suggestions=suggestions,
            interpretation=interpretation,
        )

    out_of_bounds = reads_only_the_view(statement)
    if out_of_bounds:
        return _declined(
            Path.GENERATED if not verified else Path.VERIFIED,
            out_of_bounds,
            suggestions=suggestions,
            interpretation=interpretation,
        )

    path = Path.VERIFIED if verified else Path.GENERATED
    if not rules.admits(path):
        return _declined(
            path,
            f"the {path.value} path is worth {CONFIDENCE[path]:g} and the floor "
            f"is {rules.min_confidence:g}",
            suggestions=suggestions,
            interpretation=interpretation,
        )

    return Decision(
        answered=True,
        path=path,
        confidence=CONFIDENCE[path],
        sql=statement,
        verified_query=verified,
        suggestions=suggestions,
        interpretation=interpretation,
    )


def _declined(
    path: Path,
    reason: str,
    *,
    suggestions: tuple[str, ...] = (),
    interpretation: str = "",
) -> Decision:
    """Build a refusal. Never carries SQL, whatever came back."""
    return Decision(
        answered=False,
        path=path,
        confidence=CONFIDENCE[path],
        reason=reason,
        suggestions=suggestions,
        interpretation=interpretation,
    )
