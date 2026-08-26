"""A store that keeps its versions in memory, for driving a publish.

:mod:`chip_chat.eval.dataset.publish` is where #72's versioning discipline
lives, and the interesting half of it is what happens on the *second* publish --
which entries are new, which are already there, and which have been edited in
place and must be refused. None of that can be exercised against a store that
does not remember the first publish, and it should not need an Arize space, an
API key or a network to be exercised at all.

So :class:`RecordingStore` is a real implementation of
:class:`~chip_chat.eval.dataset.store.DatasetStore` rather than a stub with
``pass`` in it: it holds versions, appends to them, and refuses the two calls
the protocol says are illegal. A test can then publish twice and assert on what
the second publish did, which is the only way the rule gets checked.

It is a fixture and would be a fraud as a backend. Nothing here persists, and
nothing here is what Arize does with a row after it has one.
"""

from collections.abc import Mapping, Sequence

from chip_chat.eval.dataset.store import Row, StoreError

__all__ = ["RecordingStore", "row_by_id"]


class RecordingStore:
    """A :class:`~chip_chat.eval.dataset.store.DatasetStore` kept in a dict.

    Attributes:
        versions: Every version written, by dataset name and in write order.
            The rows of one version are the rows that publish *added*, not the
            dataset's contents at that moment -- which is the same thing the
            real store's ``add`` receives, and keeping the distinction here is
            what makes a test about it meaningful.
    """

    def __init__(self) -> None:
        self.versions: dict[str, list[tuple[Row, ...]]] = {}

    @property
    def name(self) -> str:
        """What a report calls this."""
        return "recording"

    def existing(self, dataset: str) -> tuple[Row, ...] | None:
        """Every row written to ``dataset`` so far, or ``None`` if it is new."""
        if dataset not in self.versions:
            return None
        return tuple(row for version in self.versions[dataset] for row in version)

    def create(self, dataset: str, rows: Sequence[Row]) -> str:
        """Make the dataset.

        Raises:
            StoreError: If it already exists, or ``rows`` is empty. Both are
                the store's own rules rather than a caller's mistake, and a
                store that let either through would make a publish bug look
                like a publish.
        """
        if dataset in self.versions:
            raise StoreError(f"{dataset} already exists")
        return self._write(dataset, rows)

    def add(self, dataset: str, rows: Sequence[Row]) -> str:
        """Add a version.

        Raises:
            StoreError: If the dataset does not exist, or ``rows`` is empty.
        """
        if dataset not in self.versions:
            raise StoreError(f"{dataset} does not exist")
        return self._write(dataset, rows)

    def rows(self, dataset: str) -> tuple[Row, ...]:
        """Every row in ``dataset``, for a test that wants to read them back."""
        return self.existing(dataset) or ()

    def _write(self, dataset: str, rows: Sequence[Row]) -> str:
        if not rows:
            raise StoreError("a version with no rows in it is not a version")
        written = self.versions.setdefault(dataset, [])
        written.append(tuple(dict(row) for row in rows))
        return f"{dataset}-v{len(written)}"


def row_by_id(rows: Sequence[Row]) -> Mapping[str, Row]:
    """The rows, keyed by ``entry_id``, for an assertion about one of them."""
    return {str(row["entry_id"]): row for row in rows}
