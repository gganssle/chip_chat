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
"""

import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

__all__ = ["EPOCH", "FakeClock", "ModelCall", "RecordingModel"]

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
