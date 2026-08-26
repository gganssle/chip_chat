"""Upload flooding: many uploads from one session, and from one address.

The counters in ``test_source_ratelimit.py`` are sized for typing. These are
sized for what an upload actually costs -- a Content Safety call, a blob write
with a retention obligation, and a vision completion -- which makes flooding a
cost attack before it is a storage one, and makes the ceiling a different number
rather than the same number applied twice.

Two of these tests are about what the refusal *does not* do. A refusal that
extended the window would be a ban rather than a rate, and a refusal that
consumed the other scope's allowance would let an attacker exhaust an address by
exhausting a session. Both are the kind of bug that passes every test written
about the happy path.
"""

from collections.abc import Callable

import pytest

from chip_chat.api import (
    STOP_STATE_MESSAGE,
    BudgetScope,
    SpendGuard,
    SpendLimits,
    StopReason,
    UploadLimiter,
)
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.testing import FakeClock
from chip_chat.otel import chat_turn
from chip_chat.otel.attributes import ChipChatAttributes, GuardOutcome
from chip_chat.otel.testing import SpanRecorder, span_recorder

SOURCE = "203.0.113.7"


@pytest.fixture
def upload_limits() -> SpendLimits:
    """Small upload ceilings, so a test trips them for real."""
    return SpendLimits(
        session_uploads_per_window=2,
        source_uploads_per_window=3,
        upload_window_seconds=60.0,
        upload_token_charge=1_000,
        session_token_cap=6_000,
        daily_token_ceiling=10_000,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def limiter(upload_limits: SpendLimits, clock: FakeClock) -> UploadLimiter:
    return UploadLimiter(upload_limits, clock)


# --- the two ceilings ------------------------------------------------------


def test_a_session_uploading_in_a_loop_is_refused(limiter: UploadLimiter) -> None:
    for _ in range(2):
        assert limiter.check(session_id="s", source_address=SOURCE) is None

    stop = limiter.check(session_id="s", source_address=SOURCE)

    assert stop is not None
    assert stop.reason is StopReason.UPLOAD_RATE_LIMIT
    assert stop.usage.scope is BudgetScope.SESSION_UPLOADS


def test_a_fresh_session_per_upload_still_runs_into_the_address(
    limiter: UploadLimiter,
) -> None:
    # Sessions are free to mint, which is the whole reason there is a second
    # ceiling. Three uploads, three sessions, one address.
    for index in range(3):
        assert limiter.check(session_id=f"s{index}", source_address=SOURCE) is None

    stop = limiter.check(session_id="s99", source_address=SOURCE)

    assert stop is not None
    assert stop.usage.scope is BudgetScope.SOURCE_UPLOADS
    assert stop.usage.used == 3


def test_one_address_running_out_does_not_touch_another(
    limiter: UploadLimiter,
) -> None:
    for index in range(3):
        limiter.check(session_id=f"s{index}", source_address=SOURCE)

    assert limiter.check(session_id="other", source_address="198.51.100.4") is None


def test_the_window_slides_rather_than_resetting(
    limiter: UploadLimiter, clock: FakeClock
) -> None:
    for _ in range(2):
        limiter.check(session_id="s", source_address=SOURCE)
    assert limiter.check(session_id="s", source_address=SOURCE) is not None

    clock.advance(61.0)

    assert limiter.check(session_id="s", source_address=SOURCE) is None


# --- what a refusal must not do -------------------------------------------


def test_a_refusal_does_not_extend_the_window(
    limiter: UploadLimiter, clock: FakeClock
) -> None:
    # Otherwise the limit becomes a ban: a client that keeps hammering after
    # being refused would never be welcome again, however long it waited.
    for _ in range(2):
        limiter.check(session_id="s", source_address=SOURCE)
    clock.advance(30.0)
    for _ in range(5):
        assert limiter.check(session_id="s", source_address=SOURCE) is not None

    clock.advance(31.0)

    assert limiter.check(session_id="s", source_address=SOURCE) is None


def test_a_session_refusal_does_not_spend_the_addresss_allowance(
    limiter: UploadLimiter,
) -> None:
    # The address ceiling is three and the session ceiling is two, so the third
    # upload from one session is refused by the session. If that refusal had
    # been charged to the address, a fresh session would find only one left.
    for _ in range(3):
        limiter.check(session_id="s", source_address=SOURCE)
    assert limiter.address_usage(SOURCE).used == 2

    assert limiter.check(session_id="fresh", source_address=SOURCE) is None


def test_neither_refusal_tells_the_uploader_which_ceiling_it_was(
    limiter: UploadLimiter,
) -> None:
    # Told "your session is out", an uploader mints a session. Told "your
    # address is out", they reach for a proxy. Told the designed stop state,
    # they learn nothing -- and the scope is on the span, for the operator.
    for _ in range(2):
        limiter.check(session_id="s", source_address=SOURCE)
    by_session = limiter.check(session_id="s", source_address=SOURCE)
    for index in range(3):
        limiter.check(session_id=f"other{index}", source_address="198.51.100.9")
    by_address = limiter.check(session_id="new", source_address="198.51.100.9")

    assert by_session is not None
    assert by_address is not None
    assert by_session.reason is by_address.reason is StopReason.UPLOAD_RATE_LIMIT
    assert by_session.message == by_address.message == STOP_STATE_MESSAGE
    assert by_session.usage.scope is not by_address.usage.scope


# --- through the guard -----------------------------------------------------


def _guard(
    limits: SpendLimits,
    clock: FakeClock,
    kill_switch: ManualKillSwitch | None = None,
) -> SpendGuard:
    return SpendGuard(limits, clock=clock, kill_switch=kill_switch)


def test_the_guard_refuses_an_upload_before_anything_is_read(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    guard = _guard(upload_limits, clock)
    with chat_turn(session_id="s", turn_index=0, message=""):
        for _ in range(2):
            assert guard.upload(session_id="s", source_address=SOURCE) is None
        stop = guard.upload(session_id="s", source_address=SOURCE)

    assert stop is not None
    assert stop.message == STOP_STATE_MESSAGE


def test_the_kill_switch_beats_the_upload_ceilings(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    breaker = ManualKillSwitch()
    guard = _guard(upload_limits, clock, breaker)
    breaker.throw()

    with chat_turn(session_id="s", turn_index=0, message=""):
        stop = guard.upload(session_id="s", source_address=SOURCE)

    assert stop is not None
    assert stop.reason is StopReason.KILL_SWITCH
    # And it cost nothing: a thrown breaker must not consume an allowance the
    # visitor will want when it is put back.
    assert guard.upload_limiter.session_usage("s").used == 0


def test_an_upload_decision_lands_on_the_guard_span(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    guard = _guard(upload_limits, clock)

    with (
        span_recorder("api") as spans,
        chat_turn(session_id="s", turn_index=0, message=""),
    ):
        for _ in range(3):
            guard.upload(session_id="s", source_address=SOURCE)
        _assert_blocked(spans)


def _assert_blocked(spans: SpanRecorder) -> None:
    """The last ``guard.budget_check`` recorded a block, with the scope in metadata."""
    checks = [
        span for span in spans.finished_spans() if span.name == "guard.budget_check"
    ]
    assert len(checks) == 3
    attributes = dict(checks[-1].attributes or {})
    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.BLOCKED
    assert attributes[ChipChatAttributes.GUARD_REASON] == StopReason.UPLOAD_RATE_LIMIT
    # Uploads are counted in uploads, not in tokens. Putting them on the token
    # attributes would make every budget dashboard read a photograph as spend.
    assert ChipChatAttributes.BUDGET_TOKENS_USED not in attributes
    assert BudgetScope.SESSION_UPLOADS.value in str(attributes["metadata"])


def test_an_upload_still_costs_a_turn_against_the_ordinary_rate_limit(
    clock: FakeClock,
) -> None:
    # The upload ceilings are *underneath* the existing layers, not instead of
    # them. A turn that carries a photograph is still a turn, so it spends a
    # source-address request like every other one -- otherwise uploading would
    # be the cheap way to get around the limit that exists to stop flooding.
    limits = SpendLimits(
        source_requests_per_window=2,
        session_uploads_per_window=50,
        source_uploads_per_window=50,
    )
    guard = _guard(limits, clock)

    for index in range(2):
        with (
            chat_turn(session_id="s", turn_index=index, message=""),
            guard.turn(session_id="s", source_address=SOURCE) as budget,
        ):
            assert budget.allowed
            assert guard.upload(session_id="s", source_address=SOURCE) is None
            budget.record_upload()

    with (
        chat_turn(session_id="s", turn_index=2, message=""),
        guard.turn(session_id="s", source_address=SOURCE) as third,
    ):
        assert not third.allowed
        assert third.stop is not None
        assert third.stop.reason is StopReason.SOURCE_RATE_LIMIT


# --- uploads count against the budget --------------------------------------


def test_an_accepted_upload_costs_the_turn_its_vision_call(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    # The rate limit bounds how often; this bounds how much. An upload is a
    # vision call that has already been committed to, so it is charged when it
    # is accepted rather than when the model finally answers.
    guard = _guard(upload_limits, clock)
    with (
        chat_turn(session_id="s", turn_index=0, message=""),
        guard.turn(session_id="s", source_address=SOURCE) as budget,
    ):
        budget.record_upload()

    assert guard.ledger.session_tokens("s") == upload_limits.upload_token_charge


def test_the_model_s_real_cost_is_added_to_the_upload_s_charge(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    guard = _guard(upload_limits, clock)
    with (
        chat_turn(session_id="s", turn_index=0, message=""),
        guard.turn(session_id="s", source_address=SOURCE) as budget,
    ):
        budget.record_upload()
        budget.record_usage(prompt_tokens=400, completion_tokens=100)

    assert guard.ledger.session_tokens("s") == upload_limits.upload_token_charge + 500


def test_a_refused_upload_charges_nothing(
    upload_limits: SpendLimits, clock: FakeClock
) -> None:
    guard = _guard(upload_limits, clock)
    with (
        chat_turn(session_id="s", turn_index=0, message=""),
        guard.turn(session_id="s", source_address=SOURCE) as budget,
    ):
        budget.record_upload(0)

    assert guard.ledger.session_tokens("s") == 0


def test_uploads_drive_a_session_into_its_token_cap(clock: FakeClock) -> None:
    # Flooding as a cost attack, all the way through: enough uploads and the
    # session runs out of budget, not merely out of upload allowance.
    limits = SpendLimits(
        session_uploads_per_window=100,
        source_uploads_per_window=100,
        upload_token_charge=2_000,
        session_token_cap=5_000,
        daily_token_ceiling=1_000_000,
        turn_token_reservation=1_000,
    )
    guard = _guard(limits, clock)

    stopped = None
    for index in range(6):
        with (
            chat_turn(session_id="s", turn_index=index, message=""),
            guard.turn(session_id="s", source_address=SOURCE) as budget,
        ):
            if not budget.allowed:
                stopped = budget.stop
                break
            budget.record_upload()

    assert stopped is not None
    assert stopped.reason is StopReason.SESSION_TOKEN_CAP


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "build"),
    [
        (
            "session_uploads_per_window",
            lambda: SpendLimits(session_uploads_per_window=0),
        ),
        ("source_uploads_per_window", lambda: SpendLimits(source_uploads_per_window=-1)),
        ("upload_window_seconds", lambda: SpendLimits(upload_window_seconds=0)),
        ("upload_token_charge", lambda: SpendLimits(upload_token_charge=0)),
    ],
)
def test_an_upload_ceiling_that_would_not_bound_anything_is_refused(
    name: str, build: Callable[[], SpendLimits]
) -> None:
    with pytest.raises(ValueError, match=name):
        build()


def test_the_environment_overrides_every_upload_ceiling() -> None:
    limits = SpendLimits.from_env(
        {
            "CHIP_CHAT_SESSION_UPLOADS_PER_WINDOW": "3",
            "CHIP_CHAT_SOURCE_UPLOADS_PER_WINDOW": "7",
            "CHIP_CHAT_UPLOAD_WINDOW_SECONDS": "120",
            "CHIP_CHAT_UPLOAD_TOKEN_CHARGE": "900",
        }
    )
    assert limits.session_uploads_per_window == 3
    assert limits.source_uploads_per_window == 7
    assert limits.upload_window_seconds == 120.0
    assert limits.upload_token_charge == 900


def test_an_empty_upload_variable_means_absent_rather_than_zero() -> None:
    assert (
        SpendLimits.from_env({"CHIP_CHAT_SOURCE_UPLOADS_PER_WINDOW": "  "})
        == SpendLimits()
    )
