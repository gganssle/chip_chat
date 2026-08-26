"""The package surface: what the service built on top of this may import."""

from chip_chat.api import SERVICE_NAME, STOP_STATE_MESSAGE, SpendGuard, SpendLimits


def test_service_name_comes_from_the_shared_otel_package() -> None:
    assert SERVICE_NAME == "chip-chat-api"


def test_the_spend_cap_is_reachable_from_the_package_root() -> None:
    """The request path should never need to know which module a piece lives in."""
    guard = SpendGuard(SpendLimits())

    assert guard.entry_state() is None


def test_the_stop_state_copy_is_a_single_definition() -> None:
    assert STOP_STATE_MESSAGE == "Cilantro's had a busy day — come back tomorrow"
