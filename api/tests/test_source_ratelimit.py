"""Per-source-address limiting, against the loop the acceptance criteria name.

Time is driven by a fake clock, so the window is asserted rather than waited
for. A test that slept for the window would be sixty seconds slower and would
still only prove that sleeping works.
"""

from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import BudgetScope, StopReason
from chip_chat.api.ratelimit import SourceRateLimiter
from chip_chat.api.testing import FakeClock


def test_requests_under_the_ceiling_are_admitted(
    limits: SpendLimits, clock: FakeClock
) -> None:
    limiter = SourceRateLimiter(limits, clock)

    admitted = [
        limiter.check("203.0.113.7") for _ in range(limits.source_requests_per_window)
    ]

    assert admitted == [None] * limits.source_requests_per_window


def test_a_naive_loop_from_one_host_is_refused(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """The scenario in the issue: one host, one tight loop, no pauses."""
    limiter = SourceRateLimiter(limits, clock)

    refusals = [limiter.check("203.0.113.7") for _ in range(200)]

    allowed = [outcome for outcome in refusals if outcome is None]
    assert len(allowed) == limits.source_requests_per_window
    blocked = refusals[-1]
    assert blocked is not None
    assert blocked.reason is StopReason.SOURCE_RATE_LIMIT
    assert blocked.usage.scope is BudgetScope.SOURCE_ADDRESS


def test_one_hosts_loop_does_not_shut_out_everybody_else(
    limits: SpendLimits, clock: FakeClock
) -> None:
    limiter = SourceRateLimiter(limits, clock)
    for _ in range(200):
        limiter.check("203.0.113.7")

    assert limiter.check("198.51.100.4") is None


def test_the_window_slides_rather_than_resetting_wholesale(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """A fixed window would allow twice the promised rate across its boundary."""
    limiter = SourceRateLimiter(limits, clock)
    for _ in range(limits.source_requests_per_window):
        limiter.check("203.0.113.7")

    clock.advance(limits.source_window_seconds / 2)
    assert limiter.check("203.0.113.7") is not None

    clock.advance(limits.source_window_seconds / 2 + 1)
    assert limiter.check("203.0.113.7") is None


def test_a_refusal_does_not_extend_the_window(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """Otherwise a client that kept retrying would be banned rather than limited."""
    limiter = SourceRateLimiter(limits, clock)
    for _ in range(limits.source_requests_per_window):
        limiter.check("203.0.113.7")

    clock.advance(limits.source_window_seconds - 1)
    for _ in range(50):
        limiter.check("203.0.113.7")

    clock.advance(2)
    assert limiter.check("203.0.113.7") is None


def test_usage_reports_what_the_address_has_spent_of_its_allowance(
    limits: SpendLimits, clock: FakeClock
) -> None:
    limiter = SourceRateLimiter(limits, clock)
    limiter.check("203.0.113.7")
    limiter.check("203.0.113.7")

    usage = limiter.usage("203.0.113.7")

    assert usage.used == 2
    assert usage.limit == limits.source_requests_per_window
    assert usage.remaining == limits.source_requests_per_window - 2
