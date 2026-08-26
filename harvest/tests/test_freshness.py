"""Corpus freshness: the stalest document, and what makes a run fail on it."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chip_chat.harvest.__main__ import main
from chip_chat.harvest.blobs import InMemoryBlobStore, LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.freshness import (
    DEFAULT_MAX_AGE_DAYS,
    CorpusFreshness,
    read_freshness,
)
from chip_chat.harvest.release import Release, ReleaseStore
from chip_chat.harvest.testing import EPOCH, fake_response

WEEK = timedelta(days=7)


def seed(cache: DocumentCache, url: str, at: datetime, status: int = 200) -> None:
    """Put one document in the cache at a chosen instant."""
    cache.put(url, fake_response(url, url.encode(), status_code=status), at)


def test_the_corpus_is_as_fresh_as_its_stalest_document(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)
    seed(cache, "https://example.test/b", EPOCH + WEEK)

    freshness = read_freshness(cache, now=EPOCH + WEEK)

    assert freshness.document_count == 2
    assert freshness.oldest is not None
    assert freshness.oldest.url == "https://example.test/a"
    assert freshness.max_age == WEEK
    assert freshness.newest is not None
    assert freshness.newest.url == "https://example.test/b"


def test_robots_txt_is_not_corpus_and_cannot_flatter_the_number(
    blobs: InMemoryBlobStore,
) -> None:
    """The framework re-reads it daily, so counting it would mean a corpus
    nobody has harvested for a year still looks a day old."""
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)
    seed(cache, "https://example.test/robots.txt", EPOCH + WEEK)

    freshness = read_freshness(cache, now=EPOCH + WEEK)

    assert freshness.document_count == 1
    assert freshness.oldest is not None
    assert freshness.newest is not None
    assert freshness.oldest.url == freshness.newest.url == "https://example.test/a"


def test_a_cached_404_is_a_record_of_an_absence_not_a_document(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)
    seed(cache, "https://example.test/gone", EPOCH, status=404)

    assert read_freshness(cache, now=EPOCH).document_count == 1


def test_a_corpus_inside_the_threshold_is_fresh(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)

    freshness = read_freshness(cache, now=EPOCH + timedelta(days=6))

    assert not freshness.is_stale(timedelta(days=DEFAULT_MAX_AGE_DAYS))


def test_a_corpus_past_the_threshold_is_stale(blobs: InMemoryBlobStore) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)

    freshness = read_freshness(cache, now=EPOCH + timedelta(days=9))

    assert freshness.is_stale(timedelta(days=DEFAULT_MAX_AGE_DAYS))


def test_an_empty_corpus_is_stale_rather_than_perfect(
    blobs: InMemoryBlobStore,
) -> None:
    """The case the check exists to catch: a machine where nothing ever ran."""
    freshness = read_freshness(DocumentCache(blobs), now=EPOCH)

    assert freshness.document_count == 0
    assert freshness.max_age is None
    assert freshness.is_stale(timedelta(days=DEFAULT_MAX_AGE_DAYS))


def test_the_release_supplies_the_two_numbers_the_cache_cannot(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)
    release = Release(
        run_id="20260826T120000Z",
        published_at=EPOCH,
        prefix="corpus/runs/20260826T120000Z",
        documents=1,
        changed=3,
        report_key="corpus/runs/20260826T120000Z/change-report.md",
    )

    freshness = read_freshness(cache, now=EPOCH, release=release)

    payload = freshness.as_dict()
    assert payload["last_successful_harvest"] == EPOCH.isoformat()
    assert payload["changed_last_release"] == 3


def test_a_harvested_corpus_with_no_release_says_so(
    blobs: InMemoryBlobStore,
) -> None:
    """Documents were fetched; the run that would have published them did not
    finish. Reporting that as "never harvested" would be wrong twice."""
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/a", EPOCH)

    freshness = read_freshness(cache, now=EPOCH)

    assert freshness.document_count == 1
    assert freshness.changed_last_release is None
    assert "last release none" in freshness.render()


def test_read_freshness_refuses_a_naive_instant(blobs: InMemoryBlobStore) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        read_freshness(DocumentCache(blobs), now=datetime(2026, 8, 26))


def test_the_rendered_block_names_the_document_to_go_and_fix(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    seed(cache, "https://example.test/nutrition", EPOCH)

    rendered = read_freshness(cache, now=EPOCH + timedelta(days=41)).render(
        timedelta(days=DEFAULT_MAX_AGE_DAYS)
    )

    assert "https://example.test/nutrition" in rendered
    assert "41.0 days old" in rendered
    assert "STALE" in rendered


# --- The command -------------------------------------------------------------


def test_the_command_reports_without_a_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = DocumentCache(LocalBlobStore(tmp_path))
    seed(cache, "https://example.test/a", datetime.now(UTC) - timedelta(days=400))

    assert main(["--landing", str(tmp_path)]) == 0
    assert "Corpus freshness" in capsys.readouterr().out


def test_the_command_fails_on_a_stale_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enforcement, not a dial. This is what turns the weekly job red."""
    cache = DocumentCache(LocalBlobStore(tmp_path))
    seed(cache, "https://example.test/a", datetime.now(UTC) - timedelta(days=400))

    status = main(["--landing", str(tmp_path), "--max-age-days", "8"])

    assert status == 1
    assert "corpus is stale" in capsys.readouterr().err


def test_the_command_emits_json_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blobs = LocalBlobStore(tmp_path)
    seed(DocumentCache(blobs), "https://example.test/a", datetime.now(UTC))
    ReleaseStore(blobs).write_record("20260826T120000Z", {"ok": True})
    ReleaseStore(blobs).publish(
        Release(
            run_id="20260826T120000Z",
            published_at=datetime.now(UTC),
            prefix="corpus/runs/20260826T120000Z",
            documents=1,
            changed=0,
            report_key="corpus/runs/20260826T120000Z/change-report.md",
        )
    )

    assert main(["--landing", str(tmp_path), "--json", "--max-age-days", "8"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["document_count"] == 1
    assert payload["last_release_id"] == "20260826T120000Z"
    assert payload["changed_last_release"] == 0


def test_the_measurement_carries_its_own_instant() -> None:
    """A report read a week later is still a report about when it was taken."""
    freshness = CorpusFreshness(
        measured_at=EPOCH, document_count=0, oldest=None, newest=None
    )

    assert freshness.as_dict()["measured_at"] == EPOCH.isoformat()
