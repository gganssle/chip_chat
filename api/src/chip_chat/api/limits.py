"""The numbers the spend cap enforces, and where they come from.

Four layers, per RFC-001 section 11, plus the upload ceilings issue #80 adds
underneath them, and every one of them has a knob here:

=========================== ===================================================
Layer                       Knob
=========================== ===================================================
Per-request global ceiling  :attr:`SpendLimits.daily_token_ceiling`
Per-session                 :attr:`SpendLimits.session_turn_cap`,
                            :attr:`SpendLimits.session_token_cap`
Per-source-address          :attr:`SpendLimits.source_requests_per_window`,
                            :attr:`SpendLimits.source_window_seconds`
Per-upload                  :attr:`SpendLimits.session_uploads_per_window`,
                            :attr:`SpendLimits.source_uploads_per_window`,
                            :attr:`SpendLimits.upload_window_seconds`,
                            :attr:`SpendLimits.upload_token_charge`
Circuit breaker             not a number -- see :mod:`chip_chat.api.killswitch`
=========================== ===================================================

The defaults are deliberately small. This is a demo attached to a real
subscription, and the failure mode that costs money is a ceiling set high
"for now" and never revisited.

There is a second failure mode, and issue #108 is what it looks like: a ceiling
set small against a system that then grew underneath it, so that the cap starts
refusing the ordinary case and reads to the visitor as a fault. Every number
here is therefore written with the arithmetic that produced it and the date it
was measured, so the next person retuning one can see what it was sized against
rather than guessing whether it was ever sized at all.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["SpendLimits"]

DEFAULT_DAILY_TOKEN_CEILING = 8_000_000
"""Tokens across every visitor, per day.

Ten full conversations, and the arithmetic is deliberately that simple: ten
times :data:`DEFAULT_SESSION_TOKEN_CAP` is 8,000,000, so the number of visitors
who can reach the end of a conversation on the worst possible day is a number
somebody can hold in their head rather than a quotient they have to work out.

Note which direction this moved the headroom. The old pair -- 2,000,000 over a
120,000 session cap -- admitted about sixteen capped conversations; the new pair
admits ten. **The ceiling went up four times and the number of visitors it
serves went down**, because the conversation it is now sized for is four times
longer than the one it was sized for before. That is the honest reading of the
change and it is the reason this is not simply a bigger number: the worst-case
day now costs four times what it used to (about $2.70 of `gpt-5-mini` at list,
`docs/cost.md` §3.2's prices against §14's measured 95%-prompt split), against a
day that has never in fact exceeded thirteen conversations.
"""

DEFAULT_SESSION_TURN_CAP = 22
"""Turns one conversation may take, as a backstop rather than as the cap.

Twenty is the conversation this demo is built to hold: enough to visit all five
lanes, order something, change your mind, and ask about the photo you took.
Twenty-two is that plus a little, so that a visitor having the intended
conversation is stopped by :data:`DEFAULT_SESSION_TOKEN_CAP` -- the honest,
cost-shaped ceiling -- rather than by a turn count that knows nothing about what
the turns cost.

What this leaves the turn cap doing is the case the token cap cannot see: a loop
that is pathologically *cheap*. Twenty-two one-word turns cost almost nothing
and would run for a very long time under a token ceiling alone, and a session is
also a row in a ledger and a persona held out of the roster. The turn cap bounds
that; it is not meant to bound spend.

**The two caps used to contradict each other, and it went unnoticed because the
contradiction was invisible from either side.** 120,000 over 40 turns is 3,000
tokens a turn, which is *below* the 8,000
:data:`DEFAULT_TURN_TOKEN_RESERVATION` a single turn claims before the model is
even called -- so the token cap refused the sixteenth turn of the cheapest
conversation that could possibly exist, and forty was unreachable dead
configuration from the day it was written. The reconciliation to check whenever
either number is retuned is that quotient: 800,000 over 22 is 36,364 tokens a
turn, comfortably above the reservation and inside the range of turns actually
measured on 2026-08-31 (mean 27,437, largest 36,938). Both numbers now describe
the same conversation instead of two that differ by a factor of three.
"""

DEFAULT_SESSION_TOKEN_CAP = 800_000
"""Tokens one conversation may consume, so one visitor cannot take the day.

Sized from measurement rather than from feel, and the measurement is the whole
of issue #108. Five consecutive turns of one real conversation during the user
testing session of 2026-08-31, read off ``chip_chat.tokens.total`` on
``chat.turn`` in Application Insights between 14:03 and 14:09 UTC:

=========== ============ ==============
Turn        this turn    cumulative
=========== ============ ==============
1           30,339       30,339
2           33,708       64,047
3           18,125       82,172
4           18,074       100,246
5           36,938       137,184
=========== ============ ==============

The old cap was 120,000. **It is crossed on the fifth turn**, which is exactly
the report that opened #108 -- *"it happens after only about five or ten turns"*
-- and it is not a bug in the ledger: the reservations were settling correctly
and those are the real numbers a turn costs on the deployed app. The cap was set
when a turn was much cheaper, before #106 wired the knowledge lane, and a turn
now carries the system prompt, eleven tool schemas, retrieved passages on every
food question and the whole history replayed at each agent step. The mean is
27,437 and the prompt is nearly all of it -- 28,620 prompt against 1,719
completion on turn 1.

800,000 is twenty turns at 40,000, and 40,000 is chosen above the mean rather
than at it for a reason visible in the table: **prompt tokens grow with the
history, so late turns cost more than early ones.** Turn 5 carried 35,998 prompt
tokens where turn 1 carried 28,620. A flat per-turn average understates a long
conversation, so multiplying the mean by twenty (548,740) would have produced a
cap that binds somewhere around turn sixteen -- the same mistake as the old one,
made a second time with better arithmetic. The margin between 27,437 and 40,000
is what pays for the growth, and it is a margin rather than a measurement:
`docs/decisions/session-token-cap.md` records that the growth curve past turn
five is extrapolated from five points and has never been observed.
"""

DEFAULT_SOURCE_REQUESTS_PER_WINDOW = 20
DEFAULT_SOURCE_WINDOW_SECONDS = 60.0
"""Twenty turns a minute from one address. A person cannot type that fast."""

DEFAULT_SESSION_UPLOADS_PER_WINDOW = 5
DEFAULT_SOURCE_UPLOADS_PER_WINDOW = 10
DEFAULT_UPLOAD_WINDOW_SECONDS = 300.0
"""Uploads are counted separately, and much more tightly, than turns.

A turn is text and costs a few thousand tokens. An upload is a Content Safety
call, a blob write, a vision call and a retention obligation, and a script can
issue them as fast as it can open sockets -- which makes upload flooding a cost
attack before it is a storage one. Five per session and ten per address in five
minutes is more photographs than any visitor takes of one lunch, and far fewer
than a loop can send in a second.

Two ceilings rather than one because they fail differently: the session number
is what a single conversation may do, and the address number is what survives
somebody minting a fresh session per upload -- which costs an attacker nothing,
and is the whole reason RFC-001 section 11 puts a per-address layer underneath
the per-session one.
"""

DEFAULT_UPLOAD_TOKEN_CHARGE = 1_500
"""What one accepted upload costs the turn's budget before any model answers.

An accepted photograph *will* be sent to the vision model -- that is what
accepting it means -- and at
:data:`~chip_chat.vision.limits.DEFAULT_MAX_EDGE` on the longest edge that is
about four 512-pixel tiles plus the prompt. Charging it at acceptance rather
than at completion is the same reservation-then-settle argument the ledger makes
in :mod:`chip_chat.api.ledger`: a cost known to be coming, counted before it
arrives, so that a burst of uploads cannot each pass a ceiling that none of them
has been charged against yet. The real number replaces it when
:meth:`~chip_chat.api.guard.TurnBudget.record_usage` reports what the model
actually billed.
"""

DEFAULT_TURN_TOKEN_RESERVATION = 32_000
"""What a turn is charged against the ceiling before the model is called.

The check is synchronous and the true cost is not known until the model has
already answered, so a turn reserves a pessimistic estimate up front and settles
the real number afterwards. Set this at or above the worst turn the agent can
produce: too low and concurrent turns can collectively overshoot the ceiling,
which is the one thing this module exists to prevent.

**8,000 never satisfied that sentence, and raising the agent's completion
ceiling is what forced the arithmetic to be done.** The five turns measured on
2026-08-31 and tabulated under :data:`DEFAULT_SESSION_TOKEN_CAP` cost a mean of
27,437 tokens and a largest of 36,938 -- so the reservation was understating a
typical turn by a factor of three and the worst one by four and a half, and had
been since #106 wired the knowledge lane and put retrieved passages into every
food answer. Sequential turns never noticed, because the reservation is settled
against the real number the moment the model answers; the exposure was always
and only concurrent turns, each of which could claim 8,000 and then spend
36,938, and the daily ceiling would find out afterwards.

``chip-1sq`` raised
:class:`~chip_chat.agent.model.AzureChatModel`'s ``max_completion_tokens`` from
2,000 to 16,000, which moves the worst turn the agent can produce up by
fourteen thousand per step. Leaving the reservation where it was would have
widened a gap that was already the wrong sign.

**32,000 is bounded from above, not chosen from feel.** The reconciliation
:data:`DEFAULT_SESSION_TURN_CAP` asks for -- that the session token cap divided
by the turn cap stays above the reservation, or the token cap refuses a turn the
turn cap was supposed to allow -- gives 800,000 over 22, which is 36,364. The
reservation must sit under that and above the measured worst turn of 36,938...
which it cannot do, because those two numbers cross. **That is a real conflict
and 32,000 does not resolve it**: it sits above the mean and below the quotient,
and a turn as expensive as the worst one measured will still be under-reserved
by about five thousand tokens while it is in flight. Closing it properly means
either a larger session token cap or a reservation that knows the size of the
history it is about to replay, and neither is a change to make inside a bug
fix. What is fixed here is the factor of three; what remains is a margin of
fifteen percent on the tail, recorded rather than papered over.
"""

DEFAULT_RESET_TIMEZONE = "UTC"
"""The zone whose midnight rolls the daily counter over.

Named explicitly because "daily" is meaningless without it, and because a
ceiling that resets at 5pm local time is the kind of subtly wrong that is only
noticed from the invoice.
"""

_ENV_PREFIX = "CHIP_CHAT_"


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(_ENV_PREFIX + key, "").strip()
    if not raw:
        return default
    return int(raw)


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(_ENV_PREFIX + key, "").strip()
    if not raw:
        return default
    return float(raw)


@dataclass(frozen=True, slots=True)
class SpendLimits:
    """Every ceiling the guard evaluates, validated on construction."""

    daily_token_ceiling: int = DEFAULT_DAILY_TOKEN_CEILING
    session_turn_cap: int = DEFAULT_SESSION_TURN_CAP
    session_token_cap: int = DEFAULT_SESSION_TOKEN_CAP
    source_requests_per_window: int = DEFAULT_SOURCE_REQUESTS_PER_WINDOW
    source_window_seconds: float = DEFAULT_SOURCE_WINDOW_SECONDS
    session_uploads_per_window: int = DEFAULT_SESSION_UPLOADS_PER_WINDOW
    source_uploads_per_window: int = DEFAULT_SOURCE_UPLOADS_PER_WINDOW
    upload_window_seconds: float = DEFAULT_UPLOAD_WINDOW_SECONDS
    upload_token_charge: int = DEFAULT_UPLOAD_TOKEN_CHARGE
    turn_token_reservation: int = DEFAULT_TURN_TOKEN_RESERVATION
    reset_timezone: str = DEFAULT_RESET_TIMEZONE

    def __post_init__(self) -> None:
        """Refuse a configuration that would not actually cap anything.

        Raises:
            ValueError: If any limit is not positive, or the timezone is unknown.
        """
        positive: dict[str, float] = {
            "daily_token_ceiling": self.daily_token_ceiling,
            "session_turn_cap": self.session_turn_cap,
            "session_token_cap": self.session_token_cap,
            "source_requests_per_window": self.source_requests_per_window,
            "source_window_seconds": self.source_window_seconds,
            "session_uploads_per_window": self.session_uploads_per_window,
            "source_uploads_per_window": self.source_uploads_per_window,
            "upload_window_seconds": self.upload_window_seconds,
            "upload_token_charge": self.upload_token_charge,
            "turn_token_reservation": self.turn_token_reservation,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        # Resolved eagerly: a typo in the zone name must fail at startup, not at
        # the first midnight after the link was shared.
        self.zone()

    def zone(self) -> ZoneInfo:
        """Return the timezone whose midnight rolls the daily counter over.

        Returns:
            The resolved zone.

        Raises:
            ValueError: If :attr:`reset_timezone` names no known zone.
        """
        try:
            return ZoneInfo(self.reset_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(
                f"unknown reset_timezone: {self.reset_timezone!r}"
            ) from error

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SpendLimits":
        """Build limits from the environment.

        Reads ``CHIP_CHAT_DAILY_TOKEN_CEILING``, ``CHIP_CHAT_SESSION_TURN_CAP``,
        ``CHIP_CHAT_SESSION_TOKEN_CAP``, ``CHIP_CHAT_SOURCE_REQUESTS_PER_WINDOW``,
        ``CHIP_CHAT_SOURCE_WINDOW_SECONDS``,
        ``CHIP_CHAT_SESSION_UPLOADS_PER_WINDOW``,
        ``CHIP_CHAT_SOURCE_UPLOADS_PER_WINDOW``,
        ``CHIP_CHAT_UPLOAD_WINDOW_SECONDS``, ``CHIP_CHAT_UPLOAD_TOKEN_CHARGE``,
        ``CHIP_CHAT_TURN_TOKEN_RESERVATION`` and
        ``CHIP_CHAT_BUDGET_RESET_TIMEZONE``. Every one is optional.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configured limits.

        Raises:
            ValueError: If a value is unparseable or would not cap anything.
        """
        source = os.environ if env is None else env
        return cls(
            daily_token_ceiling=_positive_int(
                source, "DAILY_TOKEN_CEILING", DEFAULT_DAILY_TOKEN_CEILING
            ),
            session_turn_cap=_positive_int(
                source, "SESSION_TURN_CAP", DEFAULT_SESSION_TURN_CAP
            ),
            session_token_cap=_positive_int(
                source, "SESSION_TOKEN_CAP", DEFAULT_SESSION_TOKEN_CAP
            ),
            source_requests_per_window=_positive_int(
                source, "SOURCE_REQUESTS_PER_WINDOW", DEFAULT_SOURCE_REQUESTS_PER_WINDOW
            ),
            source_window_seconds=_positive_float(
                source, "SOURCE_WINDOW_SECONDS", DEFAULT_SOURCE_WINDOW_SECONDS
            ),
            session_uploads_per_window=_positive_int(
                source,
                "SESSION_UPLOADS_PER_WINDOW",
                DEFAULT_SESSION_UPLOADS_PER_WINDOW,
            ),
            source_uploads_per_window=_positive_int(
                source, "SOURCE_UPLOADS_PER_WINDOW", DEFAULT_SOURCE_UPLOADS_PER_WINDOW
            ),
            upload_window_seconds=_positive_float(
                source, "UPLOAD_WINDOW_SECONDS", DEFAULT_UPLOAD_WINDOW_SECONDS
            ),
            upload_token_charge=_positive_int(
                source, "UPLOAD_TOKEN_CHARGE", DEFAULT_UPLOAD_TOKEN_CHARGE
            ),
            turn_token_reservation=_positive_int(
                source, "TURN_TOKEN_RESERVATION", DEFAULT_TURN_TOKEN_RESERVATION
            ),
            reset_timezone=source.get(_ENV_PREFIX + "BUDGET_RESET_TIMEZONE", "").strip()
            or DEFAULT_RESET_TIMEZONE,
        )
