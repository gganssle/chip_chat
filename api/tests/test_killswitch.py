"""The circuit breaker, and the property that makes it one.

Every test here flips a switch on a live object and asserts that the *next*
check sees it. Nothing is reconstructed, reimported or restarted in between,
because "reachable without a deploy" is the requirement and a switch that only
takes effect on the way back up does not meet it.
"""

from pathlib import Path

import pytest

from chip_chat.api.killswitch import (
    KILL_SWITCH_VARIABLE,
    CachedKillSwitch,
    EnvironmentKillSwitch,
    FileKillSwitch,
    ManualKillSwitch,
    any_of,
)
from chip_chat.api.testing import FakeClock


def test_a_manual_switch_flips_without_a_restart() -> None:
    switch = ManualKillSwitch()
    assert not switch.is_thrown()

    switch.throw()
    assert switch.is_thrown()

    switch.reset()
    assert not switch.is_thrown()


def test_an_environment_switch_is_re_read_on_every_check() -> None:
    """The mapping is mutated under the switch, exactly as a portal edit would."""
    env: dict[str, str] = {}
    switch = EnvironmentKillSwitch(env=env)
    assert not switch.is_thrown()

    env[KILL_SWITCH_VARIABLE] = "on"
    assert switch.is_thrown()

    env[KILL_SWITCH_VARIABLE] = "off"
    assert not switch.is_thrown()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "run", "  OFF  "])
def test_a_recognisably_off_value_leaves_the_app_running(value: str) -> None:
    assert not EnvironmentKillSwitch(env={KILL_SWITCH_VARIABLE: value}).is_thrown()


@pytest.mark.parametrize("value", ["1", "true", "yes", "stop", "STOP IT", "please"])
def test_anything_else_stops_it(value: str) -> None:
    """An emergency stop nobody can parse should stop, not carry on spending."""
    assert EnvironmentKillSwitch(env={KILL_SWITCH_VARIABLE: value}).is_thrown()


def test_a_file_appearing_is_the_flip(tmp_path: Path) -> None:
    stop_file = tmp_path / "stop"
    switch = FileKillSwitch(stop_file)
    assert not switch.is_thrown()

    stop_file.write_text("stop")
    assert switch.is_thrown()

    stop_file.unlink()
    assert not switch.is_thrown()


def test_a_file_can_be_disarmed_without_deleting_it(tmp_path: Path) -> None:
    stop_file = tmp_path / "stop"
    stop_file.write_text("off\n")

    assert not FileKillSwitch(stop_file).is_thrown()


def test_an_unreadable_path_is_not_a_stop(tmp_path: Path) -> None:
    """Documented and deliberate: a typo must not take the demo down forever."""
    assert not FileKillSwitch(tmp_path / "nowhere" / "stop").is_thrown()


def test_any_of_stops_when_any_one_source_says_so() -> None:
    manual = ManualKillSwitch()
    env: dict[str, str] = {}
    switch = any_of(manual, EnvironmentKillSwitch(env=env))
    assert not switch.is_thrown()

    env[KILL_SWITCH_VARIABLE] = "1"
    assert switch.is_thrown()

    env.clear()
    manual.throw()
    assert switch.is_thrown()


def test_any_of_nothing_never_stops() -> None:
    assert not any_of().is_thrown()


def test_a_cache_bounds_how_stale_an_answer_can_be(clock: FakeClock) -> None:
    inner = ManualKillSwitch()
    switch = CachedKillSwitch(inner, ttl_seconds=5.0, clock=clock)
    assert not switch.is_thrown()

    inner.throw()
    assert not switch.is_thrown(), "within the ttl the cached answer stands"

    clock.advance(5.0)
    assert switch.is_thrown()


def test_a_cache_can_be_dropped_for_an_immediate_answer(clock: FakeClock) -> None:
    inner = ManualKillSwitch()
    switch = CachedKillSwitch(inner, ttl_seconds=60.0, clock=clock)
    switch.is_thrown()

    inner.throw()
    switch.invalidate()

    assert switch.is_thrown()


def test_a_cache_with_no_lifetime_is_refused() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        CachedKillSwitch(ManualKillSwitch(), ttl_seconds=0)
