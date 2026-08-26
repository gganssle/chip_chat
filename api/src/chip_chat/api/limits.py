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
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["SpendLimits"]

DEFAULT_DAILY_TOKEN_CEILING = 2_000_000
"""Tokens across every visitor, per day. Roughly a day of honest demo traffic."""

DEFAULT_SESSION_TURN_CAP = 40
"""Turns one conversation may take. Long enough to demo, short enough to bound."""

DEFAULT_SESSION_TOKEN_CAP = 120_000
"""Tokens one conversation may consume, so one visitor cannot take the day."""

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

DEFAULT_TURN_TOKEN_RESERVATION = 8_000
"""What a turn is charged against the ceiling before the model is called.

The check is synchronous and the true cost is not known until the model has
already answered, so a turn reserves a pessimistic estimate up front and settles
the real number afterwards. Set this at or above the worst turn the agent can
produce: too low and concurrent turns can collectively overshoot the ceiling,
which is the one thing this module exists to prevent.
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
