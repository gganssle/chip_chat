"""The one wrong number, refused before it reaches Snowflake.

`sql/optional/trial_credit_cap.sql` sets a credit quota on the whole account,
and a quota the monitor has already counted past suspends every warehouse in the
account the instant it is set -- in the middle of whatever conversation was
running. SQL cannot check that about itself, so :func:`chip_chat.snowflake.apply.cap`
does it first, and this holds it to that.

Nothing here talks to Snowflake. The two readings that would come from the live
account are replaced, which is the whole point: the guard has to be exercised
against the numbers that trip it, and the number that trips it is one nobody
wants to reproduce on a live trial.
"""

from pathlib import Path

import pytest

from chip_chat.snowflake import account, apply, verify


@pytest.fixture
def no_snowflake(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, dict[str, str]]]:
    """Record what would have been run, and answer both readings with nothing."""
    runs: list[tuple[Path, dict[str, str]]] = []

    def run_file(path: Path, variables: dict[str, str] | None = None) -> str:
        runs.append((path, variables or {}))
        return ""

    monkeypatch.setattr(apply.snow, "run_file", run_file)
    monkeypatch.setattr(apply, "credits_used", lambda: None)
    monkeypatch.setattr(apply, "monitor_credits_used", lambda name: None)
    return runs


@pytest.mark.parametrize("quota", [0, -1])
def test_a_quota_of_zero_or_less_is_refused(
    quota: int, no_snowflake: list[tuple[Path, dict[str, str]]]
) -> None:
    """Snowflake would accept it, and suspend the account on the next query."""
    with pytest.raises(ValueError, match="suspends the account at once"):
        apply.cap(quota)
    assert not no_snowflake, "the file ran anyway"


def test_a_quota_the_monitor_has_already_passed_is_refused(
    monkeypatch: pytest.MonkeyPatch, no_snowflake: list[tuple[Path, dict[str, str]]]
) -> None:
    """The quota counts from when the monitor was made, not from the trial's start.

    So on a re-run the number to clear is what this monitor has counted -- and a
    quota below it is not a tighter cap, it is an immediate suspension.
    """
    monkeypatch.setattr(apply, "monitor_credits_used", lambda name: 41.5)
    with pytest.raises(ValueError, match=r"already counted 41\.5 credits"):
        apply.cap(40)
    assert not no_snowflake, "the file ran anyway"


def test_a_quota_above_what_has_been_counted_applies_the_file(
    monkeypatch: pytest.MonkeyPatch, no_snowflake: list[tuple[Path, dict[str, str]]]
) -> None:
    monkeypatch.setattr(apply, "monitor_credits_used", lambda name: 41.5)
    apply.cap(60)
    assert no_snowflake == [(apply.CAP_FILE, {"trial_credit_quota": "60"})], (
        "the quota reaches the SQL as the variable the file names, or the file "
        "fails on an unsubstituted placeholder"
    )


def test_a_first_cap_needs_no_reading_at_all(
    no_snowflake: list[tuple[Path, dict[str, str]]],
) -> None:
    """An account with no monitor yet, and an ACCOUNT_USAGE view that says nothing.

    Neither is a reason to refuse to set a cap. Refusing here would mean the
    guardrail is hardest to install on exactly the account that has never had
    one.
    """
    apply.cap(130)
    assert no_snowflake == [(apply.CAP_FILE, {"trial_credit_quota": "130"})]


def test_the_monitor_the_guard_reads_is_the_one_the_file_creates() -> None:
    """Otherwise the check passes by asking about a monitor nothing ever makes."""
    assert (
        f"CREATE RESOURCE MONITOR IF NOT EXISTS {account.TRIAL_MONITOR}"
        in apply.CAP_FILE.read_text()
    )


def test_trigger_percentages_compare_as_a_set_and_not_as_a_list() -> None:
    """`SHOW RESOURCE MONITORS` does not print the triggers in ascending order.

    On 2026-08-27 the live account returned ``100%,50%,80%`` for
    ``CHIP_CHAT_SERVING_MONITOR`` -- the same three triggers
    ``05_resource_monitors.sql`` asks for, in an order nothing promises. The
    monitor check compared the parsed tuple against ``account.py``'s constant,
    which is written ascending, so it failed on a monitor that was correct.

    A trigger set is a set: two monitors that notify at the same three
    thresholds are the same guardrail whichever order they come back in. This
    is the regression test for that, and it exists because a check that fails
    on something which is not drift is worse than no check -- the next person
    reads ``100/101`` and stops reading, which is how the empty ``NOTIFY_USERS``
    beside it went unnoticed. See ``docs/cost.md`` section 14.
    """
    assert verify._percentages("100%,50%,80%") == (50, 80, 100)
    assert verify._percentages("50%,80%,100%") == (50, 80, 100)

    # And against the constant it is actually compared with, so a retune of the
    # thresholds moves this test rather than leaving it passing about nothing.
    serving = next(m for m in account.MONITORS if m.name.endswith("SERVING_MONITOR"))
    assert verify._percentages("100%,50%,80%") == serving.notify_at_percent

    # The three shapes the column arrives in when there is nothing to report.
    assert verify._percentages("") == ()
    assert verify._percentages(None) == ()
