"""What changed since last week, at the two levels worth being told about.

Issue #38 asks for diff detection "surfaced as a report rather than silently
absorbed", and gives the example that matters: *a menu item disappearing is
interesting*. That example rules out the cheap version. Comparing the digests
of the raw responses tells you that ``/api/menu`` changed; it does not tell you
that the Barbacoa Bowl is gone, and a report that says nine documents changed
is a report that gets skimmed.

So there are two levels here.

**Documents** (:func:`diff_documents`). One row per harvested URL: added,
changed, unchanged, or removed, with both digests. This comes free — the
fetch-once cache already records ``previous_sha256`` — and it is the level at
which "the site restructured its API" is visible.

**Rows** (:func:`diff_tables`). One row per parsed record, keyed by whatever
identifies it, so that a vanished item, a moved price and a flipped allergen
tag are three distinct and separately readable findings.

Two decisions in the row diff are worth stating, because getting either wrong
produces a report that is technically correct and practically worthless.

``harvested_at`` is excluded from the comparison.
    Every parsed row carries it, and it moves on every single re-harvest. Diff
    whole rows and *every row in the corpus* is reported as modified every
    week, which is the same as reporting nothing. What a row is compared on is
    everything it says about the world, minus when we last looked.

A key that turns out not to be unique degrades the diff, it does not corrupt it.
    Identity for these tables is declared by hand next to the tables (see
    ``TABLE_KEYS`` in each source's records module), and a hand-declared key can
    be wrong. When one is, :func:`diff_tables` notices the collision, falls back
    to comparing row *contents* for that table alone, and says so in the result.
    An unkeyed diff reports a modification as one removal plus one addition —
    less informative, never untrue.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.freshness import is_corpus

VOLATILE_COLUMNS = frozenset({"harvested_at"})
"""Columns excluded from a row's comparison digest. See the module docstring."""

ADDED = "added"
CHANGED = "changed"
REMOVED = "removed"
UNCHANGED = "unchanged"

KEY_SEPARATOR = "\x1f"
"""ASCII unit separator: it cannot occur in a JSON string that came from a URL,
a menu item name or a store id, so a compound key cannot be forged by a value
that happens to contain the separator."""


# --- Documents ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentChange:
    """One harvested URL, and what happened to it this run.

    Attributes:
        url: The canonical URL.
        status: ``added``, ``changed``, ``removed`` or ``unchanged``.
        before: The digest the URL resolved to in the previous release, or
            ``None`` if it is new.
        after: The digest it resolves to now, or ``None`` if it is gone.
    """

    url: str
    status: str
    before: str | None
    after: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the change as a JSON-ready mapping."""
        return {
            "url": self.url,
            "status": self.status,
            "before": self.before,
            "after": self.after,
        }


def snapshot_documents(cache: DocumentCache) -> dict[str, str]:
    """Return every corpus URL in ``cache`` mapped to its body's digest.

    Taken before a re-harvest and again after, this pair is the document-level
    diff. It is deliberately not derived from ``previous_sha256`` alone: that
    field records the last change a URL saw, whenever it happened, and a run
    needs to know what changed *in this run*.

    Args:
        cache: The document cache to read.

    Returns:
        Canonical URL to hex SHA-256.

    Raises:
        CacheCorruptError: If a pointer cannot be read.
    """
    return {
        str(pointer["requested_url"]): str(pointer["content_sha256"])
        for pointer in cache.pointers()
        if is_corpus(pointer)
    }


def diff_documents(
    before: Mapping[str, str], after: Mapping[str, str]
) -> tuple[DocumentChange, ...]:
    """Compare two document snapshots.

    Args:
        before: URL to digest, before the run.
        after: URL to digest, after it.

    Returns:
        One change per URL in either snapshot, ordered by URL. Unchanged URLs
        are included — the count of them is the evidence that a re-harvest
        confirmed the rest of the corpus rather than skipping it.
    """
    changes: list[DocumentChange] = []
    for url in sorted(set(before) | set(after)):
        was, now = before.get(url), after.get(url)
        if was is None:
            status = ADDED
        elif now is None:
            status = REMOVED
        elif was == now:
            status = UNCHANGED
        else:
            status = CHANGED
        changes.append(DocumentChange(url=url, status=status, before=was, after=now))
    return tuple(changes)


# --- Rows --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowChange:
    """One parsed row, and what happened to it this run.

    Attributes:
        key: The row's identity, rendered for a human — ``CMG-2`` for a menu
            item, ``2140/CMG-2`` for a price at one restaurant.
        status: ``added``, ``changed`` or ``removed``. Unchanged rows are not
            listed; there are tens of thousands of them and their count is
            reported instead.
    """

    key: str
    status: str


@dataclass(frozen=True, slots=True)
class TableChange:
    """What one parsed table looks like against its previous release.

    Attributes:
        table: The table's name.
        keyed: Whether the diff used the table's declared identity. ``False``
            means the key was not unique in one of the two versions and the
            diff fell back to comparing row contents, so a modified row shows
            up as a removal and an addition. See the module docstring.
        rows_before: Rows in the previous release, or ``None`` if the table is
            new.
        rows_after: Rows now.
        unchanged: Rows present in both and identical.
        changes: Every added, changed and removed row, ordered by key.
    """

    table: str
    keyed: bool
    rows_before: int | None
    rows_after: int
    unchanged: int
    changes: tuple[RowChange, ...] = ()

    @property
    def added(self) -> tuple[RowChange, ...]:
        """The rows that appeared."""
        return tuple(change for change in self.changes if change.status == ADDED)

    @property
    def removed(self) -> tuple[RowChange, ...]:
        """The rows that vanished. The interesting ones, per issue #38."""
        return tuple(change for change in self.changes if change.status == REMOVED)

    @property
    def modified(self) -> tuple[RowChange, ...]:
        """The rows that stayed but say something different."""
        return tuple(change for change in self.changes if change.status == CHANGED)

    @property
    def is_quiet(self) -> bool:
        """Return whether nothing at all happened to this table."""
        return not self.changes and self.rows_before == self.rows_after

    def as_dict(self) -> dict[str, Any]:
        """Return the table's changes as a JSON-ready mapping."""
        return {
            "table": self.table,
            "keyed": self.keyed,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "unchanged": self.unchanged,
            "added": [change.key for change in self.added],
            "removed": [change.key for change in self.removed],
            "modified": [change.key for change in self.modified],
        }


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    """One parsed table, reduced to what a diff needs.

    Attributes:
        table: The table's name.
        rows: Row key to comparison digest. Empty when the table did not exist.
        present: Whether the table existed at all. Distinguishes "no rows" —
            which the PDF dataset produces legitimately, Chipotle having
            published no nutrition sheets — from "never written".
        keyed: Whether every row key was unique.
    """

    table: str
    rows: Mapping[str, str] = field(default_factory=dict)
    present: bool = True
    keyed: bool = True


def row_key(row: Mapping[str, Any], columns: Sequence[str]) -> str:
    """Return one row's identity, as a single string.

    Args:
        row: The decoded row.
        columns: The columns that identify it, in declaration order.

    Returns:
        The values joined by :data:`KEY_SEPARATOR`. A column the row does not
        carry contributes the empty string rather than raising: a key declared
        against a table whose shape has since moved should weaken the diff, not
        abort the weekly job.
    """
    return KEY_SEPARATOR.join(str(row.get(column, "")) for column in columns)


def row_digest(row: Mapping[str, Any]) -> str:
    """Return the digest a row is compared on, ignoring when it was harvested.

    Args:
        row: The decoded row.

    Returns:
        A hex SHA-256 over the row's non-volatile fields, serialised the same
        way :mod:`chip_chat.harvest.sources.chipotle.tables` serialises them,
        so two runs over unchanged bytes produce the same digest.
    """
    comparable = {
        name: value for name, value in row.items() if name not in VOLATILE_COLUMNS
    }
    encoded = json.dumps(comparable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_rows(blobs: BlobStore, key: str) -> list[dict[str, Any]] | None:
    """Read one JSON Lines table.

    Args:
        blobs: Where to read from.
        key: The blob key.

    Returns:
        The decoded rows, or ``None`` if the blob does not exist. An empty list
        and ``None`` are different answers and the caller distinguishes them.

    Raises:
        ValueError: If a line is not a JSON object. A table half-written by an
            interrupted run must not be silently diffed as if it were short.
    """
    raw = blobs.read(key)
    if raw is None:
        return None
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise ValueError(f"{key}:{number}: expected a JSON object")
        rows.append(decoded)
    return rows


def snapshot_table(
    blobs: BlobStore, prefix: str, table: str, key_columns: Sequence[str]
) -> TableSnapshot:
    """Reduce one written table to keys and comparison digests.

    Args:
        blobs: Where the parsed tables live.
        prefix: The dataset's key prefix.
        table: The table's name.
        key_columns: The columns that identify a row.

    Returns:
        The snapshot. ``keyed`` is false if two rows shared a key, which is how
        a mis-declared identity is caught rather than believed.

    Raises:
        ValueError: If the table is not readable as JSON Lines.
    """
    rows = read_rows(blobs, f"{prefix.strip('/')}/{table}.jsonl")
    if rows is None:
        return TableSnapshot(table=table, rows={}, present=False)
    digests: dict[str, str] = {}
    keyed = True
    for row in rows:
        key = row_key(row, key_columns)
        if key in digests:
            keyed = False
        digests[key] = row_digest(row)
    if not keyed:
        # Fall back to keying every row by its own contents, which is always
        # unique enough to diff and never claims an identity it does not have.
        digests = {digest: digest for digest in (row_digest(row) for row in rows)}
    return TableSnapshot(table=table, rows=digests, present=True, keyed=keyed)


def snapshot_tables(
    blobs: BlobStore, prefix: str, keys: Mapping[str, Sequence[str]]
) -> dict[str, TableSnapshot]:
    """Snapshot every table a dataset declares.

    Args:
        blobs: Where the parsed tables live.
        prefix: The dataset's key prefix.
        keys: Table name to its identity columns — a source's ``TABLE_KEYS``.

    Returns:
        Table name to snapshot.

    Raises:
        ValueError: If a table is not readable as JSON Lines.
    """
    return {
        table: snapshot_table(blobs, prefix, table, columns)
        for table, columns in keys.items()
    }


def _render_key(key: str) -> str:
    """Return a compound row key in a form a human can read."""
    return key.replace(KEY_SEPARATOR, "/") or "(empty)"


def diff_table(before: TableSnapshot | None, after: TableSnapshot) -> TableChange:
    """Compare one table against its previous release.

    Args:
        before: The previous snapshot, or ``None`` if there was no previous
            release at all.
        after: The snapshot just taken.

    Returns:
        The table's changes. When either side was not keyed, the result is not
        keyed either — a keyed diff against an unkeyed snapshot would compare
        identities against contents and report the whole table as replaced.
    """
    was = dict(before.rows) if before is not None and before.present else {}
    keyed = after.keyed and (before is None or before.keyed)
    changes: list[RowChange] = []
    unchanged = 0
    for key in sorted(set(was) | set(after.rows)):
        old, new = was.get(key), after.rows.get(key)
        if old is None:
            changes.append(RowChange(key=_render_key(key), status=ADDED))
        elif new is None:
            changes.append(RowChange(key=_render_key(key), status=REMOVED))
        elif old == new:
            unchanged += 1
        else:
            changes.append(RowChange(key=_render_key(key), status=CHANGED))
    return TableChange(
        table=after.table,
        keyed=keyed,
        rows_before=(len(before.rows) if before is not None and before.present else None),
        rows_after=len(after.rows),
        unchanged=unchanged,
        changes=tuple(changes),
    )


def diff_tables(
    before: Mapping[str, TableSnapshot] | None,
    after: Mapping[str, TableSnapshot],
) -> tuple[TableChange, ...]:
    """Compare every table in ``after`` against its previous release.

    Args:
        before: The previous snapshots by table name, or ``None`` for a first
            release. A first release reports every row as added, which is
            accurate and is what a cold start looks like.
        after: The snapshots just taken.

    Returns:
        One :class:`TableChange` per table, in the order ``after`` lists them.
    """
    return tuple(
        diff_table(None if before is None else before.get(table), snapshot)
        for table, snapshot in after.items()
    )


# --- The report --------------------------------------------------------------

MAX_LISTED_ROWS = 20
"""How many row keys a table lists before the report says how many it did not.

A restructured API can change every row in a table; a report that then names
forty thousand of them is a report nobody opens, and one that silently shows
twenty reads as if only twenty changed. So the count is always stated.
"""


def render_report(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    ok: bool,
    documents: Sequence[DocumentChange],
    tables: Sequence[TableChange],
    requests_made: int,
    revalidations: int,
    bytes_fetched: int,
    freshness: str,
    failures: Mapping[str, str] | None = None,
) -> str:
    """Render one run's change report as Markdown.

    Args:
        run_id: The run's identifier.
        started_at: When it began, ISO.
        finished_at: When it ended, ISO.
        ok: Whether it completed and published.
        documents: The document-level diff.
        tables: The row-level diff.
        requests_made: Requests that reached the network.
        revalidations: How many of those were answered 304.
        bytes_fetched: Response bodies stored, in bytes.
        freshness: The rendered corpus freshness block.
        failures: URL to the reason it failed, if any did.

    Returns:
        The report, ending in a newline.
    """
    changed = [change for change in documents if change.status != UNCHANGED]
    lines = [
        f"# Corpus change report — {run_id}",
        "",
        f"- **Outcome**: {'published' if ok else 'FAILED — nothing published'}",
        f"- **Started**: {started_at}",
        f"- **Finished**: {finished_at}",
        f"- **Documents**: {len(documents)} in the corpus, {len(changed)} changed",
        f"- **Requests**: {requests_made}, of which {revalidations} were answered "
        f"304 Not Modified",
        f"- **Bodies fetched**: {_bytes(bytes_fetched)}",
        "",
        "```",
        freshness,
        "```",
        "",
    ]

    lines += ["## Documents", ""]
    if not changed:
        lines += [
            f"No document changed. All {len(documents)} were re-confirmed against "
            "the source.",
            "",
        ]
    else:
        lines += ["| URL | status | before | after |", "| --- | --- | --- | --- |"]
        for change in changed:
            lines.append(
                f"| `{change.url}` | {change.status} | "
                f"{_short(change.before)} | {_short(change.after)} |"
            )
        lines.append("")

    lines += ["## Rows", ""]
    noisy = [table for table in tables if not table.is_quiet]
    if not noisy:
        lines += ["No parsed row changed.", ""]
    for table in noisy:
        before = "new" if table.rows_before is None else str(table.rows_before)
        lines.append(
            f"### `{table.table}` — {before} → {table.rows_after} rows"
            f"{'' if table.keyed else ' (unkeyed: see changes.py)'}"
        )
        lines.append("")
        for status, rows in (
            (REMOVED, table.removed),
            (ADDED, table.added),
            (CHANGED, table.modified),
        ):
            if not rows:
                continue
            shown = ", ".join(f"`{row.key}`" for row in rows[:MAX_LISTED_ROWS])
            more = (
                f" and {len(rows) - MAX_LISTED_ROWS} more"
                if len(rows) > MAX_LISTED_ROWS
                else ""
            )
            lines.append(f"- **{status}** ({len(rows)}): {shown}{more}")
        lines.append("")

    if failures:
        lines += ["## Failures", ""]
        for url, reason in sorted(failures.items()):
            lines.append(f"- `{url}`: {reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _short(digest: str | None) -> str:
    """Return the first twelve characters of a digest, or a dash."""
    return f"`{digest[:12]}`" if digest else "—"


def _bytes(count: int) -> str:
    """Return a byte count in the largest unit that leaves it above one."""
    size = float(count)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def changed_count(documents: Iterable[DocumentChange]) -> int:
    """Return how many documents were not unchanged.

    Args:
        documents: The document-level diff.

    Returns:
        The count that goes into the release record.
    """
    return sum(1 for change in documents if change.status != UNCHANGED)
