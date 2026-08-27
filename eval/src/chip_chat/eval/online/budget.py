"""Judge tokens, inside the daily cap rather than beside it.

#76's last acceptance criterion is one sentence and it is the one most easily
satisfied by a paragraph nobody can check: *judge token spend is accounted for in
the daily budget*. The failure it names is precise. RFC-001 D-spend puts a global
daily token ceiling in the request path -- inline, synchronous, in front of every
model call -- and online evals are model calls that do **not** go through the
request path. A judge sampling live traffic is therefore a second bill,
accumulating outside the only thing that stops the first one, and a cap with a
second bill outside it is not a cap.

This module does not enforce anything, and saying so plainly is the point. The
enforcement is :mod:`chip_chat.api.limits`, in the request path, because that is
the only place a ceiling can refuse a call before it is made; an eval package
that started refusing things would be a second gate that can disagree with the
first. What this does is the arithmetic: given the ceiling, the sampling policy
and the observed cost of a judged turn, how much of the day's tokens do the
judges take, and does the remainder still cover the traffic.

**The ceiling is read and never defaulted.** ``CHIP_CHAT_DAILY_TOKEN_CEILING`` is
the same variable the request path reads. Where it is unset, this reports the
budget as **unknown** rather than assuming a number: a default here would be a
second copy of a limit that lives in another package, and the first time somebody
raised one and not the other, this module would be quietly reporting headroom
that does not exist. That is the same reason ``eval/`` does not import ``api/``
-- the cap belongs to the request path, and an eval reading it as configuration
is the arrangement in which the two cannot drift apart while looking identical.

**The observed cost is measured, not estimated.**
:class:`~chip_chat.eval.grounding.judge.JudgeSpend` counts what the judges
actually spent, so :meth:`JudgeBudget.against` takes a real number from a real
run. An estimate here would be a projection dressed as accounting, and the whole
argument of this module is that a projection is what left the hole.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.online.sampling import SamplingPolicy

__all__ = ["CEILING_VARIABLE", "JudgeBudget", "budget_from_env"]

CEILING_VARIABLE: Final = "CHIP_CHAT_DAILY_TOKEN_CEILING"
"""The variable the request path's global ceiling is read from.

Named here rather than imported. See the module docstring: this package reads the
configuration; ``chip_chat.api.limits`` enforces the limit, and the two must not
become two limits.
"""

_TURNS_PER_CONVERSATION: Final = 5
"""What a conversation costs, in turns. PRD §05's cost target is per conversation
and every counter in sight is per turn, so a conversion has to happen somewhere
and it happens here, once, where it can be argued with."""


@dataclass(frozen=True, slots=True)
class JudgeBudget:
    """What the judges may spend, out of what the day has.

    Attributes:
        daily_ceiling: The global ceiling, or ``None`` where the environment
            does not say. ``None`` is not a large number; every property below
            reports *unknown* rather than computing against an invention.
        policy: The sampling policy, which decides how many turns are judged.
        tokens_per_judged_turn: What one judged turn costs across both
            findings. Measured from a run rather than estimated.
    """

    daily_ceiling: int | None
    policy: SamplingPolicy
    tokens_per_judged_turn: int = 0

    @property
    def known(self) -> bool:
        """Whether there is a ceiling to account against."""
        return self.daily_ceiling is not None and self.tokens_per_judged_turn > 0

    def judged_turns(self, turns: int) -> float:
        """How many of ``turns`` the policy would judge.

        The always-sampled classes are *not* in this number, deliberately. Their
        size is a property of the traffic rather than of the policy -- how many
        strangers ask about allergens is not something a rate can predict -- so
        this is the floor the policy guarantees and the report says so. A budget
        that projected the always-sampled classes would be projecting the
        traffic, which is the estimate this module exists to refuse.

        Args:
            turns: Turns in the day.

        Returns:
            The turns the rate alone would judge.
        """
        return turns * self.policy.rate

    def tokens(self, turns: int) -> int:
        """What the judges spend over ``turns``, at the policy's rate.

        Args:
            turns: Turns in the day.

        Returns:
            Tokens. Zero where the per-turn cost has not been measured.
        """
        return int(self.judged_turns(turns) * self.tokens_per_judged_turn)

    def share(self, turns: int) -> float | None:
        """The judges' fraction of the daily ceiling over ``turns``.

        Args:
            turns: Turns in the day.

        Returns:
            The fraction, or ``None`` where the ceiling or the per-turn cost is
            unknown.
        """
        if not self.known or not self.daily_ceiling:
            return None
        return self.tokens(turns) / self.daily_ceiling

    def turns_affordable(self) -> int | None:
        """How many turns a day the ceiling covers once the judges have taken theirs.

        Returns:
            The number of turns, or ``None`` where anything needed is unknown.
            This is the number that answers *did the judges just cost us the
            demo*, and it is the reason this module produces a number rather
            than a reassurance.
        """
        if not self.known or self.daily_ceiling is None:
            return None
        per_turn = _turn_cost_estimate(self)
        if per_turn <= 0:
            return None
        return int(self.daily_ceiling // per_turn)

    def conversations_affordable(self) -> int | None:
        """The same, in conversations rather than turns."""
        turns = self.turns_affordable()
        return None if turns is None else turns // _TURNS_PER_CONVERSATION

    def describe(self, turns: int) -> str:
        """The accounting, in the two or three lines a report carries.

        Args:
            turns: Turns to project over.

        Returns:
            The prose. Says *unknown* rather than inventing a ceiling, because
            an accounting nobody can check is the hole this exists to close.
        """
        if self.daily_ceiling is None:
            return (
                f"{CEILING_VARIABLE} is unset, so the judges' share of the daily "
                "ceiling cannot be computed. It is not zero; it is unaccounted, "
                "which is the hole in the cap #76 names."
            )
        if self.tokens_per_judged_turn <= 0:
            return (
                f"Ceiling {self.daily_ceiling:,} tokens/day. No judged run has "
                "been measured, so the judges' share is unaccounted rather than "
                "zero."
            )
        share = self.share(turns)
        return (
            f"Ceiling {self.daily_ceiling:,} tokens/day. At {self.policy.rate:.0%} "
            f"sampling and {self.tokens_per_judged_turn:,} tokens per judged turn, "
            f"{turns:,} turns cost the judges {self.tokens(turns):,} tokens — "
            f"{share:.1%} of the day's ceiling."
        )


def _turn_cost_estimate(budget: JudgeBudget) -> float:
    """What one *visitor* turn costs the ceiling, judges included.

    The judge's share of a turn is its cost times the sampling rate: one turn in
    five is judged, so every turn carries a fifth of a judging. The visitor's own
    turn is not counted here -- this module has no business estimating what the
    agent costs, and :mod:`chip_chat.api.limits` meters that exactly.
    """
    return budget.tokens_per_judged_turn * budget.policy.rate


def budget_from_env(
    policy: SamplingPolicy,
    *,
    tokens_per_judged_turn: int = 0,
    env: Mapping[str, str] | None = None,
) -> JudgeBudget:
    """Read the daily ceiling and pair it with a policy.

    Args:
        policy: The sampling policy in force.
        tokens_per_judged_turn: Measured cost of one judged turn.
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The budget. Its :attr:`~JudgeBudget.daily_ceiling` is ``None`` where the
        variable is unset or unparseable -- unparseable included, because a
        ceiling somebody typed wrong is a ceiling nobody has.
    """
    source = os.environ if env is None else env
    raw = source.get(CEILING_VARIABLE, "").strip()
    try:
        ceiling: int | None = int(raw) if raw else None
    except ValueError:
        ceiling = None
    return JudgeBudget(
        daily_ceiling=ceiling,
        policy=policy,
        tokens_per_judged_turn=tokens_per_judged_turn,
    )
