"""The weekly re-harvest: refresh the corpus, diff it, publish or don't.

Issue #38 asks for a weekly job, a change report, an atomic downstream rebuild,
and visible freshness. This command is all four in one process, because they
are one thing: a run either produced a complete corpus, in which case it has a
report and it publishes, or it did not, in which case it has a report and it
publishes nothing.

    python -m chip_chat.harvest.sources.chipotle.reharvest --landing landing

What happens, in order:

1. **Snapshot.** Record every corpus document's digest, and every parsed
   table's rows, as they stand. This is the "before" the diff is against.
2. **Refresh.** Build all four datasets through the ordinary framework, with
   ``refresh=True``. Every document is asked about again; every one the source
   gives an ``ETag`` or ``Last-Modified`` for is asked about *conditionally*,
   so an unchanged page costs a 304 and no body.
3. **Stage.** Write the parsed tables under ``corpus/runs/<run_id>/parsed/``.
   Nothing outside that prefix is touched, so up to this point the live corpus
   is untouched no matter what happens.
4. **Diff and report.** Compare against the previous release at both levels,
   render the report, and write it beside the staged tables.
5. **Publish.** One write of ``corpus/current.json``. Only now is the new
   corpus the live one. A run that fell over at step 2 reaches step 4 with
   ``ok=false``, writes its report, and does not reach step 5.

**Politeness, stated for a scheduled job.** ``robots.txt`` is a 404 on both
``www.chipotle.com`` and ``services.chipotle.com`` — no rules published, which
the framework treats as allow-all — so nothing in the source constrains this.
A weekly job is a different profile from the one-off harvests of #19 to #22 and
the constraint has to come from us instead:

* Every request still goes through the framework's process-wide politeness
  gate. There is no second fetch path here, which is the point of building on
  cc-3np rather than beside it.
* The default ``--stores`` is deliberately lower than the harvest default. The
  policy dataset's store profiles are the bulk of the request count and the
  part that changes least; re-reading fifty of them weekly is fifty requests a
  week spent to learn that a restaurant is still where it was.
* Conditional revalidation means the *bytes* asked for fall to near zero on a
  quiet week even though the request count does not. The report prints both
  numbers, so the claim is checkable rather than asserted.
"""

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from chip_chat.harvest.analysis import AzureDocumentIntelligence
from chip_chat.harvest.blobs import BlobStore, LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.changes import (
    TableChange,
    TableSnapshot,
    changed_count,
    diff_documents,
    diff_tables,
    render_report,
    snapshot_documents,
    snapshot_tables,
)
from chip_chat.harvest.clock import Clock, SystemClock
from chip_chat.harvest.errors import HarvestError
from chip_chat.harvest.freshness import DEFAULT_MAX_AGE_DAYS, read_freshness
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.release import Release, ReleaseStore, run_id_for
from chip_chat.harvest.sources.chipotle import (
    nutrition_records,
    pdf_records,
    policy_records,
    records,
)
from chip_chat.harvest.sources.chipotle.config import HOME_URL
from chip_chat.harvest.sources.chipotle.datasets import (
    DATASETS,
    ENDPOINT_VARIABLE,
    DatasetBuilder,
)
from chip_chat.harvest.sources.chipotle.menu import DEFAULT_RESTAURANT_IDS
from chip_chat.harvest.transport import DEFAULT_CONTACT, HttpxTransport

DEFAULT_STORE_COUNT = 30
"""Stores the weekly run reads, against 50 for a one-off harvest.

Thirty is the floor issue #21 sets for the dataset to be meaningful, and it is
where a weekly job should sit: store profiles are the largest part of the
request count and the smallest part of what moves week to week. Raise it with
``--stores`` for a run that is specifically looking at the estate.
"""

MODULES = {
    "menu": records,
    "nutrition": nutrition_records,
    "policy": policy_records,
    "pdf": pdf_records,
}
"""Each dataset's records module, which owns both its prefix and its row keys."""


def staged_prefix(run_prefix: str, dataset: str) -> str:
    """Return where one dataset's tables are staged for one run.

    Args:
        run_prefix: The run's own prefix, from
            :meth:`~chip_chat.harvest.release.ReleaseStore.run_prefix`.
        dataset: One of :data:`~chip_chat.harvest.sources.chipotle.datasets.DATASETS`.

    Returns:
        The key prefix.
    """
    return f"{run_prefix}/parsed/{dataset}"


def table_keys() -> dict[str, dict[str, tuple[str, ...]]]:
    """Return every dataset's row identities, by dataset name.

    Returns:
        Dataset name to its ``TABLE_KEYS``.
    """
    return {name: dict(module.TABLE_KEYS) for name, module in MODULES.items()}


def snapshot_release(
    blobs: BlobStore, release: Release | None
) -> dict[str, dict[str, TableSnapshot]] | None:
    """Read the parsed tables of the previous release, if there was one.

    Args:
        blobs: The landing zone.
        release: The live release, or ``None``.

    Returns:
        Dataset name to table snapshots, or ``None`` if nothing has published
        yet — which a first run must be able to say, because "every row is new"
        and "no previous release" are different reports.

    Raises:
        ValueError: If a released table is not readable as JSON Lines.
    """
    if release is None:
        return None
    return {
        dataset: snapshot_tables(blobs, staged_prefix(release.prefix, dataset), keys)
        for dataset, keys in table_keys().items()
    }


def _stage(
    builder: DatasetBuilder, blobs: BlobStore, run_prefix: str
) -> dict[str, dict[str, Any]]:
    """Build every dataset and write it under this run's own prefix.

    Args:
        builder: The dataset builder.
        blobs: The landing zone.
        run_prefix: This run's prefix.

    Returns:
        Dataset name to its manifest.

    Raises:
        HarvestError: If a dataset could not be built. Nothing outside
            ``run_prefix`` has been written when this propagates.
    """
    manifests: dict[str, dict[str, Any]] = {}
    for name in DATASETS:
        dataset = builder.dataset(name)
        dataset.write(blobs, staged_prefix(run_prefix, name))
        manifests[name] = dict(dataset.manifest())
    return manifests


def run(
    blobs: BlobStore,
    harvester: Harvester,
    analyzer: AzureDocumentIntelligence | None,
    *,
    clock: Clock | None = None,
    restaurants: Sequence[str] | None = None,
    home_url: str = HOME_URL,
    store_count: int = DEFAULT_STORE_COUNT,
    max_age: timedelta | None = None,
) -> tuple[dict[str, Any], str]:
    """Re-harvest, diff, and publish if and only if the whole run succeeded.

    Args:
        blobs: The landing zone. Both the raw cache and the releases live here.
        harvester: The framework instance. Its politeness gate and its cache
            are the ones the one-off harvests use.
        analyzer: Document Intelligence, or ``None`` when no PDF can be read.
        clock: Source of time. Defaults to the system clock.
        restaurants: Restaurants to price the catalogue at.
        home_url: The page the API configuration is read from.
        store_count: How many stores the policy dataset reads.
        max_age: Staleness threshold to judge the resulting corpus against, or
            ``None`` to report without a verdict.

    Returns:
        ``(record, report)`` — the run's record as written, and the rendered
        Markdown change report. ``record["ok"]`` is the answer to "did this run
        publish", and ``record["stale"]`` to "is the corpus current". Both are
        returned rather than raised because a failed weekly run must still
        produce its artefacts.
    """
    ticker = clock if clock is not None else SystemClock()
    started_at = ticker.now()
    run_id = run_id_for(started_at)
    releases = ReleaseStore(blobs)
    cache = DocumentCache(blobs)

    previous = releases.current()
    documents_before = snapshot_documents(cache)
    tables_before = snapshot_release(blobs, previous)

    builder = DatasetBuilder(
        harvester,
        analyzer,
        blobs,
        restaurants=list(restaurants) if restaurants else None,
        home_url=home_url,
        store_count=store_count,
        refresh=True,
    )

    failure: str | None = None
    manifests: dict[str, dict[str, Any]] = {}
    tables: tuple[TableChange, ...] = ()
    try:
        manifests = _stage(builder, blobs, releases.run_prefix(run_id))
    except HarvestError as error:
        failure = f"{type(error).__name__}: {error}"
    else:
        tables = tuple(
            change
            for dataset, keys in table_keys().items()
            for change in diff_tables(
                None if tables_before is None else tables_before.get(dataset),
                snapshot_tables(
                    blobs, staged_prefix(releases.run_prefix(run_id), dataset), keys
                ),
            )
        )

    documents = diff_documents(documents_before, snapshot_documents(cache))
    changed = changed_count(documents)
    finished_at = ticker.now()
    freshness = read_freshness(cache, now=finished_at, release=previous)
    ok = failure is None

    report = render_report(
        run_id=run_id,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        ok=ok,
        documents=documents,
        tables=tables,
        requests_made=harvester.requests_made,
        revalidations=harvester.revalidations,
        bytes_fetched=harvester.bytes_fetched,
        freshness=freshness.render(max_age),
        failures={"run": failure} if failure else None,
    )
    report_key = releases.write_report(run_id, report)

    record: dict[str, Any] = {
        "run_id": run_id,
        "ok": ok,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "previous_release": previous.run_id if previous else None,
        "requests_made": harvester.requests_made,
        "revalidations": harvester.revalidations,
        "bytes_fetched": harvester.bytes_fetched,
        "documents": len(documents),
        "changed": changed,
        "document_changes": [
            change.as_dict() for change in documents if change.status != "unchanged"
        ],
        "table_changes": [change.as_dict() for change in tables if not change.is_quiet],
        "manifests": manifests,
        "report_key": report_key,
        "failure": failure,
    }
    release = (
        Release(
            run_id=run_id,
            published_at=finished_at,
            prefix=releases.run_prefix(run_id),
            documents=len(documents),
            changed=changed,
            report_key=report_key,
        )
        if ok
        else None
    )
    # `release or previous` rather than `previous`: on a run that is about to
    # publish, "last successful harvest" is this run. On one that is not, it is
    # whatever was live before, which is the honest answer and is also what a
    # reader of the failed run's record needs to know.
    freshness = read_freshness(cache, now=finished_at, release=release or previous)
    record["freshness"] = freshness.as_dict()
    record["stale"] = max_age is not None and freshness.is_stale(max_age)

    # Record first, pointer second, always. `publish` refuses a pointer at a
    # run whose record is not on disk, and this is the ordering that makes that
    # check pass for the right reason.
    releases.write_record(run_id, record)
    if release is not None:
        releases.publish(release)
    return record, report


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so tests can exercise it without a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.harvest.sources.chipotle.reharvest",
        description=(
            "Re-harvest the Chipotle corpus, report what changed, and publish "
            "the result only if the whole run succeeded."
        ),
    )
    parser.add_argument(
        "--landing",
        type=Path,
        required=True,
        help="Directory the raw and parsed blobs live in.",
    )
    parser.add_argument(
        "--restaurant",
        dest="restaurants",
        action="append",
        metavar="ID",
        help=(
            "A restaurant to price the catalogue at. Repeatable. Defaults to "
            f"{', '.join(DEFAULT_RESTAURANT_IDS)}."
        ),
    )
    parser.add_argument(
        "--stores",
        type=int,
        default=DEFAULT_STORE_COUNT,
        metavar="N",
        help=(
            "How many stores the policy dataset reads. Defaults to "
            f"{DEFAULT_STORE_COUNT} for a weekly run; issue #21 requires at "
            "least 30."
        ),
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=DEFAULT_MAX_AGE_DAYS,
        metavar="N",
        help=(
            "Exit non-zero if the corpus is still older than N days after this "
            f"run. Defaults to {DEFAULT_MAX_AGE_DAYS}. Pass 0 to disable the "
            "check and only report."
        ),
    )
    parser.add_argument(
        "--document-intelligence-endpoint",
        default=None,
        metavar="URL",
        help=(
            "Document Intelligence account to read PDFs with. Defaults to "
            f"${ENDPOINT_VARIABLE}. Only needed if a PDF is actually found."
        ),
    )
    parser.add_argument(
        "--home-url",
        default=HOME_URL,
        help=f"Page the API configuration is read from. Defaults to {HOME_URL}.",
    )
    parser.add_argument(
        "--contact",
        default=DEFAULT_CONTACT,
        help="Address a site owner can reach you at; goes in the User-Agent.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Also write the change report here, outside the landing zone. This "
            "is how a scheduled job publishes it where a human will see it — a "
            "build artefact, or $GITHUB_STEP_SUMMARY."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the weekly re-harvest.

    Args:
        argv: Command line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        Zero if the run published and the corpus is fresh; one if the harvest
        failed; two if it succeeded but the corpus is still stale, which is a
        different problem with a different fix and should not be reported as
        the same exit status.
    """
    args = build_parser().parse_args(argv)
    blobs = LocalBlobStore(args.landing)
    max_age = timedelta(days=args.max_age_days) if args.max_age_days > 0 else None

    with (
        Harvester(blobs, HttpxTransport(), contact=args.contact) as harvester,
        _analyzer(args.document_intelligence_endpoint) as analyzer,
    ):
        record, report = run(
            blobs,
            harvester,
            analyzer,
            restaurants=args.restaurants,
            home_url=args.home_url,
            store_count=args.stores,
            max_age=max_age,
        )

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")

    print(json.dumps(_summary(record), indent=2, sort_keys=True))
    if not record["ok"]:
        print(f"re-harvest failed: {record['failure']}", file=sys.stderr)
        print(
            f"nothing published; the live corpus is still "
            f"{record['previous_release'] or 'unpublished'}",
            file=sys.stderr,
        )
        return 1
    if record["stale"]:
        print(
            f"published {record['run_id']}, but the corpus is still older than "
            f"{args.max_age_days:g} days",
            file=sys.stderr,
        )
        return 2
    return 0


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the part of a run record that belongs on a terminal."""
    return {
        "run_id": record["run_id"],
        "ok": record["ok"],
        "documents": record["documents"],
        "changed": record["changed"],
        "requests_made": record["requests_made"],
        "revalidations": record["revalidations"],
        "bytes_fetched": record["bytes_fetched"],
        "report_key": record["report_key"],
        "freshness": record["freshness"],
        "stale": record["stale"],
    }


@contextmanager
def _analyzer(endpoint: str | None) -> Iterator[AzureDocumentIntelligence | None]:
    """Yield an analyzer, or ``None`` when there is nowhere to send a document.

    Constructed but not authenticated: the credential is resolved on the first
    call, so a run that finds no PDF — which is every run against Chipotle
    today — never asks Azure for a token.
    """
    resolved = endpoint or os.environ.get(ENDPOINT_VARIABLE, "")
    if not resolved.strip():
        yield None
        return
    with AzureDocumentIntelligence(resolved) as analyzer:
        yield analyzer


if __name__ == "__main__":
    raise SystemExit(main())
