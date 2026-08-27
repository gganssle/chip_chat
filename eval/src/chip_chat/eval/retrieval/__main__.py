"""``python -m chip_chat.eval.retrieval`` — check the set, sweep it free, or measure it.

Three modes, and the two cheap ones are cheap for different reasons.

``--check`` loads the manifest, refuses one that contradicts itself, and reports
which of issue #50's scope clauses the set meets. It reads no corpus and calls
nothing. This is the one to run after adding a question, and the one that belongs
in CI.

``--offline --chunks <path>`` additionally resolves every label against a chunk
export and sweeps all four arms against
:class:`~chip_chat.eval.retrieval.testing.OfflineIndex`. It still calls nothing
and costs nothing. **Its ablation numbers are not evidence about retrieval** —
that module's docstring says exactly why, and the rendered report says so in its
first paragraph — but its *resolution* is the real thing: which of the set's
labels name a place the committed corpus actually holds, which is #50's
chunking-regression check.

Without either flag it runs against the live alias and writes the baseline --
``make retrieval-baseline``, and what produced the committed one. That sweep
**spends the month's semantic allowance**, one request per question for every arm
that reranks, so the count is printed before anything is sent and ``--yes`` is
required to send it. On the Free tier the allowance is a hard stop rather than an
overage: past 1,000 the API returns a billing error.

``--from-index`` resolves the labels against the corpus the live alias is
*serving*, read back off it, rather than against a chunk export under the landing
zone. It costs no semantic request and it is the stricter reading: what a
resolution answers is *can the retriever return this place*, which is a question
about what the index holds. Where the two agree it changes nothing; where they
disagree -- a document the builder rejected, a rebuild that landed short --
resolving against the export would score the retriever as missing a passage
nothing could have returned. The measured baseline found one: the live index is
missing an FAQ entry the corpus export holds, and the question it answers is
scored over the two places that remain rather than penalised for the third.

.. code-block:: console

    $ python -m chip_chat.eval.retrieval --check
    $ python -m chip_chat.eval.retrieval --offline --chunks search/tests/fixtures
    $ AZURE_SEARCH_ENDPOINT=... python -m chip_chat.eval.retrieval \\
          --from-index --landing landing --yes --out eval/retrieval/BASELINE.md
"""

import argparse
import sys
from pathlib import Path

from chip_chat.eval.retrieval.configurations import (
    ABLATION,
    SERVING,
    Configuration,
    semantic_requests,
)
from chip_chat.eval.retrieval.corpus import Resolution, from_index, resolve
from chip_chat.eval.retrieval.coverage import MINIMUM_QUESTIONS, coverage
from chip_chat.eval.retrieval.questions import (
    QuestionError,
    RetrievalSet,
)
from chip_chat.eval.retrieval.report import build_report, render
from chip_chat.eval.retrieval.run import RetrieverSource, run_sweep
from chip_chat.eval.retrieval.testing import EVALUATES_FILTERS, OfflineIndex
from chip_chat.search import corpus as corpus_module
from chip_chat.search.allowance import FileAllowanceStore, SemanticAllowance
from chip_chat.search.client import (
    EntraToken,
    HttpSearchService,
    endpoint_from_env,
    pooled_client,
)
from chip_chat.search.errors import SearchError
from chip_chat.search.query import TOP
from chip_chat.search.retrieve import PROVISIONAL_RERANKER_FLOOR, Retriever
from chip_chat.search.schema import ALIAS

DEFAULT_MANIFEST = Path("eval/retrieval/questions.json")
OFFLINE_RUN_ID = "offline"
ALLOWANCE_FILE = "semantic-allowance.json"
"""The same file ``make search-retrieve`` counts into.

Deliberately the same one. A sweep and a hand-run query spend the same thousand
requests, and two counters would each be right about half of them.
"""

SEARCH_SCOPE = "https://search.azure.com/.default"


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where the set is not yet the
        set the ticket asks for, where a run could not be started, or where a
        sweep found a constraint breach. A ``--check`` that finds an incomplete
        set exits non-zero deliberately: an under-covered set is a build failure,
        or it stays under-covered.
    """
    args = _parser().parse_args(argv)
    try:
        questions = RetrievalSet.load(args.manifest)
    except QuestionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        return _check(questions)

    arms = _arms(args)
    if args.offline:
        return _offline(questions, args, arms)
    return _measured(questions, args, arms)


def _check(questions: RetrievalSet) -> int:
    """Report the set's coverage without reading a corpus or calling anything."""
    cover = coverage(questions)
    print(f"{cover.questions} labeled questions (need {MINIMUM_QUESTIONS})")
    for requirement, ids in cover.met:
        print(f"  ok      {requirement.name}: {len(ids)}/{requirement.minimum}")
    for requirement, ids in cover.unmet:
        print(
            f"  MISSING {requirement.name}: {len(ids)}/{requirement.minimum} "
            f"({requirement.source})"
        )
    return 0 if cover.complete else 1


def _offline(
    questions: RetrievalSet, args: argparse.Namespace, arms: tuple[Configuration, ...]
) -> int:
    """Resolve the labels and sweep an in-memory index. Free, and honest about it."""
    if args.chunks is None:
        print(
            "error: --offline needs a chunk export to build an index from; pass --chunks",
            file=sys.stderr,
        )
        return 1
    try:
        chunk_set = corpus_module.from_path(args.chunks, args.run_id or OFFLINE_RUN_ID)
    except corpus_module.CorpusError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    index = OfflineIndex(chunk_set, alias=args.alias)
    retriever = Retriever(
        index,
        alias=args.alias,
        top=args.top,
        floor=args.floor,
    )
    resolution = resolve(questions, chunk_set)
    answers = run_sweep(
        questions,
        RetrieverSource(retriever, name=f"offline index over {chunk_set.origin}"),
        configurations=arms,
        only=args.only,
    )
    document = render(
        build_report(
            questions,
            resolution,
            answers,
            arms,
            source=f"offline index over {chunk_set.origin}",
            measured=False,
            floor=args.floor,
            evaluates_filters=EVALUATES_FILTERS,
        )
    )
    return _emit(document, args, resolution, complete=args.complete)


def _measured(
    questions: RetrievalSet, args: argparse.Namespace, arms: tuple[Configuration, ...]
) -> int:
    """Sweep the live alias. Spends the month's semantic allowance."""
    from_release = args.chunks is None and not args.from_index
    complete = args.complete or from_release
    try:
        chunk_set = (
            corpus_module.from_release(args.landing)
            if from_release
            else (
                None
                if args.from_index
                else corpus_module.from_path(args.chunks, args.run_id)
            )
        )
    except corpus_module.CorpusError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    cost = semantic_requests(arms, len(questions))
    if cost and not args.yes:
        print(
            f"this sweep would spend {cost} of the month's 1,000 semantic "
            f"requests ({len(questions)} questions x the arms that rerank). "
            f"Pass --yes to run it, or --arms serving to run one arm.",
            file=sys.stderr,
        )
        return 1

    client = pooled_client(60.0)
    try:
        service = HttpSearchService(
            args.endpoint or endpoint_from_env(), client, EntraToken(SEARCH_SCOPE), 1000
        )
        retriever = Retriever(
            service,
            alias=args.alias,
            top=args.top,
            floor=args.floor,
            allowance=SemanticAllowance(
                store=FileAllowanceStore(args.landing / ALLOWANCE_FILE)
            ),
        )
        if chunk_set is None:
            chunk_set = from_index(
                service, args.alias, args.run_id or _live_index(service, args.alias)
            )
        resolution = resolve(questions, chunk_set)
        answers = run_sweep(
            questions,
            RetrieverSource(retriever, name=args.alias),
            configurations=arms,
            only=args.only,
        )
    except SearchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()

    document = render(
        build_report(
            questions,
            resolution,
            answers,
            arms,
            source=args.alias,
            measured=True,
            floor=args.floor,
        )
    )
    return _emit(document, args, resolution, complete=complete)


def _emit(
    document: str,
    args: argparse.Namespace,
    resolution: Resolution,
    *,
    complete: bool,
) -> int:
    """Write the report, and decide the exit status.

    Non-zero on **one** thing: a label that names nothing in a corpus that was
    supposed to hold everything. That is #50's fourth acceptance criterion, and
    it is the only failure here a person can act on without reading the whole
    document — every other number is a measurement, and gating on a measurement
    is how a gate gets switched off.

    ``complete`` is what keeps that gate meaningful, and the distinction is
    real rather than a let-out. A corpus read through the release pointer is
    the corpus the index was built from, so a label naming nothing in it names
    nothing anywhere and the run fails. A corpus read from ``--chunks`` is
    whatever somebody pointed at — the committed 31-chunk fixture is a *slice*
    of the published pages, and two of this set's labels name places that slice
    has never held. Failing on those would mean the free sweep is red forever,
    which is the same as having no gate at all.

    The unresolved labels are listed in the report either way. That listing,
    diffed against the committed baseline, is what turns "this corpus never had
    it" into "this corpus stopped having it".
    """
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    unresolved = resolution.unresolved()
    if not unresolved:
        return 0
    named = ", ".join(place.question_id for place in unresolved)
    if not complete:
        print(
            f"note: {len(unresolved)} label(s) name nothing in "
            f"{resolution.run_id}, which is a corpus nobody promised was "
            f"complete: {named}. They are unscored in the report above. Compare "
            f"it with the committed baseline to tell a slice from a regression.",
            file=sys.stderr,
        )
        return 0
    print(
        f"error: {len(unresolved)} label(s) name nothing in the live corpus "
        f"{resolution.run_id}: {named}",
        file=sys.stderr,
    )
    return 1


def _live_index(service: HttpSearchService, alias: str) -> str:
    """What the alias is serving, as the corpus's name in the report.

    An index name here rather than a release id, and that is the honest label:
    the corpus being resolved against is the one *this index holds*, which is
    what a measured sweep can return passages from. Where a landing zone is
    available, ``--landing`` names the release instead and means the same thing.
    """
    return service.alias_target(alias) or alias


def _arms(args: argparse.Namespace) -> tuple[Configuration, ...]:
    """The configurations to sweep."""
    return (SERVING,) if args.arms == "serving" else ABLATION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.retrieval",
        description="Check, sweep or measure the labeled retrieval set.",
    )
    parser.add_argument(
        "--set",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the question manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report coverage without reading a corpus",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="resolve the labels and sweep an in-memory index over --chunks",
    )
    parser.add_argument(
        "--chunks",
        type=Path,
        help="a .jsonl chunk export, or a directory of them",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="what to call the corpus --chunks holds",
    )
    parser.add_argument(
        "--landing", type=Path, default=Path("landing"), help="the landing zone root"
    )
    parser.add_argument("--alias", default=ALIAS, help="the alias to query")
    parser.add_argument(
        "--endpoint",
        default="",
        help="the search service endpoint (default: AZURE_SEARCH_ENDPOINT)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=TOP,
        help=f"passages to ask for (default: {TOP}); the metric is taken at 3",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=PROVISIONAL_RERANKER_FLOOR,
        help=(
            "the reranker score at or above which a result is grounded "
            f"(default: {PROVISIONAL_RERANKER_FLOOR}, which is unmeasured)"
        ),
    )
    parser.add_argument(
        "--arms",
        choices=("ablation", "serving"),
        default="ablation",
        help="sweep all four configurations, or only the one production runs",
    )
    parser.add_argument("--only", nargs="+", help="run only these question ids")
    parser.add_argument(
        "--from-index",
        action="store_true",
        help=(
            "resolve the labels against the corpus the live alias is serving, "
            "read back off it, rather than against a chunk export. Costs no "
            "semantic request"
        ),
    )
    parser.add_argument(
        "--complete",
        action="store_true",
        help=(
            "treat --chunks as the whole corpus, so a label naming nothing in "
            "it fails the run. Implied when the corpus is read through the "
            "release pointer"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="spend the semantic allowance a measured sweep needs",
    )
    parser.add_argument(
        "--out", type=Path, help="write the report here instead of to stdout"
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
