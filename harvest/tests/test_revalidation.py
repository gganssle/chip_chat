"""Conditional re-harvesting: asking again without downloading again.

Issue #38's sentence is "a re-harvest refreshes, it does not re-fetch what has
not changed", and the only honest way a client can honour it is a conditional
request. So the properties asserted here are about bytes and headers, not about
intentions: what the harvester *sent*, and what it did with a 304.
"""

from datetime import UTC, datetime, timedelta

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import PermanentFetchError
from chip_chat.harvest.harvester import Harvester, conditional_headers
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.testing import EPOCH, FakeClock, FakeTransport, fake_response

URL = "https://example.test/api/menu"
ROBOTS = "https://example.test/robots.txt"
ETAG = '"v1-4a2f"'
MODIFIED = "Wed, 19 Aug 2026 09:00:00 GMT"


def harvester(
    transport: FakeTransport, clock: FakeClock, blobs: InMemoryBlobStore
) -> Harvester:
    """A harvester with its own gate, so the tests do not share a rate limit."""
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(0.0, clock), 1),
    )


def site(*responses: object) -> FakeTransport:
    """A transport whose ``robots.txt`` is a 404 and whose URL is scripted."""
    return FakeTransport(
        {
            ROBOTS: fake_response(ROBOTS, b"", status_code=404),
            URL: list(responses) if len(responses) > 1 else responses[0],
        }
    )


# --- The headers -------------------------------------------------------------


def test_both_validators_are_offered_when_both_are_known() -> None:
    assert conditional_headers(ETAG, MODIFIED) == {
        "If-None-Match": ETAG,
        "If-Modified-Since": MODIFIED,
    }


def test_nothing_is_offered_when_the_server_gave_no_validator() -> None:
    assert conditional_headers(None, None) == {}
    assert conditional_headers("", "") == {}


def test_the_cache_hands_back_what_the_server_sent(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)

    cache.put(URL, fake_response(URL, b"{}", etag=ETAG, last_modified=MODIFIED), EPOCH)

    assert cache.validators(URL) == (ETAG, MODIFIED)


def test_a_url_never_fetched_has_nothing_to_be_conditional_about(
    blobs: InMemoryBlobStore,
) -> None:
    assert DocumentCache(blobs).validators(URL) == (None, None)


# --- The round trip ----------------------------------------------------------


def test_a_refresh_sends_the_stored_validators(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    transport = site(
        fake_response(URL, b'{"v": 1}', etag=ETAG, last_modified=MODIFIED),
        fake_response(URL, b"", status_code=304),
    )
    subject = harvester(transport, clock, blobs)

    subject.fetch(URL)
    subject.fetch(URL, refresh=True)

    conditional = transport.requests[-1].headers
    assert conditional["If-None-Match"] == ETAG
    assert conditional["If-Modified-Since"] == MODIFIED


def test_a_304_keeps_the_body_and_moves_the_freshness_forward(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    transport = site(
        fake_response(URL, b'{"v": 1}', etag=ETAG),
        fake_response(URL, b"", status_code=304),
    )
    subject = harvester(transport, clock, blobs)
    first = subject.fetch(URL)
    clock.advance(7 * 24 * 60 * 60)

    second = subject.fetch(URL, refresh=True)

    assert second.content == b'{"v": 1}'
    assert second.content_sha256 == first.content_sha256
    assert second.harvested_at == first.harvested_at + timedelta(days=7)
    assert second.revalidated_at == second.harvested_at


def test_a_revalidation_costs_the_site_no_body_at_all(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    """The claim in issue #38, as two numbers rather than as a sentence."""
    transport = site(
        fake_response(URL, b"x" * 4096, etag=ETAG),
        fake_response(URL, b"", status_code=304),
    )
    subject = harvester(transport, clock, blobs)

    subject.fetch(URL)
    fetched = subject.bytes_fetched
    subject.fetch(URL, refresh=True)

    assert subject.revalidations == 1
    assert subject.bytes_fetched == fetched == 4096


def test_a_changed_page_comes_back_whole_and_leaves_the_old_body_behind(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    transport = site(
        fake_response(URL, b'{"v": 1}', etag=ETAG),
        fake_response(URL, b'{"v": 2}', etag='"v2-991b"'),
    )
    subject = harvester(transport, clock, blobs)
    first = subject.fetch(URL)

    second = subject.fetch(URL, refresh=True)

    assert second.content == b'{"v": 2}'
    assert second.previous_sha256 == first.content_sha256
    assert subject.revalidations == 0
    # The whole point of content addressing: the body we can now diff against.
    assert blobs.exists(DocumentCache(blobs).content_key(first.content_sha256))


def test_a_source_with_no_validators_is_simply_re_fetched(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    transport = site(fake_response(URL, b'{"v": 1}'))
    subject = harvester(transport, clock, blobs)

    subject.fetch(URL)
    subject.fetch(URL, refresh=True)

    assert "If-None-Match" not in transport.requests[-1].headers
    assert subject.revalidations == 0


def test_a_304_to_an_unconditional_request_is_not_treated_as_unchanged(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    """A server that answers 304 without being asked is broken, and believing
    it would freeze that document in the corpus forever."""
    transport = site(fake_response(URL, b"", status_code=304))
    subject = harvester(transport, clock, blobs)

    with pytest.raises(PermanentFetchError):
        subject.fetch(URL)


def test_a_pointer_written_before_validators_existed_still_refreshes(
    blobs: InMemoryBlobStore, clock: FakeClock
) -> None:
    """Every landing zone harvested by #19 to #22 predates these fields."""
    cache = DocumentCache(blobs)
    cache.put(URL, fake_response(URL, b'{"v": 1}'), EPOCH)
    transport = site(fake_response(URL, b'{"v": 2}'))

    document = harvester(transport, clock, blobs).fetch(URL, refresh=True)

    assert document.content == b'{"v": 2}'


def test_touch_refuses_a_naive_timestamp(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    cache.put(URL, fake_response(URL, b"{}"), EPOCH)

    with pytest.raises(ValueError, match="timezone-aware"):
        cache.touch(URL, datetime(2026, 8, 26, 12, 0, 0))


def test_touching_a_url_that_was_never_fetched_returns_nothing(
    blobs: InMemoryBlobStore,
) -> None:
    assert DocumentCache(blobs).touch(URL, datetime.now(UTC)) is None
