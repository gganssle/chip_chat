"""The framework end to end: refuse, cache, wait, retry, and never fetch twice.

Every test here drives a :class:`~chip_chat.harvest.testing.FakeTransport`, so
"made no network calls" is a fact the test reads off a list rather than an
aspiration in a docstring.
"""

import threading

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import digest_of
from chip_chat.harvest.errors import (
    PermanentFetchError,
    RobotsDisallowedError,
    TransientFetchError,
)
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response
from chip_chat.harvest.transport import HttpResponse

ROBOTS = "https://example.test/robots.txt"
MENU = "https://example.test/api/menu"
NUTRITION = "https://example.test/api/nutrition"
FORBIDDEN = "https://example.test/private/prices"
CONTACT = "https://example.test/contact"


def build(
    transport: FakeTransport,
    blobs: InMemoryBlobStore,
    clock: FakeClock | None = None,
    *,
    interval: float = 2.0,
    max_concurrency: int = 1,
) -> Harvester:
    """Build a harvester on its own gate, so no test can disturb another."""
    clock = clock if clock is not None else FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact=CONTACT,
        gate=PolitenessGate(RateLimiter(interval, clock), max_concurrency),
    )


def transport_for(
    robots_text: str, extra: dict[str, object] | None = None
) -> FakeTransport:
    """A transport serving the fixture ``robots.txt`` and the menu endpoint."""
    responses: dict[str, object] = {
        ROBOTS: fake_response(ROBOTS, robots_text.encode(), content_type="text/plain"),
        MENU: fake_response(MENU, b'{"items": ["burrito"]}'),
    }
    responses.update(extra or {})
    return FakeTransport(responses)


def test_robots_txt_is_read_before_anything_else(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text)

    build(transport, blobs).fetch(MENU)

    assert transport.urls == [ROBOTS, MENU]


def test_a_disallowed_path_is_refused_and_never_fetched(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text)
    harvester = build(transport, blobs)

    with pytest.raises(RobotsDisallowedError):
        harvester.fetch(FORBIDDEN)

    assert transport.urls == [ROBOTS]
    assert not harvester.is_allowed(FORBIDDEN)
    assert harvester.cache.get(FORBIDDEN) is None


def test_a_warm_cache_makes_zero_network_calls(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    """The acceptance criterion this whole package exists for.

    The second run is a fresh harvester over the same landing zone — a new
    process, in effect — and it must not touch the site at all, not even for
    ``robots.txt``.
    """
    cold_transport = transport_for(robots_text)
    build(cold_transport, blobs).fetch(MENU)
    assert cold_transport.urls == [ROBOTS, MENU]

    warm_transport = transport_for(robots_text)
    warm_harvester = build(warm_transport, blobs)
    document = warm_harvester.fetch(MENU)

    assert warm_transport.requests == []
    assert warm_harvester.requests_made == 0
    assert document.json() == {"items": ["burrito"]}


def test_a_warm_cache_still_refuses_a_disallowed_path(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    build(transport_for(robots_text), blobs).fetch(MENU)

    warm_transport = transport_for(robots_text)
    with pytest.raises(RobotsDisallowedError):
        build(warm_transport, blobs).fetch(FORBIDDEN)

    assert warm_transport.requests == []


def test_stale_robots_rules_are_read_again(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    clock = FakeClock()
    build(transport_for(robots_text), blobs, clock).fetch(MENU)
    clock.advance(25 * 60 * 60)

    later_transport = transport_for(robots_text)
    build(later_transport, blobs, clock).fetch(MENU)

    assert later_transport.urls == [ROBOTS]


def test_the_user_agent_says_who_we_are_and_how_to_complain(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text)

    build(transport, blobs).fetch(MENU)

    sent = transport.requests[-1].headers["User-Agent"]
    assert sent.startswith("chip-chat-harvest/")
    assert CONTACT in sent


def test_the_user_agent_cannot_be_overridden_by_a_caller(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text)

    build(transport, blobs).fetch(MENU, headers={"user-agent": "definitely-a-browser"})

    headers = transport.requests[-1].headers
    assert headers["User-Agent"].startswith("chip-chat-harvest/")
    assert "definitely-a-browser" not in str(headers)


def test_requests_are_spaced_by_the_rate_limiter(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    clock = FakeClock()
    transport = transport_for(robots_text, {NUTRITION: fake_response(NUTRITION)})
    harvester = build(transport, blobs, clock)

    harvester.fetch(MENU)
    harvester.fetch(NUTRITION)

    assert clock.sleeps == [2.0, 2.0]


def test_a_declared_crawl_delay_slows_us_down(
    crawl_delay_robots_text: str, blobs: InMemoryBlobStore
) -> None:
    clock = FakeClock()
    gate = PolitenessGate(RateLimiter(2.0, clock), max_concurrency=1)
    harvester = Harvester(
        blobs,
        transport_for(crawl_delay_robots_text),
        clock=clock,
        contact=CONTACT,
        gate=gate,
    )

    harvester.fetch(MENU)

    assert gate.limiter.min_interval == 5.0


def test_the_json_accept_header_is_sent_for_json_endpoints(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text)

    payload = build(transport, blobs).fetch_json(MENU)

    assert payload == {"items": ["burrito"]}
    assert "application/json" in transport.requests[-1].headers["Accept"]


def test_a_transient_failure_is_retried_with_growing_backoff(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    clock = FakeClock()
    transport = transport_for(
        robots_text,
        {
            MENU: [
                fake_response(MENU, b"", status_code=503),
                fake_response(MENU, b"", status_code=503),
                fake_response(MENU, b'{"items": []}'),
            ]
        },
    )

    document = build(transport, blobs, clock, interval=0.0).fetch(MENU)

    assert document.status_code == 200
    assert transport.urls.count(MENU) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_a_retry_after_header_is_honoured(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    clock = FakeClock()
    throttled = HttpResponse(
        url=MENU, status_code=429, content=b"", headers={"retry-after": "17"}
    )
    transport = transport_for(
        robots_text, {MENU: [throttled, fake_response(MENU, b"{}")]}
    )

    build(transport, blobs, clock, interval=0.0).fetch(MENU)

    assert clock.sleeps == [17.0]


def test_a_connection_failure_is_retried_then_given_up_on(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text, {MENU: OSError("connection reset")})
    harvester = build(transport, blobs, interval=0.0)

    with pytest.raises(TransientFetchError, match="connection reset"):
        harvester.fetch(MENU)

    assert transport.urls.count(MENU) == 3


def test_a_4xx_is_never_retried(robots_text: str, blobs: InMemoryBlobStore) -> None:
    transport = transport_for(
        robots_text, {MENU: fake_response(MENU, b"", status_code=404)}
    )

    with pytest.raises(PermanentFetchError, match="HTTP 404"):
        build(transport, blobs, interval=0.0).fetch(MENU)

    assert transport.urls.count(MENU) == 1


def test_a_site_with_no_robots_txt_is_still_harvested(blobs: InMemoryBlobStore) -> None:
    transport = FakeTransport(
        {
            ROBOTS: fake_response(ROBOTS, b"", status_code=404),
            MENU: fake_response(MENU, b"{}"),
        }
    )

    assert build(transport, blobs).fetch(MENU).status_code == 200


def test_an_unreadable_robots_txt_stops_the_harvest(blobs: InMemoryBlobStore) -> None:
    transport = FakeTransport(
        {
            ROBOTS: fake_response(ROBOTS, b"", status_code=503),
            MENU: fake_response(MENU, b"{}"),
        }
    )
    harvester = build(transport, blobs, interval=0.0)

    with pytest.raises(RobotsDisallowedError):
        harvester.fetch(MENU)

    assert MENU not in transport.urls


def test_robots_txt_is_read_once_per_origin(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(robots_text, {NUTRITION: fake_response(NUTRITION)})
    harvester = build(transport, blobs)

    harvester.fetch(MENU)
    harvester.fetch(NUTRITION)

    assert transport.urls.count(ROBOTS) == 1


def test_a_refresh_diffs_against_what_was_there_before(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    transport = transport_for(
        robots_text,
        {MENU: [fake_response(MENU, b"old"), fake_response(MENU, b"new")]},
    )
    harvester = build(transport, blobs)

    first = harvester.fetch(MENU)
    second = harvester.fetch(MENU, refresh=True)

    assert first.content == b"old"
    assert second.content == b"new"
    assert second.previous_sha256 == digest_of(b"old")


def test_closing_the_harvester_closes_its_transport(blobs: InMemoryBlobStore) -> None:
    transport = FakeTransport()

    with build(transport, blobs):
        pass

    assert transport.closed


def test_parallel_harvesting_still_spaces_requests(
    robots_text: str, blobs: InMemoryBlobStore
) -> None:
    """Four threads through one shared gate still leave two seconds between calls.

    Time is frozen, so no thread's wait can be shortened by another's sleep.
    The four recorded waits are the four distinct slots the limiter handed
    out: none early, none doubled up, all four URLs fetched.
    """
    clock = FakeClock(auto_advance=False)
    urls = [f"https://example.test/api/item/{index}" for index in range(4)]
    transport = transport_for(robots_text, {url: fake_response(url) for url in urls})
    harvester = build(transport, blobs, clock, max_concurrency=4)
    harvester.is_allowed(urls[0])  # Read robots.txt once, before the threads start.
    clock.sleeps.clear()
    ready = threading.Barrier(4)

    def worker(url: str) -> None:
        ready.wait(timeout=5)
        harvester.fetch(url)

    threads = [threading.Thread(target=worker, args=(url,)) for url in urls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(clock.sleeps) == [2.0, 4.0, 6.0, 8.0]
    assert sorted(transport.urls[1:]) == urls
