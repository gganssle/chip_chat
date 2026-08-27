"""Doubles for the two Snowflake-backed lanes, so their failure paths are cheap.

RFC-001 §10 gives every lane a decline, and a decline is what a live service is
worst at producing on demand. So the account and personalization lanes are built
against two seams -- :class:`~chip_chat.snowflake.reads.Connection` and
:class:`~chip_chat.snowflake.cortex.AnalystTransport` -- and this module
implements both over dictionaries.

Ships with the package for the same reason :mod:`chip_chat.agent.testing` and
:mod:`chip_chat.otel.testing` do: ``agent/tests`` drives whole turns through
these lanes, and a double living under ``snowflake/tests`` would not be
importable from there.

**The fakes refuse what they do not model.** :class:`FakeConnection` raises on a
statement it was not given an answer for, rather than returning an empty result.
An empty result is a *meaningful* answer everywhere in this package -- no usual
order, no recommendations, an unloaded reward catalogue -- so a fake that
shrugged with one would let a test pass while measuring the fake.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from chip_chat.snowflake import reads

__all__ = [
    "FakeConnection",
    "StubAnalyst",
    "UnmodelledStatementError",
    "analyst_response",
    "checkout_of",
    "failing_checkout",
    "sql_part",
    "suggestions_part",
]


class UnmodelledStatementError(RuntimeError):
    """The fake connection was asked something it holds no answer for.

    Deliberately fatal. See the module docstring: empty means something here.
    """


def _normalise(sql: str) -> str:
    """Collapse whitespace so a test can match a statement it wrote by hand."""
    return " ".join(sql.split()).rstrip(";").casefold()


class FakeConnection:
    """A :class:`~chip_chat.snowflake.reads.Connection` over a dictionary.

    Attributes:
        statements: Every statement run on it, in order, as they were written.
            The assertion that matters is usually about what was *not* run.
    """

    __slots__ = ("_answers", "_raises", "statements")

    def __init__(
        self,
        answers: Mapping[str, Sequence[Sequence[object]]] | None = None,
        *,
        raises: Mapping[str, Exception] | None = None,
    ) -> None:
        """Initialise the connection.

        Args:
            answers: Statement to rows. Matched on whitespace-collapsed,
                case-folded text, so a test can write the SQL the way it reads.
            raises: Statement to the exception running it should produce. The
                serving table that does not exist yet, and the warehouse that
                refused.
        """
        self._answers = {_normalise(sql): rows for sql, rows in (answers or {}).items()}
        self._raises = {_normalise(sql): error for sql, error in (raises or {}).items()}
        self.statements: list[str] = []

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Return the rows this statement was given, or raise.

        Raises:
            UnmodelledStatementError: If the statement was not configured.
            Exception: Whatever ``raises`` holds for this statement.
        """
        del parameters
        self.statements.append(sql)
        key = _normalise(sql)
        if key in self._raises:
            raise self._raises[key]
        if key in self._answers:
            return self._answers[key]
        raise UnmodelledStatementError(
            f"the fake connection holds no answer for {sql!r}; add one and say "
            "what #43's policies would do with it"
        )


def checkout_of(connection: reads.Connection) -> reads.SessionCheckout:
    """Return a :data:`~chip_chat.snowflake.reads.SessionCheckout` over one connection.

    The session id is accepted and ignored, which is the shape of the real thing
    from the lane's side: it hands the id to the pool and the pool is what knows
    whose it is. A fake that resolved the id itself would be a fake asserting the
    identity the pool exists to assert.
    """

    @contextmanager
    def checkout(session_id: str) -> Iterator[reads.Connection]:
        del session_id
        yield connection

    return checkout


def failing_checkout(error: Exception) -> reads.SessionCheckout:
    """Return a checkout that raises ``error`` instead of yielding.

    The RFC-001 §10 path: the pool could not produce a bound connection, so the
    lane declines and the conversation continues. Every lane method has to
    survive this, which is why it is one line to arrange.
    """

    @contextmanager
    def checkout(session_id: str) -> Iterator[reads.Connection]:
        del session_id
        raise error
        yield  # pragma: no cover - unreachable, and what makes this a generator

    return checkout


class StubAnalyst:
    """An :class:`~chip_chat.snowflake.cortex.AnalystTransport` over a script.

    Attributes:
        questions: Every question it was asked, in order. What a test asserts on
            to show the visitor's words reached Cortex Analyst unchanged.
    """

    __slots__ = ("_elapsed", "_error", "_responses", "questions")

    def __init__(
        self,
        *responses: Mapping[str, Any] | None,
        elapsed_seconds: float = 3.65,
        error: Exception | None = None,
    ) -> None:
        """Initialise the stub.

        Args:
            responses: What :meth:`ask` returns, one per call. The last is
                repeated once the script runs out, so a test that only cares
                about one call writes one.
            elapsed_seconds: What it reports the round trip took. Defaults to
                the median measured against the live account on 2026-08-27
                (``docs/snowflake-semantic-view.md`` §4), so a test that does
                nothing about time is testing against a realistic one.
            error: Raised instead of answering. For the token source that could
                not mint, which is the one failure the real transport lets out.
        """
        self._responses = list(responses) or [None]
        self._elapsed = elapsed_seconds
        self._error = error
        self.questions: list[str] = []

    def ask(self, question: str) -> tuple[Mapping[str, Any] | None, float]:
        """Record the question and return the next scripted response."""
        self.questions.append(question)
        if self._error is not None:
            raise self._error
        response = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        return response, self._elapsed


def analyst_response(*content: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap content parts the way ``/api/v2/cortex/analyst/message`` wraps them."""
    return {"message": {"role": "analyst", "content": [dict(part) for part in content]}}


def sql_part(statement: str, *, verified: str = "") -> dict[str, Any]:
    """One ``sql`` content part, with or without a verified query behind it."""
    return {
        "type": "sql",
        "statement": statement,
        "confidence": {"verified_query_used": {"name": verified} if verified else None},
    }


def suggestions_part(*questions: str) -> dict[str, Any]:
    """One ``suggestions`` part: Analyst saying what it does cover instead."""
    return {"type": "suggestions", "suggestions": list(questions)}
