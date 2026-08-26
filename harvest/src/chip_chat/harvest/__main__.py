"""Ask a landing zone how fresh its corpus is, and fail if it has stopped moving.

    python -m chip_chat.harvest --landing landing
    python -m chip_chat.harvest --landing landing --max-age-days 8 --json

Source-agnostic on purpose: it reads the fetch-once cache and the release
pointer, both of which belong to the framework rather than to Chipotle, so a
second source added later is measured by this same command without touching it.

It lives here rather than in :mod:`chip_chat.harvest.freshness` because that
module is re-exported from the package's ``__init__``, and a module that is both
imported at package load and run with ``-m`` is executed twice under two names
— Python says so with a ``RuntimeWarning`` and is right to.

The check is the enforcement half of issue #38. ``--max-age-days`` makes a stale
corpus an exit status rather than a line of output, which is the difference
between a freshness signal and a freshness dashboard.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.freshness import DEFAULT_MAX_AGE_DAYS, read_freshness
from chip_chat.harvest.release import read_current


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so tests can exercise it without a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.harvest",
        description=(
            "Report how old the harvested corpus is, and fail if it has "
            "stopped being re-harvested."
        ),
    )
    parser.add_argument(
        "--landing",
        type=Path,
        required=True,
        help="Directory the raw and parsed blobs live in.",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Exit non-zero if the oldest document is older than N days, or if "
            f"the corpus is empty. Pass {DEFAULT_MAX_AGE_DAYS} to match the "
            "weekly schedule. Without it this command only reports."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the metrics as JSON instead of as prose.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Report corpus freshness, and enforce a threshold if one was given.

    Args:
        argv: Command line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        Zero if the corpus is fresh or no threshold was given; one if it is
        stale.
    """
    args = build_parser().parse_args(argv)
    blobs = LocalBlobStore(args.landing)
    freshness = read_freshness(
        DocumentCache(blobs), now=datetime.now(UTC), release=read_current(blobs)
    )
    max_age = None if args.max_age_days is None else timedelta(days=args.max_age_days)

    if args.json:
        print(json.dumps(freshness.as_dict(), indent=2, sort_keys=True))
    else:
        print(freshness.render(max_age))

    if max_age is not None and freshness.is_stale(max_age):
        print(
            f"corpus is stale: nothing has re-harvested it within "
            f"{args.max_age_days:g} days",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
