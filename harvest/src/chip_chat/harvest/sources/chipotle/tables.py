"""Turning parsed rows into bytes, and describing what came out.

Both datasets this source produces — the menu in issue #19 and the nutrition
and allergen data in issue #20 — are flat tables of frozen dataclasses that
have to serialise identically on every run. That requirement is the same for
both, so it is implemented once here rather than twice beside each of them.

The serialisation is deliberately boring: sorted keys, tight separators, one
object per line. Two runs over the same cache produce the same bytes, which is
what makes the SHA-256 in a manifest a number worth comparing.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from chip_chat.harvest.blobs import BlobStore


def _json_ready(value: Any) -> Any:
    """Return ``value`` in a form :func:`json.dumps` can write deterministically."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def _row(record: Any) -> dict[str, Any]:
    """Return one dataclass instance as a JSON-ready mapping."""
    return {
        field.name: _json_ready(getattr(record, field.name)) for field in fields(record)
    }


def to_jsonl(records: Iterable[Any]) -> bytes:
    """Serialise records as JSON Lines, one compact object per line.

    Keys are sorted and separators are tight, so two runs over the same cache
    produce identical bytes and a digest is a meaningful thing to compare.

    Args:
        records: The dataclass instances to write, in the order wanted.

    Returns:
        The encoded document, ending in a newline when it is not empty.
    """
    lines = [
        json.dumps(_row(record), sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def describe(tables: Iterable[tuple[str, Sequence[Any]]]) -> dict[str, Any]:
    """Return each table's row count and the digest of its serialised bytes.

    Args:
        tables: ``(name, rows)`` pairs, in the order the manifest should list
            them.

    Returns:
        Table name to ``{"rows": int, "sha256": str}``.
    """
    return {
        name: {
            "rows": len(rows),
            "sha256": hashlib.sha256(to_jsonl(rows)).hexdigest(),
        }
        for name, rows in tables
    }


def write_tables(
    blobs: BlobStore,
    prefix: str,
    tables: Iterable[tuple[str, Sequence[Any]]],
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Write every table, and the manifest, under one prefix.

    Args:
        blobs: Where to write. The same store the raw bytes landed in, under a
            different prefix.
        prefix: Key prefix for the parsed tables.
        tables: ``(name, rows)`` pairs.
        manifest: The manifest to write beside them.

    Returns:
        Table name to the key it was written at, with the manifest under the
        key ``manifest``.
    """
    root = prefix.strip("/")
    written: dict[str, str] = {}
    for name, rows in tables:
        key = f"{root}/{name}.jsonl"
        blobs.write(key, to_jsonl(rows))
        written[name] = key
    manifest_key = f"{root}/manifest.json"
    blobs.write(
        manifest_key,
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    written["manifest"] = manifest_key
    return written
