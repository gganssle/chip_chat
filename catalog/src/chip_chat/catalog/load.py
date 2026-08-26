"""Reading a written catalogue back into its records.

Issue #24 asks that the tables be "produced and loadable", and the second half
is the harder claim: a table that can only be written is a table whose schema
is whatever the writer happened to do that day. This module reads the JSON
Lines back through the same dataclasses that wrote them, so
``test_catalog_roundtrip.py`` can assert that a catalogue written and read
again is equal to the one that was written — which is the only version of
"loadable" worth asserting.

The decoding is driven by the record classes' own type annotations rather than
by a hand-written table per class. A column added to
:mod:`chip_chat.catalog.records` is therefore loadable the moment it exists,
and a column whose written value does not fit its annotation raises rather
than arriving as a string that looks like a number.
"""

import json
import types
import typing
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, get_args, get_origin

from chip_chat.catalog.errors import CatalogLoadError
from chip_chat.catalog.records import (
    DEFAULT_PREFIX,
    TABLES,
    Allergen,
    Caveat,
    ItemAllergen,
    ItemPrice,
    MenuCatalog,
    MenuItem,
    Modifier,
    Store,
    VocabularyTerm,
)
from chip_chat.harvest.blobs import BlobStore

ROW_TYPES: dict[str, type] = {
    "menu_items": MenuItem,
    "item_prices": ItemPrice,
    "modifiers": Modifier,
    "stores": Store,
    "item_allergens": ItemAllergen,
    "allergens": Allergen,
    "caveats": Caveat,
    "vocabulary": VocabularyTerm,
}
"""Which record class each table's rows are. Keyed by :data:`~...records.TABLES`."""


def load_catalog(blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> MenuCatalog:
    """Read a catalogue back from the blob store it was written to.

    Args:
        blobs: Where the catalogue was written.
        prefix: The key prefix it was written under.

    Returns:
        The catalogue.

    Raises:
        CatalogLoadError: If a table or the manifest is missing, if a row does
            not fit its record class, or if the manifest's row counts do not
            match what was read. A catalogue that loads short is worse than
            one that does not load, because nothing downstream would notice.
    """
    root = prefix.strip("/")
    manifest = _manifest(blobs, f"{root}/manifest.json")
    tables = {
        name: _rows(blobs, f"{root}/{name}.jsonl", ROW_TYPES[name]) for name in TABLES
    }
    _require_counts(manifest, tables)

    catalog = MenuCatalog(
        reference_restaurant_id=_int(manifest, "reference_restaurant_id"),
        restaurant_ids=tuple(
            int(value) for value in _sequence(manifest, "restaurant_ids")
        ),
        menu_items=tuple(tables["menu_items"]),
        item_prices=tuple(tables["item_prices"]),
        modifiers=tuple(tables["modifiers"]),
        stores=tuple(tables["stores"]),
        item_allergens=tuple(tables["item_allergens"]),
        allergens=tuple(tables["allergens"]),
        caveats=tuple(tables["caveats"]),
        vocabulary=tuple(tables["vocabulary"]),
    )
    _require_version(manifest, catalog)
    return catalog


def _read(blobs: BlobStore, key: str) -> bytes:
    """Read one blob, or say which one was not there.

    A catalogue with a table missing is the failure mode this exists for: the
    build wrote eight tables and something downstream is about to resolve
    against seven of them without noticing.
    """
    body = blobs.read(key)
    if body is None:
        raise CatalogLoadError(f"no catalogue blob at {key}")
    return body


def _manifest(blobs: BlobStore, key: str) -> dict[str, Any]:
    """Read and decode the manifest."""
    payload = json.loads(_read(blobs, key).decode("utf-8"))
    if not isinstance(payload, dict):
        raise CatalogLoadError(f"{key} is not a manifest")
    return payload


def _rows(blobs: BlobStore, key: str, row_type: type) -> list[Any]:
    """Decode one table's JSON Lines into its record class."""
    body = _read(blobs, key).decode("utf-8")
    rows: list[Any] = []
    for number, line in enumerate(body.splitlines(), start=1):
        try:
            rows.append(_decode(json.loads(line), row_type))
        except (CatalogLoadError, ValueError, TypeError) as error:
            raise CatalogLoadError(f"{key} line {number}: {error}") from error
    return rows


def _require_counts(manifest: dict[str, Any], tables: dict[str, list[Any]]) -> None:
    """Fail if a table read short of what the manifest says it holds."""
    described = manifest.get("tables")
    if not isinstance(described, dict):
        raise CatalogLoadError("the manifest describes no tables")
    for name, rows in tables.items():
        entry = described.get(name)
        expected = entry.get("rows") if isinstance(entry, dict) else None
        if expected != len(rows):
            raise CatalogLoadError(
                f"{name} holds {len(rows)} rows and the manifest says {expected}"
            )


def _require_version(manifest: dict[str, Any], catalog: MenuCatalog) -> None:
    """Fail if the catalogue read back is not the catalogue that was written.

    The digests are recomputed from the loaded records rather than trusted, so
    a table edited in place after it was written is caught here instead of
    downstream, where "the catalogue says so" is the end of the argument.
    """
    for key, actual in (
        ("catalog_version", catalog.version()),
        ("content_version", catalog.content_version()),
    ):
        expected = manifest.get(key)
        if expected != actual:
            raise CatalogLoadError(
                f"{key} is {actual} and the manifest says {expected}; the "
                f"catalogue has been altered since it was written"
            )


def _int(manifest: dict[str, Any], key: str) -> int:
    """Read one integer out of the manifest."""
    value = manifest.get(key)
    if not isinstance(value, int):
        raise CatalogLoadError(f"the manifest's {key} is {value!r}, not an integer")
    return value


def _sequence(manifest: dict[str, Any], key: str) -> Sequence[Any]:
    """Read one list out of the manifest."""
    value = manifest.get(key)
    if not isinstance(value, list):
        raise CatalogLoadError(f"the manifest's {key} is {value!r}, not a list")
    return value


def _decode(payload: Any, wanted: Any) -> Any:
    """Return ``payload`` as ``wanted``, following the annotation exactly.

    Args:
        payload: What :func:`json.loads` produced.
        wanted: The annotation the value has to satisfy.

    Returns:
        The decoded value.

    Raises:
        CatalogLoadError: If the value does not fit. Nothing is coerced past
            what JSON loses on the way out — a ``Decimal`` written as a string
            comes back a ``Decimal``, and a string written where an integer
            belongs is an error rather than an ``int()`` call.
    """
    origin = get_origin(wanted)
    if origin in (typing.Union, types.UnionType):
        return _decode_union(payload, get_args(wanted))
    if origin is tuple:
        return _decode_tuple(payload, get_args(wanted))
    if is_dataclass(wanted) and isinstance(wanted, type):
        return _decode_row(payload, wanted)
    if isinstance(wanted, type) and issubclass(wanted, Enum):
        return wanted(payload)
    return _decode_scalar(payload, wanted)


def _decode_union(payload: Any, options: tuple[Any, ...]) -> Any:
    """Decode a ``X | None`` annotation, which is every optional column here."""
    if payload is None:
        if type(None) in options:
            return None
        raise CatalogLoadError(f"null is not one of {options}")
    for option in options:
        if option is type(None):
            continue
        return _decode(payload, option)
    raise CatalogLoadError(f"{payload!r} is not one of {options}")


def _decode_tuple(payload: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Decode a homogeneous ``tuple[X, ...]`` column."""
    if not isinstance(payload, list):
        raise CatalogLoadError(f"{payload!r} is not a list")
    if len(args) != 2 or args[1] is not Ellipsis:
        raise CatalogLoadError(f"only homogeneous tuples are supported, not {args}")
    return tuple(_decode(item, args[0]) for item in payload)


def _decode_row(payload: Any, wanted: type) -> Any:
    """Decode a nested record — a store's hours, and nothing else today."""
    if not isinstance(payload, dict):
        raise CatalogLoadError(f"{payload!r} is not an object")
    hints = typing.get_type_hints(wanted)
    names = {field.name for field in fields(wanted)}
    unknown = set(payload) - names
    if unknown:
        raise CatalogLoadError(
            f"{wanted.__name__} has no column {', '.join(sorted(unknown))}"
        )
    missing = names - set(payload)
    if missing:
        raise CatalogLoadError(
            f"{wanted.__name__} is missing {', '.join(sorted(missing))}"
        )
    return wanted(**{name: _decode(payload[name], hints[name]) for name in names})


def _decode_scalar(payload: Any, wanted: Any) -> Any:
    """Decode the four scalar kinds a catalogue column can hold."""
    if wanted is datetime:
        if not isinstance(payload, str):
            raise CatalogLoadError(f"{payload!r} is not a timestamp")
        return datetime.fromisoformat(payload)
    if wanted is Decimal:
        if not isinstance(payload, str):
            raise CatalogLoadError(
                f"{payload!r} is not a decimal; money and figures are written "
                f"as strings so that they do not pick up binary-float noise"
            )
        try:
            return Decimal(payload)
        except InvalidOperation as error:
            raise CatalogLoadError(f"{payload!r} is not a decimal") from error
    if wanted is bool:
        if not isinstance(payload, bool):
            raise CatalogLoadError(f"{payload!r} is not a boolean")
        return payload
    if wanted is int:
        if not isinstance(payload, int) or isinstance(payload, bool):
            raise CatalogLoadError(f"{payload!r} is not an integer")
        return payload
    if wanted is str:
        if not isinstance(payload, str):
            raise CatalogLoadError(f"{payload!r} is not a string")
        return payload
    raise CatalogLoadError(f"no rule for decoding {wanted!r}")
