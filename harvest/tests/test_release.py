"""The pointer that makes a corpus atomic: one write, and never a copy."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.release import (
    Release,
    ReleaseError,
    ReleaseStore,
    read_current,
    run_id_for,
)
from chip_chat.harvest.testing import EPOCH


def release(store: ReleaseStore, run_id: str, published_at: datetime) -> Release:
    """A release object pointing at ``run_id``."""
    return Release(
        run_id=run_id,
        published_at=published_at,
        prefix=store.run_prefix(run_id),
        documents=57,
        changed=2,
        report_key=store.report_key(run_id),
    )


def test_a_run_id_sorts_chronologically_as_a_plain_string() -> None:
    """A blob store's only ordering is lexical, so the id has to carry it."""
    earlier = run_id_for(datetime(2026, 8, 26, 9, 0, tzinfo=UTC))
    later = run_id_for(datetime(2026, 9, 2, 9, 0, tzinfo=UTC))

    assert earlier == "20260826T090000Z"
    assert earlier < later


def test_a_run_id_is_in_utc_whatever_zone_it_was_started_in() -> None:
    """A fixed offset, so this does not depend on the machine's own zone."""
    east = datetime(2026, 8, 26, 19, 34, 56, tzinfo=timezone(timedelta(hours=2)))

    assert run_id_for(east) == "20260826T173456Z"


def test_a_run_id_refuses_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_id_for(datetime(2026, 8, 26, 9, 0))


def test_nothing_is_live_until_something_publishes(
    blobs: InMemoryBlobStore,
) -> None:
    assert read_current(blobs) is None


def test_publishing_makes_one_run_the_live_corpus(
    blobs: InMemoryBlobStore,
) -> None:
    store = ReleaseStore(blobs)
    store.write_record("20260826T090000Z", {"ok": True})

    store.publish(release(store, "20260826T090000Z", EPOCH))

    live = read_current(blobs)
    assert live is not None
    assert live.run_id == "20260826T090000Z"
    assert live.prefix == "corpus/runs/20260826T090000Z"
    assert live.changed == 2


def test_a_second_release_replaces_the_first_with_one_write(
    blobs: InMemoryBlobStore,
) -> None:
    store = ReleaseStore(blobs)
    for run_id in ("20260826T090000Z", "20260902T090000Z"):
        store.write_record(run_id, {"ok": True})
        store.publish(release(store, run_id, EPOCH))

    live = read_current(blobs)
    assert live is not None
    assert live.run_id == "20260902T090000Z"
    # The replaced run is still there, under its own prefix, to compare against.
    assert blobs.exists(store.record_key("20260826T090000Z"))


def test_publishing_a_run_with_no_record_is_refused(
    blobs: InMemoryBlobStore,
) -> None:
    """A pointer at a corpus that is not there is the one failure this module
    exists to prevent, so it is checked rather than assumed."""
    store = ReleaseStore(blobs)

    with pytest.raises(ReleaseError, match="does not exist"):
        store.publish(release(store, "20260826T090000Z", EPOCH))


def test_a_failed_run_leaves_a_record_and_no_pointer(
    blobs: InMemoryBlobStore,
) -> None:
    store = ReleaseStore(blobs)
    store.write_record("20260826T090000Z", {"ok": True})
    store.publish(release(store, "20260826T090000Z", EPOCH))

    store.write_record("20260902T090000Z", {"ok": False, "failure": "HTTP 503"})
    store.write_report("20260902T090000Z", "# it went wrong\n")

    live = read_current(blobs)
    assert live is not None
    assert live.run_id == "20260826T090000Z"
    assert blobs.exists(store.record_key("20260902T090000Z"))


def test_an_unreadable_pointer_is_an_error_not_an_absence(
    blobs: InMemoryBlobStore,
) -> None:
    """Answering None would let a re-harvest publish over a release it could
    not see."""
    blobs.write("corpus/current.json", b"{ this is not json")

    with pytest.raises(ReleaseError, match="not JSON"):
        read_current(blobs)


def test_a_pointer_missing_a_field_is_an_error(blobs: InMemoryBlobStore) -> None:
    blobs.write("corpus/current.json", b'{"run_id": "20260826T090000Z"}')

    with pytest.raises(ReleaseError, match="unreadable release pointer"):
        read_current(blobs)


def test_a_release_round_trips_through_its_own_serialisation() -> None:
    store = ReleaseStore(InMemoryBlobStore())
    original = release(store, "20260826T090000Z", EPOCH)

    assert Release.from_dict(original.as_dict()) == original
