"""``robots.txt`` parsing, and what happens when it cannot be read.

Everything here runs against ``tests/fixtures/robots.txt``. A test that
fetched a real site's rules would be both impolite and non-deterministic.
"""

import pytest

from chip_chat.harvest.robots import (
    RobotsPolicy,
    origin_of,
    policy_from_response,
    robots_url_for,
)
from chip_chat.harvest.transport import HttpResponse, build_user_agent

AGENT = build_user_agent("https://example.test/contact")


def test_a_disallowed_path_is_refused(robots_text: str) -> None:
    policy = RobotsPolicy.from_text(robots_text)

    assert not policy.can_fetch(AGENT, "https://example.test/private/secrets")
    assert not policy.can_fetch(AGENT, "https://example.test/order/checkout")


def test_an_allowed_path_is_permitted(robots_text: str) -> None:
    policy = RobotsPolicy.from_text(robots_text)

    assert policy.can_fetch(AGENT, "https://example.test/api/menu")
    assert policy.can_fetch(AGENT, "https://example.test/nutrition")


def test_the_declared_crawl_delay_is_reported(crawl_delay_robots_text: str) -> None:
    policy = RobotsPolicy.from_text(crawl_delay_robots_text)

    assert policy.crawl_delay(AGENT) == 5.0


def test_a_site_that_asks_for_no_delay_gets_our_default(robots_text: str) -> None:
    assert RobotsPolicy.from_text(robots_text).crawl_delay(AGENT) is None


def test_a_site_with_no_rules_permits_everything() -> None:
    assert RobotsPolicy.allow_all().can_fetch(AGENT, "https://example.test/private/x")


def test_a_deny_all_policy_permits_nothing() -> None:
    assert not RobotsPolicy.deny_all().can_fetch(AGENT, "https://example.test/")
    assert RobotsPolicy.deny_all().crawl_delay(AGENT) is None


def test_a_2xx_response_is_parsed(robots_text: str) -> None:
    policy = policy_from_response(
        HttpResponse(
            url="https://example.test/robots.txt",
            status_code=200,
            content=robots_text.encode(),
        )
    )

    assert not policy.can_fetch(AGENT, "https://example.test/private/secrets")


def test_a_404_means_the_site_published_no_rules() -> None:
    policy = policy_from_response(
        HttpResponse(url="https://example.test/robots.txt", status_code=404, content=b"")
    )

    assert policy.can_fetch(AGENT, "https://example.test/anything")


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_a_server_error_fails_closed(status_code: int) -> None:
    policy = policy_from_response(
        HttpResponse(
            url="https://example.test/robots.txt",
            status_code=status_code,
            content=b"",
        )
    )

    assert not policy.can_fetch(AGENT, "https://example.test/anything")


def test_an_unreadable_robots_txt_fails_closed() -> None:
    assert not policy_from_response(None).can_fetch(AGENT, "https://example.test/")


def test_robots_url_is_derived_from_scheme_host_and_port() -> None:
    assert (
        robots_url_for("https://example.test/a/b?c=d")
        == "https://example.test/robots.txt"
    )
    assert (
        robots_url_for("http://example.test:8080/a")
        == "http://example.test:8080/robots.txt"
    )


def test_origin_drops_the_path() -> None:
    assert origin_of("https://example.test/a/b?c=d") == "https://example.test"


@pytest.mark.parametrize("url", ["/relative/path", "example.test/menu", ""])
def test_a_relative_url_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="absolute URL"):
        robots_url_for(url)
