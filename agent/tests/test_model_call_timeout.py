"""The model client is constructed with a bounded call, on both auth paths.

A turn is up to :data:`chip_chat.agent.loop.DEFAULT_MAX_STEPS` model calls, so an
unbounded call is an unbounded turn. That was true from the day this module was
written and it did not show, because Container Apps ingress used to close any
response idle for sixty seconds -- the platform was accidentally acting as the
timeout nobody had configured.

``chip-901`` removed that accident for good reasons: ``_held_open`` in the API now
writes a heartbeat for as long as the turn runs, so slow turns survive. The
consequence is that the missing timeout became *less visible* rather than less
real, and a hung deployment could hold a conversation open for the better part of
an hour with the app cheerfully writing whitespace at it. That is what these
tests hold shut.

**What is not measured here.** Whether ninety seconds is the right number.
``docs/deployment.md`` §3.13 measured whole-turn latency, not single-call
latency, and nothing in this repository has measured the latter. These tests
assert that a bound exists, is passed on both construction paths, and is
configurable -- not that it is correct. ``docs/decisions/model-call-timeout.md``
is explicit about the difference.
"""

from typing import Any

import pytest

from chip_chat.agent import foundry
from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError

ENV: dict[str, str] = {
    "CHIP_CHAT_FOUNDRY_ENDPOINT": "https://example-account.openai.azure.com",
    "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-5-mini",
    "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT": "gpt-5-vision",
}
"""The three required variables, so a test can add only what it is about."""


def _config(**overrides: str) -> FoundryConfig:
    """Build a configuration from :data:`ENV` plus ``overrides``."""
    return FoundryConfig.from_env({**ENV, **overrides})


def test_a_default_configuration_bounds_the_call() -> None:
    """The default is a real bound, not the library's ten minutes."""
    config = _config()
    assert config.timeout_seconds == foundry._DEFAULT_TIMEOUT_SECONDS
    assert config.timeout_seconds < 600.0, (
        "the openai-python default is 600 seconds; a default that does not "
        "improve on it is not a bound"
    )
    assert config.max_retries < 2, "the library default is 2; state fewer or none"


def test_the_bound_reaches_both_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the api-key path and the Entra path pass timeout and max_retries.

    Two constructions in one function is exactly the shape where a keyword gets
    added to the first and forgotten on the second, and the forgotten one is the
    Entra path -- which is the one that runs in production.
    """
    seen: list[dict[str, Any]] = []

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

    import openai

    monkeypatch.setattr(openai, "AzureOpenAI", _Recorder)
    monkeypatch.setattr(
        foundry, "credential", lambda: pytest.fail("Entra path built a credential")
    )

    foundry.chat_client(_config(CHIP_CHAT_FOUNDRY_API_KEY="a-development-key"))

    monkeypatch.setattr(foundry, "credential", lambda: object())
    monkeypatch.setattr(
        "azure.identity.get_bearer_token_provider", lambda *a, **k: lambda: "token"
    )
    foundry.chat_client(_config())

    assert len(seen) == 2, "expected one api-key client and one Entra client"
    for kwargs in seen:
        assert kwargs["timeout"] == foundry._DEFAULT_TIMEOUT_SECONDS
        assert kwargs["max_retries"] == foundry._DEFAULT_MAX_RETRIES


def test_the_bound_is_configurable_without_a_code_change() -> None:
    """``SpendLimits.from_env`` set the precedent; this follows it."""
    config = _config(
        CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS="12.5",
        CHIP_CHAT_FOUNDRY_MAX_RETRIES="0",
    )
    assert config.timeout_seconds == 12.5
    assert config.max_retries == 0, "zero is meaningful: one attempt, no retry"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS", "90s"),
        ("CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS", "0"),
        ("CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS", "-1"),
        ("CHIP_CHAT_FOUNDRY_MAX_RETRIES", "lots"),
        ("CHIP_CHAT_FOUNDRY_MAX_RETRIES", "-1"),
    ],
)
def test_an_unreadable_bound_is_refused_rather_than_ignored(
    variable: str, value: str
) -> None:
    """A timeout that silently reverts to its default is the original bug again.

    Somebody who writes ``90s`` believes they have set a timeout. Falling back to
    the default would leave them believing it, which is worse than refusing to
    start.
    """
    with pytest.raises(FoundryConfigError):
        _config(**{variable: value})


def test_the_worst_case_turn_is_stated_rather_than_implied() -> None:
    """The per-call bound is not a turn budget, and the arithmetic says so.

    This test computes the worst case rather than asserting a number, so that it
    reports the real figure when either constant moves. It is here because the
    number is easy to read as a turn budget and is not one -- bounding a turn is
    a separate mechanism that has to decide what to say when it fires.
    """
    from chip_chat.agent.loop import DEFAULT_MAX_STEPS

    attempts = 1 + foundry._DEFAULT_MAX_RETRIES
    worst_case = foundry._DEFAULT_TIMEOUT_SECONDS * attempts * DEFAULT_MAX_STEPS
    assert worst_case > foundry._DEFAULT_TIMEOUT_SECONDS, (
        "a turn is several calls; the per-call bound cannot be read as a turn "
        f"bound. Worst case here is {worst_case:.0f}s across {DEFAULT_MAX_STEPS} "
        f"steps of {attempts} attempts."
    )
    assert worst_case < 3600, (
        f"worst-case turn is {worst_case:.0f}s. Before this bound existed it was "
        "unbounded in principle and 6000s in practice; if it has crept back "
        "towards an hour, the constants need revisiting rather than this "
        "assertion loosening."
    )
