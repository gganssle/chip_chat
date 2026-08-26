"""The fetch-once cache: provenance, content addressing, and diffable re-harvests."""

from datetime import UTC, datetime

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache, canonical_url, digest_of
from chip_chat.harvest.errors import CacheCorruptError
from chip_chat.harvest.testing import EPOCH, fake_response

URL = "https://example.test/api/menu"


def test_every_cached_document_carries_source_url_and_harvested_at(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)

    cache.put(URL, fake_response(URL, b'{"items": []}'), EPOCH)
    document = cache.get(URL)

    assert document is not None
    assert document.source_url == URL
    assert document.harvested_at == EPOCH


def test_a_redirect_records_where_the_bytes_actually_came_from(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    final = "https://example.test/api/v2/menu"

    cache.put(URL, fake_response(final, b"{}"), EPOCH)
    document = cache.get(URL)

    assert document is not None
    assert document.requested_url == URL
    assert document.source_url == final


def test_a_missing_url_is_a_miss_not_an_error(blobs: InMemoryBlobStore) -> None:
    assert DocumentCache(blobs).get("https://example.test/never-fetched") is None


def test_the_body_is_stored_under_the_digest_of_its_own_bytes(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    body = b'{"items": [1, 2, 3]}'

    document = cache.put(URL, fake_response(URL, body), EPOCH)

    assert document.content_sha256 == digest_of(body)
    assert blobs.read(cache.content_key(digest_of(body))) == body


def test_the_body_is_stored_untouched(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    body = b'\xef\xbb\xbf{ \t"raw":  1 }\n\x00'

    cache.put(URL, fake_response(URL, body), EPOCH)
    document = cache.get(URL)

    assert document is not None
    assert document.content == body


def test_re_harvesting_unchanged_content_writes_no_new_blob(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    cache.put(URL, fake_response(URL, b"same"), EPOCH)
    before = list(blobs.keys())

    cache.put(URL, fake_response(URL, b"same"), EPOCH)

    assert list(blobs.keys()) == before


def test_changed_content_is_written_beside_the_old_body_not_over_it(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    first = cache.put(URL, fake_response(URL, b"before"), EPOCH)

    second = cache.put(URL, fake_response(URL, b"after"), EPOCH)

    assert second.content_sha256 != first.content_sha256
    assert second.previous_sha256 == first.content_sha256
    assert blobs.read(cache.content_key(first.content_sha256)) == b"before"
    assert blobs.read(cache.content_key(second.content_sha256)) == b"after"


def test_an_unchanged_re_harvest_keeps_the_last_change_on_record(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    first = cache.put(URL, fake_response(URL, b"before"), EPOCH)
    cache.put(URL, fake_response(URL, b"after"), EPOCH)

    third = cache.put(URL, fake_response(URL, b"after"), EPOCH)

    assert third.previous_sha256 == first.content_sha256


def test_the_status_code_survives_so_a_404_is_not_refetched_forever(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)

    cache.put(URL, fake_response(URL, b"", status_code=404), EPOCH)
    document = cache.get(URL)

    assert document is not None
    assert document.status_code == 404


def test_a_naive_timestamp_is_refused(blobs: InMemoryBlobStore) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DocumentCache(blobs).put(URL, fake_response(URL), datetime(2026, 1, 1))


def test_a_pointer_whose_body_vanished_is_reported_as_corruption(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    document = cache.put(URL, fake_response(URL, b"body"), EPOCH)
    blobs.write(cache.content_key(document.content_sha256), b"")

    with pytest.raises(CacheCorruptError, match="does not match digest"):
        cache.get(URL)


def test_json_bodies_are_parsed_on_demand(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)

    document = cache.put(URL, fake_response(URL, b'{"items": [7]}'), EPOCH)

    assert document.json() == {"items": [7]}
    assert document.text == '{"items": [7]}'


def test_the_cache_can_list_what_it_holds(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    cache.put(URL, fake_response(URL), EPOCH)
    cache.put("https://example.test/api/nutrition", fake_response(URL), EPOCH)

    assert sorted(cache.urls()) == [
        "https://example.test/api/menu",
        "https://example.test/api/nutrition",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.TEST/a", "https://example.test/a"),
        ("https://example.test", "https://example.test/"),
        ("https://example.test:443/a", "https://example.test/a"),
        ("http://example.test:80/a", "http://example.test/a"),
        ("https://example.test:8443/a", "https://example.test:8443/a"),
        ("https://example.test/a#section", "https://example.test/a"),
        ("https://example.test/a?b=2&a=1", "https://example.test/a?b=2&a=1"),
    ],
)
def test_urls_that_mean_the_same_thing_share_one_cache_entry(
    raw: str, expected: str
) -> None:
    assert canonical_url(raw) == expected


def test_a_fragment_does_not_split_a_cache_entry(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    cache.put(URL, fake_response(URL, b"body"), EPOCH)

    assert cache.get(f"{URL}#nutrition") is not None


def test_timestamps_round_trip_as_utc(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    harvested_at = datetime(2026, 8, 25, 19, 30, 15, 123456, tzinfo=UTC)

    cache.put(URL, fake_response(URL), harvested_at)
    document = cache.get(URL)

    assert document is not None
    assert document.harvested_at == harvested_at


def test_a_corrupt_entry_is_replaced_rather_than_refused(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    document = cache.put(URL, fake_response(URL, b"body"), EPOCH)
    blobs.write(cache.content_key(document.content_sha256), b"")

    cache.put(URL, fake_response(URL, b"fresh"), EPOCH)

    recovered = cache.get(URL)
    assert recovered is not None
    assert recovered.content == b"fresh"
