"""How old the corpus is, measured rather than assumed.

Issue #38 asks for three numbers: the oldest ``harvested_at`` in the corpus,
the last successful harvest, and how many items changed. Only the first is a
property of the cache alone; the other two are properties of a *run*, and they
come from the release record in :mod:`chip_chat.harvest.release`.

The oldest is the one that matters and the one nobody would pick by accident.
An average age hides the one page that stopped being re-fetched six months ago;
a newest age is a number that is always reassuring and never true. The corpus
is exactly as fresh as its stalest document, so that is what is reported, along
with which document it is — because "the corpus is 41 days old" is a fact and
"``…/nutrition`` is 41 days old" is something you can go and fix.

**Freshness here is enforced, not merely displayed.** :meth:`CorpusFreshness.is_stale`
is what makes the weekly job fail rather than draw a dial, and
``python -m chip_chat.harvest --landing … --max-age-days 8`` exits non-zero on
a corpus that has stopped being re-harvested. A staleness signal that only
renders is a staleness signal nobody reads on the week it matters.

Two documents are deliberately excluded from the measurement, and both would
otherwise flatter it:

``robots.txt``
    Re-read every 24 hours by the framework whether or not anything else is
    harvested, so it is always young. It is also not corpus — nothing in it
    ever reaches a citation.

Non-200 pointers
    A 404 that was cached is a record of an absence, not a document. Counting
    it as corpus would inflate the population, and RFC-001 section 08's
    citations can never point at one.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.release import Release

ROBOTS_PATH = "/robots.txt"
"""Suffix of a canonical URL that makes it framework traffic rather than corpus."""

DEFAULT_MAX_AGE_DAYS = 8
"""The staleness threshold, in days, and a deliberate eight rather than seven.

The re-harvest runs weekly. A threshold of exactly seven days fails on the
morning of every run that is an hour late, which trains people to ignore it.
Eight days means one missed run is a failure and a slow run is not.
"""

SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class DocumentAge:
    """One document, and when it was last confirmed current.

    Attributes:
        url: The canonical URL.
        harvested_at: The pointer's ``harvested_at``, which a conditional
            re-harvest moves forward without re-fetching the body.
    """

    url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class CorpusFreshness:
    """What the corpus's age is, as of one instant.

    Attributes:
        measured_at: When this was computed. Every age below is relative to it,
            so a report read a week later is still readable as a report about
            the moment it was taken.
        document_count: Corpus documents, excluding ``robots.txt`` and cached
            non-200 responses.
        oldest: The stalest document, or ``None`` if the corpus is empty. This
            is the freshness of the corpus.
        newest: The most recently confirmed document, or ``None``.
        last_release: The last release that completed, or ``None`` if none ever
            has. Note that a corpus can be young and have no release — the
            documents were fetched, the run that would have published them did
            not finish.
    """

    measured_at: datetime
    document_count: int
    oldest: DocumentAge | None
    newest: DocumentAge | None
    last_release: Release | None = None

    @property
    def max_age(self) -> timedelta | None:
        """Age of the stalest document, or ``None`` on an empty corpus."""
        if self.oldest is None:
            return None
        return self.measured_at - self.oldest.harvested_at

    @property
    def max_age_days(self) -> float | None:
        """:attr:`max_age` in days, or ``None`` on an empty corpus."""
        age = self.max_age
        return None if age is None else age.total_seconds() / SECONDS_PER_DAY

    @property
    def changed_last_release(self) -> int | None:
        """Documents that changed in the last completed run, or ``None``."""
        return None if self.last_release is None else self.last_release.changed

    def is_stale(self, max_age: timedelta) -> bool:
        """Return whether the corpus has stopped being re-harvested.

        An **empty** corpus counts as stale. That is the deliberate answer: a
        landing zone with nothing in it is not a fresh corpus, it is not a
        corpus, and a check that passed on one would pass on a machine where
        the harvest had never run at all — which is exactly the case it exists
        to catch.

        Args:
            max_age: How old the stalest document may be.

        Returns:
            ``True`` if the corpus is empty, or if its stalest document is
            older than ``max_age``.
        """
        age = self.max_age
        return age is None or age > max_age

    def as_dict(self) -> dict[str, Any]:
        """Return the freshness as a JSON-ready mapping.

        Returns:
            The three numbers issue #38 asks for, plus what is needed to act
            on them: which document is the oldest, and which run last
            published.
        """
        return {
            "measured_at": self.measured_at.isoformat(),
            "document_count": self.document_count,
            "oldest_harvested_at": (
                self.oldest.harvested_at.isoformat() if self.oldest else None
            ),
            "oldest_url": self.oldest.url if self.oldest else None,
            "newest_harvested_at": (
                self.newest.harvested_at.isoformat() if self.newest else None
            ),
            "max_age_days": (
                None if self.max_age_days is None else round(self.max_age_days, 3)
            ),
            "last_successful_harvest": (
                self.last_release.published_at.isoformat() if self.last_release else None
            ),
            "last_release_id": (self.last_release.run_id if self.last_release else None),
            "changed_last_release": self.changed_last_release,
        }

    def render(self, max_age: timedelta | None = None) -> str:
        """Return the freshness as lines a human reads without a schema.

        Args:
            max_age: The threshold to judge against, or ``None`` to report the
                numbers without a verdict.

        Returns:
            A short plain-text block, no trailing newline.
        """
        if self.oldest is None:
            body = ["Corpus freshness: EMPTY — no harvested documents in this store."]
        else:
            days = self.max_age_days or 0.0
            body = [
                f"Corpus freshness: {self.document_count} documents, "
                f"oldest {days:.1f} days old",
                f"  oldest       {self.oldest.harvested_at.isoformat()}  "
                f"{self.oldest.url}",
            ]
            if self.newest is not None:
                body.append(
                    f"  newest       {self.newest.harvested_at.isoformat()}  "
                    f"{self.newest.url}"
                )
        if self.last_release is None:
            body.append("  last release none — nothing has ever finished publishing")
        else:
            body.append(
                f"  last release {self.last_release.published_at.isoformat()}  "
                f"{self.last_release.run_id} "
                f"({self.last_release.changed} documents changed)"
            )
        if max_age is not None:
            verdict = "STALE" if self.is_stale(max_age) else "fresh"
            allowed = max_age.total_seconds() / SECONDS_PER_DAY
            body.append(f"  verdict      {verdict} (threshold {allowed:g} days)")
        return "\n".join(body)


def is_corpus(pointer: Mapping[str, Any]) -> bool:
    """Return whether a cache pointer describes a corpus document.

    Args:
        pointer: One decoded pointer, as
            :meth:`~chip_chat.harvest.cache.DocumentCache.pointers` yields it.

    Returns:
        ``False`` for ``robots.txt`` and for anything cached with a non-200
        status; ``True`` otherwise. See this module's docstring for why both
        would otherwise flatter the measurement.
    """
    url = str(pointer.get("requested_url", ""))
    if url.endswith(ROBOTS_PATH):
        return False
    return int(pointer.get("status_code", 0)) == 200


def ages(cache: DocumentCache) -> Iterable[DocumentAge]:
    """Yield one :class:`DocumentAge` per corpus document in ``cache``.

    Args:
        cache: The document cache to read.

    Yields:
        The ages, in whatever order the store lists its pointers.

    Raises:
        CacheCorruptError: If a pointer cannot be read.
    """
    for pointer in cache.pointers():
        if not is_corpus(pointer):
            continue
        yield DocumentAge(
            url=str(pointer["requested_url"]),
            harvested_at=datetime.fromisoformat(str(pointer["harvested_at"])),
        )


def read_freshness(
    cache: DocumentCache,
    *,
    now: datetime,
    release: Release | None = None,
) -> CorpusFreshness:
    """Measure the corpus's freshness as of ``now``.

    Args:
        cache: The document cache holding the corpus.
        now: The instant to measure against. Passed rather than read so that
            the number in a report and the number a test asserts come from the
            same place.
        release: The last completed release, if one is known. Supplying it is
            what turns two of issue #38's three numbers from unknown into
            reported.

    Returns:
        The freshness.

    Raises:
        ValueError: If ``now`` is naive.
        CacheCorruptError: If a pointer cannot be read.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    measured = sorted(ages(cache), key=lambda age: age.harvested_at)
    return CorpusFreshness(
        measured_at=now,
        document_count=len(measured),
        oldest=measured[0] if measured else None,
        newest=measured[-1] if measured else None,
        last_release=release,
    )
