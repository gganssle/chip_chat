"""What a version is, and why it is a hash rather than a number.

#72's whole argument is one line of the ticket: *adding entries creates a new
version rather than mutating the old one, so a score from three weeks ago still
means something*. That property has to survive people, so it is not a
convention here -- it is arithmetic.

**The version is the content.** :func:`fingerprint` is a digest over the
entries' own digests, so two builds of the same entries produce the same version
and a change to any entry produces a different one. Nobody increments it, nobody
forgets to increment it, and nobody can quietly edit a case and leave the
version where it was. An ordinal would read better on a chart axis and would be
wrong exactly when it mattered.

**Every uploaded row carries it.** :func:`rows` stamps the version on each row
on the way out, so a published example says which build put it there. Combined
with the committed ``eval/dataset/DATASET.json``, that is what makes an old
score legible: the score names a version, the version names a build, and the
build is a commit.

**Order is part of it.** A set whose cases were reordered is a set a reader
meets differently, and calling that the same version would be a claim that
reading order does not matter. It costs nothing to be strict here, and the
strictness raises no false alarms: nothing reorders these files by itself.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from chip_chat.eval.dataset.entries import DIGEST_LENGTH, DatasetEntry, digest_of

__all__ = ["VERSION_COLUMN", "VERSION_LENGTH", "fingerprint", "rows"]

VERSION_COLUMN: Final = "dataset_version"
"""The column every uploaded row carries the version in."""

VERSION_LENGTH: Final = DIGEST_LENGTH
"""Hex characters of the version. The same width as an entry's digest."""


def fingerprint(entries: Sequence[DatasetEntry]) -> str:
    """The version these entries are, as :data:`VERSION_LENGTH` hex characters.

    Args:
        entries: The dataset's entries, in build order.

    Returns:
        The version identifier. Stable across processes and machines: a digest
        of digests of canonical JSON, with nothing in it a clock or a hash seed
        could move.
    """
    return digest_of("\n".join(entry.digest for entry in entries))


def rows(
    entries: Sequence[DatasetEntry], version: str
) -> tuple[Mapping[str, str | int | bool], ...]:
    """The rows to upload: every entry, stamped with the version.

    Args:
        entries: The entries to upload -- the whole dataset on a first publish,
            and only the new ones on a later one.
        version: What :func:`fingerprint` said the whole build is. A later
            version's new rows carry the build they arrived in, which is the
            question anybody looking at one actually has.

    Returns:
        One row per entry, each carrying :data:`VERSION_COLUMN`.
    """
    return tuple({**entry.row(), VERSION_COLUMN: version} for entry in entries)
