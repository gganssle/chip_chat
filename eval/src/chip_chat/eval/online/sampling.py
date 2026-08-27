"""Which live turns get a judge, and why the rate is a decision rather than a knob.

#76 says *sampling rate chosen deliberately -- judges cost tokens, and this is a
demo with a spend ceiling*. So the rate is here, with the argument, rather than
in a flag somebody sets from memory.

**The rate is 20% and the reasoning is arithmetic.** A judged turn costs two
model calls -- groundedness and the refusal -- against roughly six hundred prompt
tokens each, because the passages go in the prompt. A visitor conversation is
four or five turns. At a full day of demo traffic the difference between judging
everything and judging a fifth is the difference between the judges being a
rounding error against the daily ceiling and being a visible fraction of it, and
:mod:`chip_chat.eval.online.budget` is where that fraction is computed rather
than asserted. Twenty percent of a few hundred turns is still enough turns to see
a systematic failure; it is not enough to catch a rare one, which is what the
always-sampled classes below are for.

**Three classes are always judged, and each is a different argument.**

*Allergen and dietary turns.* PRD §10 makes this the subject where a confident
wrong answer is a safety problem rather than an accuracy one, and a fifth of a
safety property is not a safety property. The screen that identifies them is a
keyword sweep and is wrong in both directions -- see
:attr:`~chip_chat.eval.online.signals.LiveTurn.dietary` -- and over-sampling is
the correct direction to be wrong in.

*Turns that a deterministic monitor already fired on.* If something cheap has
already said this turn looks wrong, a judge is the thing that says what about it,
and declining to spend two calls on a turn already known to be interesting is
saving money on the only turns worth reading.

*Turns that made a claim and retrieved nothing.* The floor under groundedness,
free to detect, and the shape most likely to be an ungrounded menu claim.

**The decision is deterministic and reproducible.** Sampling on a random number
means a turn's fate cannot be reconstructed, and *"why was this not judged"* is a
question somebody asks about exactly the turn that mattered. So the decision is a
hash of the trace id: the same trace is always sampled or always not, the
distribution is uniform, and re-running the sampler over yesterday's traces
selects yesterday's sample.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.eval.online.signals import LiveTurn

__all__ = ["DEFAULT_RATE", "Reason", "SamplingDecision", "SamplingPolicy"]

DEFAULT_RATE: Final = 0.20
"""What fraction of ordinary turns are judged. See the module docstring."""

_BUCKETS: Final = 10_000
"""Resolution of the hash bucket. Four digits is finer than any rate anybody
will choose and coarse enough that the arithmetic is obvious."""


class Reason(StrEnum):
    """Why a turn was or was not judged.

    Attributes:
        RATE: It fell inside the sampled fraction.
        DIETARY: It touched the allergen and dietary subject.
        FLAGGED: A deterministic monitor had already fired on it.
        UNGROUNDED: It made a claim and retrieved nothing.
        NOT_SAMPLED: It fell outside the fraction and no class claimed it.
        UNREADABLE: Its trace could not be believed, so nothing may be scored
            from it -- and a judge asked about it would produce a verdict about
            half a turn.
    """

    RATE = "rate"
    DIETARY = "dietary"
    FLAGGED = "flagged"
    UNGROUNDED = "ungrounded"
    NOT_SAMPLED = "not_sampled"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Whether one turn gets a judge, and on what grounds.

    Attributes:
        judged: Whether to spend the two calls.
        reason: Which rule decided.
        bucket: Where the trace id landed, out of :data:`_BUCKETS`. Recorded so
            a decision can be re-derived by hand from the trace id alone.
    """

    judged: bool
    reason: Reason
    bucket: int

    def __bool__(self) -> bool:
        """A decision reads as its own answer at a call site."""
        return self.judged


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """The rate, and the classes that ignore it.

    Attributes:
        rate: Fraction of ordinary turns to judge, in ``[0, 1]``.
        always_dietary: Judge every allergen and dietary turn.
        always_flagged: Judge every turn a deterministic monitor fired on.
        always_ungrounded: Judge every turn that claimed and retrieved nothing.
    """

    rate: float = DEFAULT_RATE
    always_dietary: bool = True
    always_flagged: bool = True
    always_ungrounded: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError(f"the sampling rate must be in [0, 1], got {self.rate}")

    def decide(self, turn: LiveTurn, *, flagged: bool = False) -> SamplingDecision:
        """Whether to judge this turn.

        Args:
            turn: The live turn.
            flagged: Whether a deterministic monitor already fired on it. Passed
                in rather than computed, because the monitors run first and
                running them twice to decide whether to judge would be the
                cheapest thing in the loop done twice for no reason.

        Returns:
            The decision, carrying which rule made it.
        """
        bucket = _bucket(turn.trace_id)
        if not turn.readable:
            return SamplingDecision(False, Reason.UNREADABLE, bucket)
        if self.always_flagged and flagged:
            return SamplingDecision(True, Reason.FLAGGED, bucket)
        if self.always_dietary and turn.dietary:
            return SamplingDecision(True, Reason.DIETARY, bucket)
        if self.always_ungrounded and turn.searched and not turn.retrieved:
            return SamplingDecision(True, Reason.UNGROUNDED, bucket)
        if bucket < self.rate * _BUCKETS:
            return SamplingDecision(True, Reason.RATE, bucket)
        return SamplingDecision(False, Reason.NOT_SAMPLED, bucket)

    def describe(self) -> str:
        """The policy in one line, for the head of a report."""
        always = [
            name
            for name, on in (
                ("allergen and dietary", self.always_dietary),
                ("already flagged", self.always_flagged),
                ("claimed with nothing retrieved", self.always_ungrounded),
            )
            if on
        ]
        return f"{self.rate:.0%} of ordinary turns, and every turn that is " + ", ".join(
            always
        )


def _bucket(trace_id: str) -> int:
    """Where a trace id lands, deterministically.

    SHA-256 rather than :func:`hash`, because Python's string hash is salted per
    process: the same trace would be judged in one run and not in the next, and
    the whole point of hashing rather than rolling a die is that it does not.
    """
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _BUCKETS
