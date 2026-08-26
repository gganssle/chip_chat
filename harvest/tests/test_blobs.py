"""The blob store contract, and the key validation that keeps it inside its root."""

from pathlib import Path

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore, LocalBlobStore, validate_key


def test_local_store_round_trips_bytes(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)

    store.write("raw/blobs/sha256/ab/abcd", b"\x00\x01menu")

    assert store.read("raw/blobs/sha256/ab/abcd") == b"\x00\x01menu"
    assert store.exists("raw/blobs/sha256/ab/abcd")


def test_local_store_reports_a_missing_blob_as_none(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)

    assert store.read("raw/index/00/nothing.json") is None
    assert not store.exists("raw/index/00/nothing.json")


def test_local_store_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)

    store.write("raw/one", b"a")

    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["one"]


def test_keys_are_filtered_by_prefix_and_sorted(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.write("raw/index/b", b"b")
    store.write("raw/index/a", b"a")
    store.write("raw/blobs/c", b"c")

    assert list(store.keys("raw/index/")) == ["raw/index/a", "raw/index/b"]


def test_in_memory_store_matches_the_local_one(tmp_path: Path) -> None:
    for store in (InMemoryBlobStore(), LocalBlobStore(tmp_path)):
        store.write("raw/index/a", b"a")

        assert store.read("raw/index/a") == b"a"
        assert store.read("raw/index/missing") is None
        assert list(store.keys("raw/")) == ["raw/index/a"]


@pytest.mark.parametrize(
    "key",
    ["", "/absolute", "raw/../../etc/passwd", "raw//double", "raw/./here", "raw/a b"],
)
def test_unsafe_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="blob key"):
        validate_key(key)


def test_a_traversing_key_cannot_escape_the_store_root(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "landing")

    with pytest.raises(ValueError, match="blob key"):
        store.write("../escaped", b"nope")

    assert not (tmp_path / "escaped").exists()
