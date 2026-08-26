"""Where a dataset version goes, and the three things that can be done to it.

The seam is :class:`DatasetStore` and it is three methods wide. What is *not*
in it is the load-bearing part: there is no operation that replaces an example.
#72's versioning discipline -- *adding entries creates a new version rather than
mutating the old one* -- is therefore not a rule this package follows, it is a
rule this package cannot break, because the call that would break it does not
exist on the far side of the seam. The Arize SDK offers such a call
(``update_examples``); the adapter below declines to wrap it, and
:mod:`chip_chat.eval.dataset.publish` refuses a build that would have needed it.

Two methods where one would do, for the same reason. ``create`` and ``add`` are
different operations on the backend -- the first makes a dataset, the second
makes a version of one -- and an adapter that guessed between them from whether
a call had failed would be a place for a first upload to quietly become an
overwrite.

The Arize SDK is imported inside the methods rather than at module scope, and it
is not in the lockfile at all. It drags in pandas, pyarrow and a generated REST
client, and it constrains protobuf hard enough to pull the whole workspace's
pin backwards -- a real cost to pay on every install for a command CI never
runs. ``eval/pyproject.toml`` carries the argument and
``eval/dataset/README.md`` carries the ``uv run --with arize`` invocation that
supplies it for the one command that needs it. ``chip_chat.otel.exporters``
makes the same lazy-import move for the smaller half of the same reason.
"""

import os
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

__all__ = [
    "API_KEY_VARIABLE",
    "SPACE_VARIABLE",
    "ArizeDatasetStore",
    "DatasetStore",
    "StoreError",
    "arize_store_from_env",
]

Row = Mapping[str, str | int | bool]
"""One dataset row: flat, and every value a scalar a table can hold."""

SPACE_VARIABLE: Final = "ARIZE_SPACE_ID"
"""Which Arize space the dataset lives in. A name works too; an id is safer."""

API_KEY_VARIABLE: Final = "ARIZE_API_KEY"
"""Read by the SDK itself. Named here only so an error can say what is missing."""


class StoreError(RuntimeError):
    """A store that could not be reached, or was not configured to be."""


class DatasetStore(Protocol):
    """Somewhere a versioned dataset can be kept.

    Three methods. Anything that can say what a dataset already holds, make a
    new one, and add a version to an existing one is a store: Arize AX, a
    directory of JSON files, or a recorder in a test.
    """

    @property
    def name(self) -> str:
        """What this store is, for the report. A product, a space, a path."""
        ...

    def existing(self, dataset: str) -> tuple[Row, ...] | None:
        """The rows ``dataset`` already holds, or ``None`` if there is no such dataset.

        The distinction matters and is not an optimisation: ``None`` sends
        :func:`~chip_chat.eval.dataset.publish.publish` down the create path
        and an empty tuple sends it down the add-a-version path, and getting
        those the wrong way round is how a first upload becomes a second
        dataset with the same name.

        Args:
            dataset: The dataset's name.

        Returns:
            Its current rows, or ``None``.
        """
        ...

    def create(self, dataset: str, rows: Sequence[Row]) -> str:
        """Make the dataset, with ``rows`` as its first version.

        Args:
            dataset: The name to create it under.
            rows: Every row. Never empty.

        Returns:
            What the store calls what it just made. A backend that names the
            first version separately from the dataset may return either; the
            value is carried into the report and never parsed.
        """
        ...

    def add(self, dataset: str, rows: Sequence[Row]) -> str:
        """Add ``rows`` to an existing dataset, as a new version.

        Args:
            dataset: The dataset's name.
            rows: The rows to add -- the new entries, not the whole set.

        Returns:
            What the store calls the version this created.
        """
        ...


class ArizeDatasetStore:
    """Arize AX, through the ``arize`` SDK's datasets client.

    Attributes:
        space: The Arize space id, or a space name.
        api_key: The key, or ``None`` to let the SDK read
            :data:`API_KEY_VARIABLE` itself.
    """

    def __init__(self, space: str, api_key: str | None = None) -> None:
        self.space = space
        self.api_key = api_key
        self._client: Any | None = None

    @property
    def name(self) -> str:
        """The product and the space, for the report."""
        return f"arize:{self.space}"

    def existing(self, dataset: str) -> tuple[Row, ...] | None:
        """Every row of the dataset's latest version, or ``None`` if it is new.

        The listing is filtered on an exact name rather than trusted: the SDK's
        ``name`` argument is a case-insensitive *substring* filter, so a space
        holding ``cilantro-golden-set-draft`` would otherwise answer a question
        about ``cilantro-golden-set``. It is also a paged listing whose first
        page is asked for, which is safe for the same reason -- the substring
        has already narrowed it to the handful of datasets sharing this name.
        """
        datasets = self._datasets()
        listing = datasets.list(name=dataset, space=self.space)
        if not any(item.name == dataset for item in listing.datasets):
            return None
        response = datasets.list_examples(dataset=dataset, space=self.space, all=True)
        return tuple(example.to_dict() for example in response.examples)

    def create(self, dataset: str, rows: Sequence[Row]) -> str:
        """Create the dataset with every row in it, and return its id.

        Arize names the dataset here and the version implicitly; the id is what
        there is to report, and a publish only ever reports it.
        """
        created = self._datasets().create(
            name=dataset, space=self.space, examples=[dict(row) for row in rows]
        )
        return str(created.id)

    def add(self, dataset: str, rows: Sequence[Row]) -> str:
        """Append the new rows, and return the version they landed in."""
        appended = self._datasets().append_examples(
            dataset=dataset, space=self.space, examples=[dict(row) for row in rows]
        )
        return str(appended.dataset_version_id)

    def _datasets(self) -> Any:
        """The SDK's datasets client, built once and kept.

        Imported here rather than at module scope -- see the module docstring.
        """
        if self._client is None:
            try:
                from arize.client import ArizeClient
            except ImportError as error:  # pragma: no cover -- an absent optional dep
                raise StoreError(
                    "uploading needs the `arize` SDK, which this workspace "
                    "deliberately does not pin -- see the note in "
                    "eval/pyproject.toml. Run the command as: "
                    "uv run --with arize python -m chip_chat.eval.dataset --upload"
                ) from error
            kwargs = {} if self.api_key is None else {"api_key": self.api_key}
            self._client = ArizeClient(**kwargs)
        return self._client.datasets


def arize_store_from_env(
    env: Mapping[str, str] | None = None,
) -> ArizeDatasetStore:
    """Build a store from the environment.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The store.

    Raises:
        StoreError: If the space is not configured. The API key is not checked
            here -- the SDK reads it itself, and duplicating that check would
            put a second opinion about the key's name in this file.
    """
    source = os.environ if env is None else env
    space = source.get(SPACE_VARIABLE, "").strip()
    if not space:
        raise StoreError(f"{SPACE_VARIABLE} is not set; there is nowhere to upload to")
    return ArizeDatasetStore(space=space, api_key=source.get(API_KEY_VARIABLE) or None)
