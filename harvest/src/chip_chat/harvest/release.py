"""The corpus is published, never patched.

RFC-001 section 08 states the rule for the retrieval index: *the index is
rebuilt, never patched* — a fresh index is built alongside the live one and an
alias is swapped to it, so a harvest that dies halfway cannot leave the corpus
half-updated. Issue #48 will do that to an Azure AI Search index. This module
is the same rule one layer up, at the only layer that exists today, and it is
deliberately the *same* rule rather than a different one that resembles it.

A run writes everything it produces under ``corpus/runs/<run_id>/`` and touches
nothing outside it. Then, and only if the whole run succeeded, it writes one
small object at ``corpus/current.json`` naming that run. That single write is
the swap. Downstream reads the corpus by resolving the pointer, so:

* a run that fails at document 40 of 57 leaves ``current.json`` naming last
  week's run, and every consumer keeps seeing a complete corpus;
* the failed run is still on disk, under its own id, with a record saying what
  went wrong — a failure you can read is worth more than a failure you rolled
  back;
* nothing is ever copied to make a release live, so there is no window during
  the promotion in which half the tables are new.

**One thing this does not make atomic, said plainly.** The raw fetch-once cache
under ``raw/`` is shared and is written as the harvest goes. That is safe but it
is not transactional: bodies are content-addressed and a new one never destroys
an old one, so nothing is lost, but a failed run does leave some pointers moved
forward. Bronze (gh-33) ingests ``raw/`` directly and therefore sees that.
The release pointer is what makes a *corpus* atomic; the raw zone is
append-only, which is a weaker and different promise. See
``docs/corpus-freshness.md``.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.errors import HarvestError

DEFAULT_PREFIX = "corpus"
"""Root of everything a re-harvest publishes. Beside ``raw/``, never inside it."""

CURRENT = "current.json"
"""The pointer. One write of this object is the whole swap."""

RUNS = "runs"


class ReleaseError(HarvestError):
    """A release could not be read or published."""


def run_id_for(started_at: datetime) -> str:
    """Return the run identifier for a run that started at ``started_at``.

    Compact UTC ISO — ``20260826T193456Z`` — so that runs sort chronologically
    as plain strings, in a blob store whose only ordering is lexical.

    Args:
        started_at: When the run began. Must be timezone-aware.

    Returns:
        The identifier.

    Raises:
        ValueError: If ``started_at`` is naive. A run id built from a naive
            timestamp would sort against the others by an unknown offset.
    """
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")
    return started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True, slots=True)
class Release:
    """One completed run, as ``current.json`` records it.

    Only completed runs become releases. A failed run has a record under
    ``corpus/runs/`` and is not one of these, which is the distinction the
    whole module exists to keep.

    Attributes:
        run_id: The run that produced this corpus.
        published_at: When the pointer was written — the "last successful
            harvest" of issue #38.
        prefix: Key prefix everything this run produced lives under. Consumers
            resolve the corpus through this rather than through a fixed path,
            which is what makes the swap a swap.
        documents: Corpus documents in the release.
        changed: How many of them differed from the previous release. Zero is
            a normal and good answer for a week in which the menu did not move.
        report_key: Where the run's change report was written.
    """

    run_id: str
    published_at: datetime
    prefix: str
    documents: int
    changed: int
    report_key: str

    def as_dict(self) -> dict[str, Any]:
        """Return the release as a JSON-ready mapping."""
        return {
            "run_id": self.run_id,
            "published_at": self.published_at.isoformat(),
            "prefix": self.prefix,
            "documents": self.documents,
            "changed": self.changed,
            "report_key": self.report_key,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Release":
        """Read a release back.

        Args:
            raw: A decoded ``current.json``.

        Returns:
            The release.

        Raises:
            ReleaseError: If a required field is missing or unreadable.
        """
        try:
            return cls(
                run_id=str(raw["run_id"]),
                published_at=datetime.fromisoformat(str(raw["published_at"])),
                prefix=str(raw["prefix"]),
                documents=int(raw["documents"]),
                changed=int(raw["changed"]),
                report_key=str(raw["report_key"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ReleaseError(f"unreadable release pointer: {error}") from error


class ReleaseStore:
    """Where runs are staged and where the live pointer lives."""

    def __init__(self, blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> None:
        """Initialise the store.

        Args:
            blobs: The landing zone. The same store the raw bytes are in,
                under a different prefix.
            prefix: Key prefix for everything published.
        """
        self._blobs = blobs
        self._prefix = prefix.strip("/")

    @property
    def current_key(self) -> str:
        """The one key whose contents decide which corpus is live."""
        return f"{self._prefix}/{CURRENT}"

    def run_prefix(self, run_id: str) -> str:
        """Return the prefix one run stages everything under.

        Args:
            run_id: The run's identifier.

        Returns:
            The prefix, with no trailing slash.
        """
        return f"{self._prefix}/{RUNS}/{run_id}"

    def record_key(self, run_id: str) -> str:
        """Return the key holding one run's record, complete or failed."""
        return f"{self.run_prefix(run_id)}/run.json"

    def report_key(self, run_id: str) -> str:
        """Return the key holding one run's change report, for a human."""
        return f"{self.run_prefix(run_id)}/change-report.md"

    def write_record(self, run_id: str, record: Mapping[str, Any]) -> str:
        """Write one run's record under its own prefix.

        Writing this for a **failed** run is as important as writing it for a
        successful one, and is why it is a separate call from :meth:`publish`.
        A weekly job whose only artefact on failure is a log line is a job
        whose failures nobody can compare.

        Args:
            run_id: The run's identifier.
            record: The record, JSON-ready.

        Returns:
            The key written.
        """
        key = self.record_key(run_id)
        self._blobs.write(key, _dumps(record))
        return key

    def write_report(self, run_id: str, report: str) -> str:
        """Write one run's rendered change report.

        Args:
            run_id: The run's identifier.
            report: The report, as Markdown.

        Returns:
            The key written.
        """
        key = self.report_key(run_id)
        self._blobs.write(key, report.encode("utf-8"))
        return key

    def current(self) -> Release | None:
        """Return the live release, or ``None`` if nothing has published yet.

        Returns:
            The release.

        Raises:
            ReleaseError: If the pointer exists and cannot be read. A corpus
                whose pointer is unreadable is not a corpus with no pointer,
                and answering ``None`` would let a re-harvest publish over a
                release it could not see.
        """
        raw = self._blobs.read(self.current_key)
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"{self.current_key} is not JSON: {error}") from error
        if not isinstance(decoded, dict):
            raise ReleaseError(f"{self.current_key} is not an object")
        return Release.from_dict(decoded)

    def publish(self, release: Release) -> Release:
        """Make ``release`` the live corpus, with one write.

        The run's own record must already be on disk: this writes the pointer
        and nothing else, so that the moment between "the corpus exists" and
        "the corpus is live" is a single object store operation rather than a
        loop over files.

        Args:
            release: The release to publish.

        Returns:
            The release, unchanged, so this can be the last expression of a run.

        Raises:
            ReleaseError: If the run's record is not where the release says it
                is. Publishing a pointer at a corpus that is not there is the
                one failure this module exists to prevent, so it is checked
                rather than assumed.
        """
        record = self.record_key(release.run_id)
        if not self._blobs.exists(record):
            raise ReleaseError(
                f"refusing to publish {release.run_id}: {record} does not exist"
            )
        self._blobs.write(self.current_key, _dumps(release.as_dict()))
        return release


def read_current(blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> Release | None:
    """Return the live release in ``blobs``, or ``None``.

    A convenience for the readers — the freshness command, and in time the
    index build of issue #48 — that want the pointer and nothing else.

    Args:
        blobs: The landing zone.
        prefix: Key prefix for everything published.

    Returns:
        The release, or ``None`` if nothing has published yet.

    Raises:
        ReleaseError: If the pointer exists and cannot be read.
    """
    return ReleaseStore(blobs, prefix).current()


def _dumps(payload: Mapping[str, Any]) -> bytes:
    """Serialise deterministically, so two identical runs write identical bytes."""
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
