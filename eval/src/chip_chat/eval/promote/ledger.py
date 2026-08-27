"""Where every entry came from, and which sources are permanent.

Two questions #77 asks that nothing in the repository could previously answer.
*Which of these entries came from real traffic?* and *is every attack the
adversarial suite survived still being run?*

**The ledger is a separate file, and that is the whole design.** The obvious
implementation is a ``provenance`` column on
:class:`~chip_chat.eval.dataset.entries.DatasetEntry`. It is also wrong, for a
reason that is invisible until the first promotion: the dataset's version is a
hash of its entries and each entry's digest is a hash of its columns, so adding a
column rebases **every existing digest** and moves the version for a reason that
has nothing to do with the rows. ``eval/dataset/README.md`` says the publish path
*will not change an entry that is already published*, and it means it --
:func:`chip_chat.eval.dataset.publish.publish` refuses an upload whose held
digests disagree. So provenance lives beside the dataset, keyed by case id, and
the day the first trace is promoted the only thing that moves is the version,
because there is one more row.

**A permanent source is a promise with a check behind it.** #77's third criterion
is *every adversarial-suite attack exists as a permanent regression entry*, and
``eval/README.md`` explains at length why the attacks are **not** in the dataset:
an attack has no expected output for an experiment to be scored against, so
promoting one would mean scoring correct behaviour as a breach that failed to
land. Both things are true at once, and the resolution is that *permanent* is not
the same as *in the dataset*. The suite is a permanent regression source: its
manifest is committed, `make adversarial-redteam` runs every attack in it, and CI
blocks on that target. What was missing was anything that fails when an attack is
added and the promise is not kept. :class:`PermanentSource` records the ids, and
:func:`check` compares them to the manifest -- so an attack added without being
recorded is a build failure rather than a gap nobody notices.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_LEDGER",
    "LedgerError",
    "PermanentSource",
    "Promotion",
    "Provenance",
    "check",
    "load",
    "today",
    "write",
]

DEFAULT_LEDGER: Final = Path("eval/dataset/PROVENANCE.json")
"""Where the ledger lives. Beside the dataset, never inside it."""

_SCHEMA: Final = 1


class LedgerError(ValueError):
    """A ledger that cannot be believed."""


@dataclass(frozen=True, slots=True)
class Promotion:
    """One entry, and where it came from.

    Attributes:
        case_id: The golden case id. The join to the manifest and, with the
            ``golden/`` prefix, to the dataset row.
        source: Where it came from -- ``production`` for a real visitor trace,
            ``authored`` for a case somebody wrote, ``adversarial`` for one
            promoted out of the suite.
        trace_id: The trace, where there was one. This is the field that makes
            *"which of these came from real traffic"* answerable.
        monitors: What selected it, by monitor name. Empty where a person chose
            it by reading.
        promoted_at: ISO-8601 UTC, to the day. The day rather than the second:
            a promotion is an editorial act and the hour it happened is noise
            in a file people read.
        why: What the monitor saw, or what the person noticed.
    """

    case_id: str
    source: str
    trace_id: str = ""
    monitors: tuple[str, ...] = ()
    promoted_at: str = ""
    why: str = ""

    @property
    def from_traffic(self) -> bool:
        """Whether this entry originates from a real production turn."""
        return self.source == "production" and bool(self.trace_id)

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form."""
        return {
            "case_id": self.case_id,
            "source": self.source,
            "trace_id": self.trace_id,
            "monitors": list(self.monitors),
            "promoted_at": self.promoted_at,
            "why": self.why,
        }


@dataclass(frozen=True, slots=True)
class PermanentSource:
    """A body of regression cases that is run forever, and how that is enforced.

    Attributes:
        name: What it is called.
        manifest: The committed file holding the cases.
        runs_in: The command that runs them, and the thing CI blocks on.
        ids: Every case in it, at the time the ledger was written. Compared to
            the manifest by :func:`check`, so adding a case without recording
            it fails the build.
        why: Why this source is permanent rather than in the dataset.
    """

    name: str
    manifest: str
    runs_in: str
    ids: tuple[str, ...] = ()
    why: str = ""

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form."""
        return {
            "name": self.name,
            "manifest": self.manifest,
            "runs_in": self.runs_in,
            "ids": list(self.ids),
            "why": self.why,
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    """The whole ledger.

    Attributes:
        promotions: One per entry that has a recorded origin. An entry with no
            row here is not an error: the thirty-four cases the set opened with
            were authored, and back-filling a row for each would be inventing a
            history. What matters is that everything promoted *since* has one.
        permanent: Sources that are run forever.
    """

    promotions: tuple[Promotion, ...] = ()
    permanent: tuple[PermanentSource, ...] = ()

    @property
    def from_traffic(self) -> tuple[Promotion, ...]:
        """Every entry that originates from a real production turn."""
        return tuple(item for item in self.promotions if item.from_traffic)

    def source(self, name: str) -> PermanentSource | None:
        """One permanent source by name, or ``None``."""
        for item in self.permanent:
            if item.name == name:
                return item
        return None

    def with_promotion(self, promotion: Promotion) -> "Provenance":
        """This ledger plus one row.

        Args:
            promotion: The row to add.

        Returns:
            A new ledger. Append-only by construction: there is no method that
            edits a row, for the reason the dataset's publish path has no
            operation that replaces one. A changed case is a new case.

        Raises:
            LedgerError: If the case already has a row. Two rows for one case
                is two answers to *where did this come from*, and the second one
                is always the wrong one.
        """
        if any(item.case_id == promotion.case_id for item in self.promotions):
            raise LedgerError(f"{promotion.case_id} already has a provenance row")
        return Provenance(
            promotions=(*self.promotions, promotion), permanent=self.permanent
        )

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form. Sorted, so two files diff cleanly."""
        return {
            "schema": _SCHEMA,
            "promotions": [dict(item.as_json()) for item in self.promotions],
            "permanent": [dict(item.as_json()) for item in self.permanent],
        }


def load(path: Path = DEFAULT_LEDGER) -> Provenance:
    """Read the ledger.

    Args:
        path: The file. A missing file is an empty ledger rather than an error:
            a repository that has never promoted anything has nothing to record,
            and refusing to run would make the first promotion the hard one.

    Returns:
        The ledger.

    Raises:
        LedgerError: If the file exists and cannot be read as one.
    """
    if not path.exists():
        return Provenance()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LedgerError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise LedgerError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise LedgerError(f"{path} is not an object")
    if payload.get("schema") != _SCHEMA:
        raise LedgerError(f"{path}: unknown schema {payload.get('schema')!r}")
    return Provenance(
        promotions=tuple(
            _promotion(item, path) for item in payload.get("promotions", ())
        ),
        permanent=tuple(_permanent(item, path) for item in payload.get("permanent", ())),
    )


def write(provenance: Provenance, path: Path = DEFAULT_LEDGER) -> None:
    """Record the ledger.

    Args:
        provenance: What to write.
        path: Where.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(provenance.as_json()), indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def check(
    provenance: Provenance, sources: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """Hold every permanent source to its manifest.

    Args:
        provenance: The ledger.
        sources: Manifest name to the ids it currently holds.

    Returns:
        One line per discrepancy, empty where every promise is kept. A returned
        line is a build failure and not a warning: a permanent source that has
        grown an entry nobody recorded is precisely the state #77's third
        criterion exists to prevent, and it is invisible from anywhere else.
    """
    problems: list[str] = []
    for name, ids in sources.items():
        recorded = provenance.source(name)
        if recorded is None:
            problems.append(
                f"{name} is not recorded as a permanent source, so nothing "
                "promises its cases are still being run"
            )
            continue
        missing = sorted(set(ids) - set(recorded.ids))
        extra = sorted(set(recorded.ids) - set(ids))
        if missing:
            problems.append(
                f"{name}: {len(missing)} case(s) in the manifest and not in the "
                f"ledger: {', '.join(missing)}"
            )
        if extra:
            problems.append(
                f"{name}: {len(extra)} case(s) in the ledger and not in the "
                f"manifest: {', '.join(extra)}"
            )
    return tuple(problems)


def today() -> str:
    """The date a promotion is stamped with, ISO-8601 UTC."""
    return datetime.now(UTC).date().isoformat()


def _promotion(payload: Any, path: Path) -> Promotion:
    if not isinstance(payload, dict):
        raise LedgerError(f"{path}: a promotion is not an object")
    try:
        return Promotion(
            case_id=str(payload["case_id"]),
            source=str(payload["source"]),
            trace_id=str(payload.get("trace_id", "")),
            monitors=tuple(str(item) for item in payload.get("monitors", ())),
            promoted_at=str(payload.get("promoted_at", "")),
            why=str(payload.get("why", "")),
        )
    except KeyError as error:
        raise LedgerError(f"{path}: a promotion is missing {error}") from None


def _permanent(payload: Any, path: Path) -> PermanentSource:
    if not isinstance(payload, dict):
        raise LedgerError(f"{path}: a permanent source is not an object")
    try:
        return PermanentSource(
            name=str(payload["name"]),
            manifest=str(payload["manifest"]),
            runs_in=str(payload["runs_in"]),
            ids=tuple(str(item) for item in payload.get("ids", ())),
            why=str(payload.get("why", "")),
        )
    except KeyError as error:
        raise LedgerError(f"{path}: a permanent source is missing {error}") from None
