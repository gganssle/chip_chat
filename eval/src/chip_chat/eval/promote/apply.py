"""Appending a labelled draft to the golden set, and recording where it came from.

Three writes, in an order chosen so that a failure leaves nothing half-done.

1. The case is validated **before** anything is written, by loading the manifest
   it would produce through :meth:`~chip_chat.eval.golden.cases.GoldenSet.load`.
   That is the whole validation, and it is deliberately not a second copy of the
   rules: the loader already refuses a case with no requirement, a write case
   that does not check ``confirms_first``, a ``cites_adjacent`` without a
   ``cites``, an allergen message not marked ``dietary``, and a lane that
   disagrees with its tool. A promotion path with its own validator would be a
   second set of rules, free to disagree with the one the set is actually held
   to.
2. ``eval/golden/cases.json`` gains the case, appended, with the existing entries
   byte-identical. Appended rather than sorted: the dataset's version is a hash
   over entries **in build order**, so re-ordering the manifest would move the
   version without changing a row.
3. The ledger gains a row. Last, because a provenance row for a case that was
   refused is worse than no row at all.

**The committed dataset is not rewritten here**, and that is not an oversight.
``make dataset`` is the command that rebuilds ``DATASET.json``, ``make
dataset-check`` is what fails in CI while it is stale, and a promotion that
quietly regenerated the build would hide the version change inside an unrelated
commit. The promotion prints the next command instead. It is one line, and the
person who just added a case is the person who should see the version move.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from chip_chat.eval.golden.cases import DEFAULT_MANIFEST, CaseError, GoldenSet
from chip_chat.eval.promote.candidates import NEEDS_A_HUMAN, observation
from chip_chat.eval.promote.ledger import (
    DEFAULT_LEDGER,
    LedgerError,
    Promotion,
    Provenance,
    load,
    today,
    write,
)

__all__ = ["PromotionError", "apply_draft"]


class PromotionError(ValueError):
    """A draft that cannot become a case."""


def apply_draft(
    body: Mapping[str, Any],
    *,
    manifest: Path = DEFAULT_MANIFEST,
    ledger_path: Path = DEFAULT_LEDGER,
    source: str = "production",
) -> str:
    """Add one labelled draft to the golden set.

    Args:
        body: The draft, as ``--draft`` produced it and a person edited it.
        manifest: The golden case manifest to append to.
        ledger_path: The provenance ledger to record in.
        source: Where this entry came from, for the ledger.

    Returns:
        The case id that was added.

    Raises:
        PromotionError: If the draft still carries a placeholder, names a case
            id the set already holds, or produces a manifest the loader refuses.
            Every one of them leaves both files untouched.
    """
    case = {key: value for key, value in body.items() if not key.startswith("_")}
    _resolved(case)
    case_id = str(case.get("id", "")).strip()
    if not case_id:
        raise PromotionError("the draft has no `id`")

    payload = _manifest(manifest)
    existing = {entry.get("id") for entry in payload["cases"]}
    if case_id in existing:
        raise PromotionError(
            f"{manifest} already holds a case called {case_id!r}. A changed "
            "question is a new question: give it a new id rather than editing "
            "one that already has scores against it."
        )

    candidate = dict(payload)
    candidate["cases"] = [*payload["cases"], case]
    _believable(candidate, manifest)

    manifest.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    seen = observation(body)
    try:
        provenance = load(ledger_path).with_promotion(
            Promotion(
                case_id=case_id,
                source=source,
                trace_id=str(seen.get("trace_id", "")),
                monitors=tuple(str(item) for item in seen.get("monitors", ())),
                promoted_at=today(),
                why=str(seen.get("why_selected", "")),
            )
        )
    except LedgerError as error:
        raise PromotionError(str(error)) from error
    write(provenance, ledger_path)
    return case_id


def _resolved(case: Mapping[str, Any]) -> None:
    """Refuse a draft still carrying a placeholder.

    The one check this module makes that the loader cannot: ``TODO`` is a
    perfectly well-formed string, and a case whose ``why`` is ``TODO`` would
    load, run and score. What it would not do is mean anything.
    """
    unresolved = sorted(_placeholders(case))
    if unresolved:
        raise PromotionError(
            f"the draft still carries {NEEDS_A_HUMAN} in: {', '.join(unresolved)}. "
            "Those are the fields a trace cannot supply — which requirements this "
            "covers, what has to be observed, and why the case is worth having."
        )


def _placeholders(case: Mapping[str, Any], prefix: str = "") -> set[str]:
    found: set[str] = set()
    for key, value in case.items():
        where = f"{prefix}{key}"
        if value == NEEDS_A_HUMAN or (isinstance(value, list) and NEEDS_A_HUMAN in value):
            found.add(where)
        elif isinstance(value, dict):
            found |= _placeholders(value, f"{where}.")
    return found


def _manifest(manifest: Path) -> dict[str, Any]:
    """The manifest as a mutable object, refusing one that is not the set's shape."""
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as error:
        raise PromotionError(f"cannot read {manifest}: {error}") from error
    except json.JSONDecodeError as error:
        raise PromotionError(f"{manifest} is not valid JSON: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise PromotionError(f"{manifest} must be an object with a `cases` array")
    return payload


def _believable(payload: Mapping[str, Any], manifest: Path) -> None:
    """Load the manifest this draft would produce, and refuse it if it will not.

    Written to a temporary file beside the real one rather than parsed in
    memory, because :meth:`~chip_chat.eval.golden.cases.GoldenSet.load` takes a
    path and reimplementing it against a mapping would be the second validator
    this module exists not to have.
    """
    scratch = manifest.with_suffix(".candidate.json")
    try:
        scratch.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        try:
            GoldenSet.load(scratch)
        except CaseError as error:
            raise PromotionError(
                f"the set would not load with this case: {error}"
            ) from None
    finally:
        scratch.unlink(missing_ok=True)


def promoted_ids(cases: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every case id in a manifest, in file order."""
    return tuple(str(case.get("id", "")) for case in cases)


def traffic_entries(provenance: Provenance) -> int:
    """How many entries in the set originate from real production traffic.

    #77's second acceptance criterion is a count of ten, and this is the
    function that answers it. It reads the ledger rather than the manifest,
    because the manifest cannot tell an authored case from a promoted one and
    the whole reason the ledger exists is that adding a column to make it able
    to would rebase the dataset's version.
    """
    return len(provenance.from_traffic)
