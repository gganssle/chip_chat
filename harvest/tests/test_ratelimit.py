"""Rate limiting and the concurrency ceiling.

Nothing here sleeps. The limiter is driven by a fake clock, so what the tests
assert on is the wait each caller was *handed*, which is the thing that has to
be right; a test that measured elapsed wall time would be slower, flakier, and
weaker evidence.
"""

import threading

import pytest

from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.testing import FakeClock


def test_the_first_request_waits_for_nothing(clock: FakeClock) -> None:
    limiter = RateLimiter(2.0, clock)

    assert limiter.acquire() == 0.0
    assert clock.sleeps == []


def test_later_requests_are_spaced_by_the_interval(clock: FakeClock) -> None:
    limiter = RateLimiter(2.0, clock)

    waits = [limiter.acquire() for _ in range(4)]

    assert waits == [0.0, 2.0, 2.0, 2.0]
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_a_caller_that_arrives_late_is_not_made_to_wait(clock: FakeClock) -> None:
    limiter = RateLimiter(2.0, clock)
    limiter.acquire()

    clock.advance(10.0)

    assert limiter.acquire() == 0.0


def test_a_crawl_delay_can_slow_the_limiter_but_never_speed_it_up() -> None:
    limiter = RateLimiter(2.0, FakeClock())

    limiter.slow_to(5.0)
    assert limiter.min_interval == 5.0

    limiter.slow_to(0.5)
    assert limiter.min_interval == 5.0


def test_a_negative_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="min_interval"):
        RateLimiter(-1.0)


def test_concurrent_callers_receive_distinct_evenly_spaced_slots() -> None:
    """Eight threads at once must still leave the site two seconds apart.

    Time is frozen, so no thread's wait can be shortened by another thread's
    sleep. Eight callers must therefore come away with the eight distinct
    departure times 0, 2, 4 ... 14 — one each, none early, none doubled up.
    """
    clock = FakeClock(auto_advance=False)
    limiter = RateLimiter(2.0, clock)
    waits: list[float] = []
    waits_lock = threading.Lock()
    ready = threading.Barrier(8)

    def caller() -> None:
        ready.wait(timeout=5)
        wait = limiter.acquire()
        with waits_lock:
            waits.append(wait)

    threads = [threading.Thread(target=caller) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(waits) == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]


def test_the_gate_caps_how_many_requests_are_in_flight() -> None:
    clock = FakeClock(auto_advance=False)
    gate = PolitenessGate(RateLimiter(0.0, clock), max_concurrency=2)
    entered = threading.Semaphore(0)
    release = threading.Event()
    state = {"current": 0, "peak": 0}
    state_lock = threading.Lock()

    def caller() -> None:
        with gate.slot():
            with state_lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            entered.release()
            release.wait(timeout=5)
            with state_lock:
                state["current"] -= 1

    threads = [threading.Thread(target=caller) for _ in range(6)]
    for thread in threads:
        thread.start()
    for _ in range(2):
        assert entered.acquire(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert state["peak"] == 2


def test_a_gate_must_admit_at_least_one_caller() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        PolitenessGate(max_concurrency=0)
