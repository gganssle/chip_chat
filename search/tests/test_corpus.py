"""Where the chunks come from, and why the index carries the release's name."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import chunk

from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.release import Release, ReleaseStore
from chip_chat.search import corpus, schema

FIXTURE = Path(__file__).parent / "fixtures" / "chunks.jsonl"


def publish(landing: Path, run_id: str, rows: list[dict[str, object]]) -> None:
    """Stage a run and publish it, the way a re-harvest does."""
    store = LocalBlobStore(landing)
    releases = ReleaseStore(store)
    prefix = releases.run_prefix(run_id)
    store.write(
        f"{prefix}/{corpus.CHUNKS}/corpus_chunks.jsonl",
        "".join(json.dumps(row) + "\n" for row in rows).encode(),
    )
    releases.write_record(run_id, {"run_id": run_id})
    releases.publish(
        Release(
            run_id=run_id,
            published_at=datetime(2026, 8, 26, tzinfo=UTC),
            prefix=prefix,
            documents=len(rows),
            changed=0,
            report_key=releases.report_key(run_id),
        )
    )


def test_the_live_release_names_the_chunks_and_the_index(tmp_path: Path) -> None:
    # docs/corpus-freshness.md: "the index build reads the release pointer, so
    # the two swaps are the same swap."
    publish(tmp_path, "20260826T195844Z", [chunk("a" * 64), chunk("b" * 64)])
    loaded = corpus.from_release(tmp_path)
    assert loaded.run_id == "20260826T195844Z"
    assert len(loaded) == 2
    assert schema.index_name(loaded.run_id) == "corpus-20260826t195844z"


def test_a_second_release_moves_the_corpus_with_one_write(tmp_path: Path) -> None:
    publish(tmp_path, "20260826T195844Z", [chunk("a" * 64)])
    publish(tmp_path, "20260902T195844Z", [chunk("a" * 64), chunk("b" * 64)])
    loaded = corpus.from_release(tmp_path)
    assert loaded.run_id == "20260902T195844Z"
    assert len(loaded) == 2


def test_a_landing_zone_nothing_published_into_is_not_an_empty_corpus(
    tmp_path: Path,
) -> None:
    with pytest.raises(corpus.CorpusError, match="no corpus has been published"):
        corpus.from_release(tmp_path)


def test_a_release_with_no_chunk_export_is_refused(tmp_path: Path) -> None:
    # An index built from no chunks answers every question with silence and
    # swaps into place looking exactly like a success.
    publish(tmp_path, "20260826T195844Z", [])
    store = LocalBlobStore(tmp_path)
    for key in list(store.keys("corpus/runs/20260826T195844Z/chunks/")):
        (tmp_path / key).unlink()
    with pytest.raises(corpus.CorpusError, match="published no chunks"):
        corpus.from_release(tmp_path)


def test_a_line_that_is_not_json_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "chunks.jsonl"
    path.write_text('{"chunk_id": "a"}\nnot json\n')
    with pytest.raises(corpus.CorpusError, match=r":2 is not JSON"):
        corpus.from_path(path, "20260826T195844Z")


def test_a_build_read_from_a_directory_still_needs_a_release_to_name_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chunks.jsonl"
    path.write_text(json.dumps(chunk("a" * 64)) + "\n")
    with pytest.raises(corpus.CorpusError, match="becomes the index's name"):
        corpus.from_path(path, "  ")


def test_a_directory_with_no_jsonl_in_it_is_refused(tmp_path: Path) -> None:
    with pytest.raises(corpus.CorpusError, match="holds no"):
        corpus.from_path(tmp_path, "20260826T195844Z")


# --- The committed fixture ---------------------------------------------------


def test_the_fixture_corpus_is_real_published_text() -> None:
    # search/tests/fixtures/chunks.jsonl is 31 chunks rendered from the parsed
    # tables in the live landing zone -- real item names, real headings, real
    # source URLs. It stands in for the gold export until cc-2yw lands, and it
    # is what `make search-build` and `make search-verify` ran against.
    loaded = corpus.from_path(FIXTURE, "20260827T053000Z")
    assert len(loaded) == 31
    kinds = {str(row["kind"]) for row in loaded.rows}
    assert kinds == {"MENU_ITEM", "POLICY_SECTION", "FAQ_ENTRY"}
    for row in loaded.rows:
        assert str(row["source_url"]).startswith("https://")
        assert str(row["harvested_at"]).endswith("+00:00")
