"""``python -m chip_chat.search`` — build, inspect, roll back, verify, ask.

    schema     print the index definition, with no network and no credential
    status     what the service holds and what the alias serves
    build      build a new index from the live corpus release and swap to it
    rollback   point the alias back at the index before this one
    verify     hold the live service to #48.3 and #48.4
    retrieve   ask the live alias a question and print what came back
    vector-arm drive hybrid queries until the Free tier drops the vector half

``vector-arm`` is ``chip-wez``'s, and it is the only command here that exists to
reproduce a defect rather than to run the product. It sends a burst of hybrid
queries through the ordinary retriever and reports, per query, what
:mod:`chip_chat.search.fusion` read off the fused scores — so a run of it is the
evidence that the detector fires against the live service and a measurement of
the rate on the day. It costs no semantic requests: reranking is off throughout.

``retrieve`` is #49's, and it is the cheapest way to see the whole knowledge
lane work: it prints the passages, every score that ranked them, the citation on
each one, and whether the semantic reranker was used or the month's allowance
had already been spent. ``--no-rerank`` runs the degrade path on purpose, which
is the only way to exercise it without waiting for a ceiling nobody wants to
reach. It needs no embedding deployment and no vectorizer key: the *index* holds
the vectorizer, so a query is text.

``schema`` is the one worth knowing about. The index definition is a pure
function of the chunk schema and the embedding deployment, so it can be printed
and read — and diffed against the last one — before anything is created. On a
tier with three indexes and a corpus that is rebuilt rather than altered, being
able to review the definition for free is worth more than it would be elsewhere.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from chip_chat.search import build as build_module
from chip_chat.search import corpus, schema, verify
from chip_chat.search import query as query_module
from chip_chat.search.allowance import FileAllowanceStore, SemanticAllowance
from chip_chat.search.client import (
    SEARCH_SCOPE,
    UPLOAD_BATCH_LIMIT,
    EntraToken,
    HttpSearchService,
    endpoint_from_env,
    pooled_client,
)
from chip_chat.search.embedding import (
    COGNITIVE_SERVICES_SCOPE,
    EmbeddingDeployment,
    HttpEmbedder,
)
from chip_chat.search.errors import SearchError
from chip_chat.search.fusion import SINGLE_RANKER_CEILING
from chip_chat.search.retrieve import Retriever

VECTORIZER_KEY_VARIABLE = "CHIP_CHAT_SEARCH_VECTORIZER_KEY"
"""A Foundry account key, handed to the *search service* so it can embed queries.

Deliberately not ``CHIP_CHAT_FOUNDRY_API_KEY``, which
``chip_chat.agent.foundry`` documents as a development escape hatch whose
presence in a deployed environment means something has gone wrong. This one is
the opposite: it is required in a deployed environment, because the Free tier
gives the search service no managed identity to reach Azure OpenAI with, and its
absence is what turns query-time vectorization off. Two intents, two names.

It belongs in Key Vault. ``make search-build`` reads it out with ``az keyvault
secret show`` rather than keeping it anywhere on disk.
"""


def _chunks(arguments: argparse.Namespace) -> corpus.ChunkSet:
    if arguments.chunks:
        if not arguments.run_id:
            raise SystemExit(
                "--chunks needs --run-id: the index is named after a corpus "
                "release, and a directory name is not one"
            )
        return corpus.from_path(Path(arguments.chunks), arguments.run_id)
    return corpus.from_release(Path(arguments.landing))


PLACEHOLDER_ENDPOINT = "https://aif-example.cognitiveservices.azure.com/"
"""Stands in for the Foundry account when ``schema`` is run with no environment.

Only the vectorizer's ``resourceUri`` differs, so a definition printed with this
in it is the definition that would be created, minus one hostname.
"""

VERIFY_BATCH = 10
"""Documents per upload request while verifying, against a default of 1,000.

Small so that a failed build can fail *halfway*. The service rejects a malformed
key by failing the whole request it is in, so at the default this corpus is one
request, the failed index ends up empty, and #48.4's "a partial harvest" is not
what was demonstrated. Ten makes the load several requests, of which the earlier
ones are really in the index when the later one dies.
"""


ALLOWANCE_FILE = "semantic-allowance.json"
"""Where ``retrieve`` keeps its count of the month's semantic requests.

Under the landing root, which is already gitignored and already the directory
this repository keeps run-scoped state in. A file rather than nothing because
the whole point of the counter is that it survives the process: a CLI that
forgot on every invocation would be a counter of one.
"""


VECTOR_ARM_PROBES = 40
"""Hybrid queries ``vector-arm`` sends unless told otherwise.

Chosen off the measurement rather than for round numbers.
``docs/retrieval.md`` §9 found that a rested service answers roughly the first
twenty vector queries and then mostly stops, so a probe of ten would sample only
the healthy prefix and report a service that works. Forty crosses the knee, and
the run prints its own first-half and second-half rates so the reader can see
whether it did on the day.
"""

VECTOR_ARM_QUESTIONS: tuple[str, ...] = (
    "what is in a chicken burrito bowl",
    "how do rewards points work",
    "which items are marked as containing dairy",
    "how many calories are in a steak burrito",
    "can I cancel an order after placing it",
    "what comes on a lifestyle bowl",
    "is the sofritas vegan",
    "what are the ingredients in barbacoa",
)
"""Questions ``vector-arm`` rotates through. Eight, and rotated rather than
repeated, because a single question asked forty times would let a response cache
answer thirty-nine of them and report a service that never degraded."""


def _vector_arm(retriever: Retriever, repeat: int) -> dict[str, object]:
    """Send ``repeat`` hybrid queries and report what the vector half did.

    The point of this command is that it is the *product's* reading being
    exercised and not a second one written for the report: every probe goes
    through :meth:`chip_chat.search.retrieve.Retriever.search` and the verdict
    printed is :attr:`chip_chat.search.retrieve.Retrieval.vector_arm`. A probe
    that computed the tell itself would be a test of the probe.

    Reranking is off throughout, so a forty-query run spends **none** of the
    month's 1,000 semantic requests. That costs nothing that matters here: the
    fused ``@search.score`` the reading is taken from is the same number on both
    paths — verified against the live alias, where a reranked response carried
    two-ranker scores at ranks four and five under a single-ranker top hit.

    Args:
        retriever: The retriever to probe, pointed at the live alias.
        repeat: How many queries to send.

    Returns:
        A JSON-ready summary: the per-arm counts, the two halves of the run
        separately, and one line per probe.
    """
    probes: list[dict[str, object]] = []
    for index in range(repeat):
        question = VECTOR_ARM_QUESTIONS[index % len(VECTOR_ARM_QUESTIONS)]
        result = retriever.search(question, rerank=False)
        probes.append(
            {
                "n": index + 1,
                "query": question,
                "vector_arm": result.vector_arm.value,
                "returned": len(result.passages),
                "top_score": round(
                    max((p.score for p in result.passages), default=0.0), 6
                ),
                "confidence": result.confidence.value,
            }
        )
    dropped = [probe for probe in probes if probe["vector_arm"] == "dropped"]
    half = len(probes) // 2
    return {
        "alias": retriever.alias,
        "queries": len(probes),
        "single_ranker_ceiling": round(SINGLE_RANKER_CEILING, 6),
        "dropped": len(dropped),
        "dropped_rate": round(len(dropped) / len(probes), 3) if probes else None,
        "dropped_first_half": sum(
            1 for probe in probes[:half] if probe["vector_arm"] == "dropped"
        ),
        "dropped_second_half": sum(
            1 for probe in probes[half:] if probe["vector_arm"] == "dropped"
        ),
        "probes": probes,
    }


def _service(
    arguments: argparse.Namespace, batch: int = UPLOAD_BATCH_LIMIT
) -> tuple[HttpSearchService, object]:
    client = pooled_client(60.0)
    endpoint = arguments.endpoint or endpoint_from_env()
    return (
        HttpSearchService(endpoint, client, EntraToken(SEARCH_SCOPE), batch),
        client,
    )


def _embedder(client: object, deployment: EmbeddingDeployment) -> HttpEmbedder:
    return HttpEmbedder(deployment, client, EntraToken(COGNITIVE_SERVICES_SCOPE))


def _vectorizer_key(arguments: argparse.Namespace) -> str | None:
    if arguments.no_vectorizer:
        return None
    key = os.environ.get(VECTORIZER_KEY_VARIABLE, "").strip()
    if not key:
        raise SystemExit(
            f"{VECTORIZER_KEY_VARIABLE} is not set, so the index would be built "
            f"with no query-time vectorizer and every caller would have to "
            f"embed its own queries. Set it, or pass --no-vectorizer to say "
            f"that is what you meant. See docs/retrieval-index.md."
        )
    return key


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit status. ``0`` on success, ``1`` on any refusal, and
        ``2`` when a verification ran and its answer was no — the same three-way
        distinction ``make freshness`` draws, and for the same reason: "it did
        not run" and "it ran and failed" are different problems.
    """
    parser = argparse.ArgumentParser(prog="python -m chip_chat.search")
    parser.add_argument(
        "command",
        choices=(
            "schema",
            "status",
            "build",
            "rollback",
            "verify",
            "retrieve",
            "vector-arm",
        ),
    )
    parser.add_argument("--landing", default="landing", help="the landing zone root")
    parser.add_argument("--chunks", default="", help="read chunks from here instead")
    parser.add_argument("--run-id", default="", help="name the build after this release")
    parser.add_argument("--alias", default=schema.ALIAS)
    parser.add_argument("--endpoint", default="", help="AZURE_SEARCH_ENDPOINT")
    parser.add_argument(
        "--no-swap", action="store_true", help="build and verify without going live"
    )
    parser.add_argument(
        "--no-vectorizer",
        action="store_true",
        help="build an index the service cannot embed queries for",
    )
    parser.add_argument("--query", default="", help="the question, for `retrieve`")
    parser.add_argument(
        "--top", type=int, default=query_module.TOP, help="passages to return"
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="run `retrieve` on the degrade path, without the semantic ranker",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=VECTOR_ARM_PROBES,
        help="hybrid queries to send, for `vector-arm`",
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "retrieve" and not arguments.query:
        # Before anything reaches for a credential: "you forgot the question"
        # and "your environment is not set up" are different problems, and the
        # second is a much longer afternoon than the first.
        raise SystemExit("retrieve needs --query")

    try:
        if arguments.command == "schema":
            # No credential, no network, and deliberately no Terraform: the
            # index definition is a pure function of the chunk schema, and the
            # only thing the endpoint decides is one URL inside the vectorizer.
            # Reading the definition before creating an index is a habit worth
            # keeping cheap, on a tier that allows three of them.
            try:
                deployment = EmbeddingDeployment.from_env()
            except SearchError:
                deployment = EmbeddingDeployment(endpoint=PLACEHOLDER_ENDPOINT)
            print(
                json.dumps(
                    schema.index(
                        schema.index_name(arguments.run_id or "20260101T000000Z"),
                        deployment,
                        None if arguments.no_vectorizer else "<redacted>",
                    ),
                    indent=2,
                )
            )
            return 0

        service, client = _service(
            arguments,
            VERIFY_BATCH if arguments.command == "verify" else UPLOAD_BATCH_LIMIT,
        )
        if arguments.command == "vector-arm":
            print(
                json.dumps(
                    _vector_arm(
                        Retriever(service, alias=arguments.alias, top=arguments.top),
                        arguments.repeat,
                    ),
                    indent=2,
                )
            )
            return 0
        if arguments.command == "retrieve":
            retriever = Retriever(
                service,
                alias=arguments.alias,
                top=arguments.top,
                allowance=SemanticAllowance(
                    store=FileAllowanceStore(Path(arguments.landing) / ALLOWANCE_FILE)
                ),
            )
            result = retriever.search(arguments.query, rerank=not arguments.no_rerank)
            print(
                json.dumps(
                    {
                        "query": result.query,
                        "confidence": result.confidence.value,
                        "reranked": result.reranked,
                        "degraded": result.degraded,
                        "vector_arm": result.vector_arm.value,
                        "floor": result.floor,
                        "constraints": result.constraints.as_dict(),
                        "notes": list(result.notes),
                        "uncitable": result.uncitable,
                        "allowance": retriever.allowance.report().as_dict(),
                        "passages": [
                            {
                                **passage.citation(),
                                "score": passage.score,
                                "reranker_score": passage.reranker_score,
                                "overlap": round(passage.overlap, 3),
                                "caption": passage.caption,
                                "text": passage.text,
                            }
                            for passage in result.passages
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        if arguments.command == "status":
            print(json.dumps(build_module.statistics(service, arguments.alias), indent=2))
            return 0
        if arguments.command == "rollback":
            print(
                f"{arguments.alias} -> {build_module.rollback(service, arguments.alias)}"
            )
            return 0

        deployment = EmbeddingDeployment.from_env()
        embedder = _embedder(client, deployment)
        key = _vectorizer_key(arguments)

        if arguments.command == "build":
            report = build_module.build(
                service,
                _chunks(arguments),
                deployment,
                embedder,
                alias=arguments.alias,
                vectorizer_key=key,
                swap=not arguments.no_swap,
            )
            print(report.render())
            return 0

        chunk_set = _chunks(arguments)
        smaller = corpus.ChunkSet(
            run_id=f"{chunk_set.run_id}",
            rows=chunk_set.rows[:-1],
            origin=f"{chunk_set.origin} (one chunk held back, so the two "
            f"indexes differ in size)",
        )
        swap = verify.check_swap(
            service,
            chunk_set,
            smaller,
            deployment,
            embedder,
            alias=arguments.alias,
            vectorizer_key=key,
        )
        print(swap.render())
        failed = verify.check_failed_build(
            service,
            chunk_set,
            deployment,
            embedder,
            alias=arguments.alias,
            vectorizer_key=key,
        )
        print(failed.render())
        if key is not None:
            print(
                json.dumps(
                    verify.check_vectorization(
                        service, arguments.alias, "what has no dairy in it"
                    ),
                    indent=2,
                )
            )
        return 0 if swap.passed and failed.passed else 2
    except SearchError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
