"""Test doubles for anything that sits behind the spend cap.

These ship with the package rather than living in ``api/tests`` because the
service built on top of the guard needs them too, and because the acceptance
criteria on this feature are stated in terms of one of them:

    A test that drives the counter past the ceiling and asserts NO model call is
    attempted -- assert on a mock that would record the call, not on the
    response text.

:class:`RecordingModel` is that mock. Asserting on the response text proves only
that the copy is right; asserting that this object recorded nothing proves that
no tokens were bought, which is the property that keeps the invoice small.

:class:`RecordingWriteBackend` is the same idea for the second launch gate. The
acceptance criteria of issue #63 are about writes that must *not* happen and
retries that must not double-write, and both are properties of what reached the
database rather than of what came back. So the double implements the one
behaviour of ``sql/12_procedures.sql`` those criteria turn on -- a retry key is
claimed once and replayed thereafter -- and counts the writes separately from
the calls.
"""

import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from chip_chat.api.ops import OpsUnavailableError

__all__ = [
    "EPOCH",
    "FakeClock",
    "ModelCall",
    "ProcedureCall",
    "RecordingModel",
    "RecordingWriteBackend",
]

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
"""An arbitrary but fixed instant, so a day boundary is something a test can aim at."""


class FakeClock:
    """A clock the test drives, with the two hands the spend cap reads."""

    __slots__ = ("_lock", "_monotonic", "_now")

    def __init__(self, start: datetime = EPOCH) -> None:
        """Initialise the clock.

        Args:
            start: The wall-clock instant :meth:`now` starts from. Must be
                timezone-aware; the daily ceiling converts it into its reset
                zone and a naive value would silently mean local time.

        Raises:
            ValueError: If ``start`` is naive.
        """
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._lock = threading.Lock()
        self._monotonic = 0.0
        self._now = start

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move both hands forward.

        Args:
            seconds: How far forward to move.
        """
        with self._lock:
            self._monotonic += seconds
            self._now += timedelta(seconds=seconds)

    def set_now(self, moment: datetime) -> None:
        """Jump wall-clock time to ``moment``, leaving the monotonic hand alone.

        For the day-boundary tests, where what matters is the calendar and not
        how many seconds elapsed.

        Args:
            moment: The new wall-clock instant. Must be timezone-aware.

        Raises:
            ValueError: If ``moment`` is naive.
        """
        if moment.tzinfo is None:
            raise ValueError("set_now needs a timezone-aware moment")
        with self._lock:
            self._now = moment


class ModelCall:
    """One invocation a :class:`RecordingModel` was asked to make.

    Attributes:
        prompt: What the caller sent.
        session_id: The conversation it belonged to, where the caller says.
    """

    __slots__ = ("prompt", "session_id")

    def __init__(self, prompt: str, session_id: str | None = None) -> None:
        self.prompt = prompt
        self.session_id = session_id

    def __repr__(self) -> str:
        return f"ModelCall(prompt={self.prompt!r}, session_id={self.session_id!r})"


class RecordingModel:
    """A stand-in for the agent that records every call and buys nothing.

    The point of the object is the *absence* of entries in :attr:`calls`. A
    guard test asserts ``model.calls == []`` after driving the ceiling past its
    limit; if the guard ever regresses into something asynchronous, that
    assertion fails even though the visitor-facing copy would still look right.
    """

    __slots__ = ("_lock", "calls", "completion_tokens", "prompt_tokens", "reply")

    def __init__(
        self,
        reply: str = "sure thing",
        *,
        prompt_tokens: int = 1_000,
        completion_tokens: int = 200,
    ) -> None:
        """Initialise the double.

        Args:
            reply: What :meth:`complete` returns.
            prompt_tokens: Input tokens each call claims to have cost.
            completion_tokens: Output tokens each call claims to have cost.
        """
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls: list[ModelCall] = []
        self._lock = threading.Lock()

    def complete(self, prompt: str, session_id: str | None = None) -> str:
        """Record the call and return the canned reply.

        Args:
            prompt: What the caller would have sent to the model.
            session_id: The conversation, where the caller tracks one.

        Returns:
            :attr:`reply`.
        """
        with self._lock:
            self.calls.append(ModelCall(prompt, session_id))
        return self.reply

    @property
    def call_count(self) -> int:
        """How many times a model would have been invoked."""
        with self._lock:
            return len(self.calls)

    @property
    def tokens_billed(self) -> int:
        """What every recorded call would have cost, in tokens."""
        return self.call_count * (self.prompt_tokens + self.completion_tokens)

    @property
    def prompts(self) -> Sequence[str]:
        """The prompts of every recorded call, in order."""
        with self._lock:
            return [call.prompt for call in self.calls]


class ProcedureCall:
    """One stored procedure call a :class:`RecordingWriteBackend` was asked for.

    Attributes:
        procedure: The fully qualified name, as the ops API assembled it.
        demo_id: The visitor bound on the session it was called through. On a
            real connection this is a session variable rather than an argument,
            and it is recorded here because "whose row did that write" is the
            question RFC-001 section 05 exists to answer.
        arguments: Positional, in declaration order, retry key first.
    """

    __slots__ = ("arguments", "demo_id", "procedure")

    def __init__(self, procedure: str, demo_id: str, arguments: Sequence[object]) -> None:
        self.procedure = procedure
        self.demo_id = demo_id
        self.arguments = tuple(arguments)

    @property
    def retry_key(self) -> str:
        """The idempotency key, which every procedure takes first."""
        return str(self.arguments[0])

    @property
    def name(self) -> str:
        """The unqualified procedure name."""
        return self.procedure.rsplit(".", 1)[-1]

    def __repr__(self) -> str:
        return f"ProcedureCall({self.name!r}, {self.demo_id!r}, {self.arguments!r})"


class RecordingWriteBackend:
    """A stand-in for the Functions app's Snowflake connection.

    Implements exactly as much of ``sql/12_procedures.sql`` as the ops API's
    acceptance criteria depend on:

    * a retry key is claimed once, and a second call carrying it returns the
      stored receipt with ``replayed`` true rather than writing again;
    * a key spent by a different action is ``RETRY_KEY_SPENT_ON_ANOTHER_ACTION``;
    * a rejection is a returned object with ``ok`` false, never an exception.

    The distinction that matters is :attr:`calls` versus :attr:`writes`. A test
    asserting "one write" asserts on the second: a call that replayed a receipt
    is a call the database answered and not a row anybody was charged for.
    """

    __slots__ = (
        "_available",
        "_commit_then_fail",
        "_lock",
        "_rejection",
        "_spent",
        "_unavailable_calls",
        "calls",
        "writes",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = True
        self._unavailable_calls = 0
        self._commit_then_fail = 0
        self._rejection: Mapping[str, Any] | None = None
        self._spent: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self.calls: list[ProcedureCall] = []
        self.writes: list[ProcedureCall] = []

    # --- what the ops API sees -------------------------------------------

    def session(self, demo_id: str) -> "RecordingWriteBackend._Session":
        """Acquire a session, or refuse the way an unreachable service does."""
        with self._lock:
            if not self._available:
                raise OpsUnavailableError("the recording backend is down")
        return RecordingWriteBackend._Session(self, demo_id)

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available."""
        with self._lock:
            return self._available

    # --- what a test drives ----------------------------------------------

    def take_down(self) -> None:
        """Make the write path unreachable, as taking the Functions app down does."""
        with self._lock:
            self._available = False

    def bring_up(self) -> None:
        """Reverse :meth:`take_down`."""
        with self._lock:
            self._available = True

    def fail_calls(self, times: int = 1) -> None:
        """Fail the next ``times`` calls before anything is written."""
        with self._lock:
            self._unavailable_calls = times

    def commit_then_fail(self, times: int = 1) -> None:
        """Write, then lose the connection -- the case the retry key exists for.

        The procedure commits and spends its key; the caller is told nothing.
        A retry carrying the same key must find the receipt rather than write a
        second order, which is the whole of acceptance criterion three.
        """
        with self._lock:
            self._commit_then_fail = times

    def reject_next(self, code: str, detail: str = "refused by the catalogue") -> None:
        """Make the next call return a typed rejection rather than a receipt."""
        with self._lock:
            self._rejection = {"ok": False, "rejection": code, "detail": detail}

    def receipts(self) -> tuple[Mapping[str, Any], ...]:
        """Every receipt written, in order. One per actual write."""
        with self._lock:
            return tuple(receipt for _, receipt in self._spent.values())

    # --- the procedure itself, in miniature -------------------------------

    def _call(
        self, demo_id: str, procedure_name: str, arguments: Sequence[object]
    ) -> Mapping[str, Any]:
        call = ProcedureCall(procedure_name, demo_id, arguments)
        with self._lock:
            self.calls.append(call)
            if self._unavailable_calls > 0:
                self._unavailable_calls -= 1
                raise OpsUnavailableError("the recording backend dropped the call")

            key = (demo_id, call.retry_key)
            spent = self._spent.get(key)
            if spent is not None:
                action, receipt = spent
                if action != call.name:
                    return {
                        "ok": False,
                        "rejection": "RETRY_KEY_SPENT_ON_ANOTHER_ACTION",
                        "detail": f"this retry key was already spent by {action}",
                    }
                return {**receipt, "replayed": True}

            if self._rejection is not None:
                rejection = dict(self._rejection)
                self._rejection = None
                return rejection

            self.writes.append(call)
            receipt = {
                "ok": True,
                "action": call.name.upper(),
                "subject_id": f"{call.name}-{len(self.writes)}",
                "arguments": list(call.arguments[1:]),
                "simulation": "Simulated. Nothing was cooked, charged or sent.",
            }
            self._spent[key] = (call.name, receipt)
            if self._commit_then_fail > 0:
                self._commit_then_fail -= 1
                raise OpsUnavailableError(
                    "the recording backend committed and then lost the connection"
                )
            return dict(receipt)

    class _Session:
        """One bound session. Takes no identifier: the visitor is already bound."""

        __slots__ = ("_backend", "_demo_id")

        def __init__(self, backend: "RecordingWriteBackend", demo_id: str) -> None:
            self._backend = backend
            self._demo_id = demo_id

        def call(
            self, procedure_name: str, arguments: Sequence[object]
        ) -> Mapping[str, Any]:
            """Call one procedure through this session."""
            return self._backend._call(self._demo_id, procedure_name, arguments)
