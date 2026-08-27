"""Where the chunks come from, and why the index is named after them.

``docs/corpus-freshness.md`` §1 makes a promise on this issue's behalf: *"the
index build reads the release pointer, so the two swaps are the same swap."*
This module keeps it. A re-harvest stages everything under
``corpus/runs/<run_id>/`` and, only if the whole run succeeded, writes one small
object at ``corpus/current.json`` naming that run. This reads that pointer,
loads the chunks the run published, and hands the build the run id — which
:func:`chip_chat.search.schema.index_name` turns into the index name. So there
is one identity from the harvest to the alias:

    corpus/current.json  ──▶  20260826T195844Z  ──▶  corpus-20260826t195844z
                                                            ▲
                                              alias `corpus` points here

and two questions that would otherwise need a lookup table — *which harvest is
being served* and *which index would serve this harvest* — are answered by
reading a name.

**The chunk export.** A release publishes ``parsed/<dataset>/*.jsonl``, the
tables the harvest produced. Chunks are a layer further on: gold builds
``chip_chat.gold_harvested.corpus_chunks`` from conformed silver (#35), and a
Delta table in Unity Catalog is not something a build running on a laptop or in
Actions can read without a cluster. So the contract between the two layers is
**newline-delimited JSON under the release prefix**, one object per chunk, one
key per column of :data:`chip_chat.search.chunks.FIELDS`:

    corpus/runs/<run_id>/chunks/*.jsonl

That is the same shape, in the same place, under the same pointer as everything
else a run publishes, which is what makes the export a *step of the run* rather
than a second pipeline with its own freshness. Writing it is the gold layer's
job and is tracked separately; until it lands, ``--chunks`` reads a directory
directly and ``--run-id`` names the build, which is also how a fixture corpus is
indexed in a test.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from chip_chat.harvest.blobs import BlobStore, LocalBlobStore
from chip_chat.harvest.release import ReleaseError, ReleaseStore
from chip_chat.search.errors import SearchError

__all__ = ["CHUNKS", "ChunkSet", "CorpusError", "from_path", "from_release"]

CHUNKS: Final = "chunks"
"""The subdirectory of a release that holds the chunk export."""

_SUFFIX: Final = ".jsonl"


class CorpusError(SearchError):
    """The chunks could not be read."""


@dataclass(frozen=True, slots=True)
class ChunkSet:
    """The chunks of one corpus release, and which release they are.

    Attributes:
        run_id: The release that published them. Names the index.
        rows: The chunk rows, in the order they were read.
        origin: Where they were read from, for the build's own report.
    """

    run_id: str
    rows: tuple[Mapping[str, Any], ...]
    origin: str

    def __len__(self) -> int:
        return len(self.rows)


def _parse(raw: bytes, origin: str) -> Iterator[Mapping[str, Any]]:
    """Yield the JSON objects of one NDJSON blob."""
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as error:
            raise CorpusError(f"{origin}:{number} is not JSON: {error}") from error
        if not isinstance(decoded, dict):
            raise CorpusError(f"{origin}:{number} is not an object")
        yield decoded


def _read(store: BlobStore, keys: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
    """Read and parse every NDJSON blob at ``keys``, in the order given."""
    rows: list[Mapping[str, Any]] = []
    for key in keys:
        raw = store.read(key)
        if raw is None:
            raise CorpusError(f"{key} disappeared between listing it and reading it")
        rows.extend(_parse(raw, key))
    return tuple(rows)


def from_release(landing: Path, prefix: str = "corpus") -> ChunkSet:
    """Return the chunks of the live corpus release.

    Args:
        landing: The landing zone root.
        prefix: Key prefix the releases live under.

    Returns:
        The chunk set, named by the live release's run id.

    Raises:
        CorpusError: If nothing has been published, if the pointer is
            unreadable, or if the release published no chunk export. The last
            of those is a refusal rather than an empty build: an index built
            from no chunks is an index that answers every question with
            silence, and it would swap into place looking exactly like a
            success.
    """
    store = LocalBlobStore(landing)
    releases = ReleaseStore(store, prefix)
    try:
        release = releases.current()
    except ReleaseError as error:
        raise CorpusError(str(error)) from error
    if release is None:
        raise CorpusError(
            f"no corpus has been published under {landing}/{prefix}. "
            f"`make reharvest` publishes one."
        )
    root = f"{release.prefix}/{CHUNKS}/"
    keys = [key for key in store.keys(root) if key.endswith(_SUFFIX)]
    if not keys:
        raise CorpusError(
            f"release {release.run_id} published no chunks under {root}. "
            f"The gold layer writes that export; see chip_chat.search.corpus."
        )
    return ChunkSet(
        run_id=release.run_id, rows=_read(store, keys), origin=f"{landing}/{root}"
    )


def from_path(path: Path, run_id: str) -> ChunkSet:
    """Return the chunks in a file or directory, named by ``run_id``.

    The escape hatch, and the one a test uses. ``run_id`` is required rather
    than derived from the path because the index name is the corpus's identity
    and a directory name is not one — an index called ``corpus-tmp`` tells the
    next person nothing about which harvest it holds.

    Args:
        path: A ``.jsonl`` file, or a directory of them.
        run_id: What to name the build after.

    Returns:
        The chunk set.

    Raises:
        CorpusError: If the path holds no NDJSON, or ``run_id`` is empty.
    """
    if not run_id.strip():
        raise CorpusError("a build needs a run id: it becomes the index's name")
    if path.is_dir():
        files = sorted(path.glob(f"*{_SUFFIX}"))
    elif path.is_file():
        files = [path]
    else:
        raise CorpusError(f"{path} does not exist")
    if not files:
        raise CorpusError(f"{path} holds no {_SUFFIX} files")
    rows: list[Mapping[str, Any]] = []
    for file in files:
        rows.extend(_parse(file.read_bytes(), str(file)))
    return ChunkSet(run_id=run_id.strip(), rows=tuple(rows), origin=str(path))
