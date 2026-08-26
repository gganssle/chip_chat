"""One synthetic session, exercising every node of the span schema.

This is the thing you run to find out whether traces are reaching a backend. It
builds three turns out of nothing -- no model is called, no service is contacted,
every value is invented -- and between them they emit every span name in
:mod:`chip_chat.otel.schema`, nested the way RFC-001 section 09 says they nest.

.. code-block:: bash

    make dev        # brings Phoenix up and runs this once
    make trace      # runs it again

It ships with the package for the same reason :mod:`chip_chat.otel.testing`
does. The schema is consumed outside this repository, and a claim about the shape
of a trace is only worth something if the far end can reproduce it: pointing this
module at a backend and reading the result is how you check that the collector,
the exporter and the schema still agree. It is a fixture of the schema, not
product logic -- the agent, its tools and the turns they really run arrive in
issues #16 and #64, and none of them import this module.

It is also the tool decision D6 gets checked with. Repointing
``OTEL_EXPORTER_OTLP_ENDPOINT`` from Phoenix at Arize AX (issue #78) has to be a
configuration change and nothing else; running this against the new endpoint and
seeing the same tree is what turns that from an intention into an observation.
"""

import sys
import time
import uuid
from collections.abc import Sequence

from chip_chat.otel.attributes import ConfirmationState
from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.schema import OpsAction, ToolName
from chip_chat.otel.spans import (
    Document,
    LlmRecorder,
    Message,
    TokenUsage,
    VisionRecorder,
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    cortex_analyst_query,
    llm_completion,
    matcher_resolve,
    ops_write,
    render_response,
    retriever_search,
    tool_call,
    vision_describe,
)
from chip_chat.otel.tracing import configure_tracing, shutdown_tracing

__all__ = [
    "DEMO_ID",
    "PERSONA_ID",
    "account_turn",
    "emit_demo_session",
    "knowledge_turn",
    "main",
    "new_session_id",
    "vision_order_turn",
]

PERSONA_ID = "persona-loyal-regular"
"""The demo persona these turns pretend to be browsing as."""

DEMO_ID = "demo-0000-0000-0000"
"""Correlation value only. Never read back off a span to authorise anything."""

_MODEL = "gpt-4o"
_PROVIDER = "azure"
_VISION_MODEL = "gpt-4o"

_STEP_PAUSE_SECONDS = 0.04
"""What :func:`main` waits inside each span, so the waterfall is legible."""


def new_session_id() -> str:
    """Return a fresh session id, distinct per run so traces do not merge."""
    return f"smoke-{uuid.uuid4().hex[:12]}"


def _work(seconds: float) -> None:
    """Stand in for the work a real span wraps.

    Spans that open and close in the same microsecond render as a stack of
    zero-width bars, which is a waterfall you cannot read. A few milliseconds
    per span buys a picture worth looking at and costs nothing that matters.
    """
    if seconds:
        time.sleep(seconds)


_MENU_DOCUMENTS: Sequence[Document] = (
    Document(
        id="menu-barbacoa-0",
        content="Barbacoa: beef seasoned with chipotle adobo, cumin, cloves and bay.",
        score=0.83,
        metadata={
            "source_url": "https://www.chipotle.com/",
            "harvested_at": "2026-08-25",
        },
    ),
    Document(
        id="menu-salsa-3",
        content="Red chili salsa is the hottest of the four salsas.",
        score=0.61,
        metadata={
            "source_url": "https://www.chipotle.com/",
            "harvested_at": "2026-08-25",
        },
    ),
)


_PHOTO_REF = "uploads/2026-08-25/smoke-meal.jpg"
"""The photograph the vision turn describes.

A ``container/name`` key, the form :class:`~chip_chat.vision.store.BlobRef`
renders and the only form that crosses a tool boundary."""

_NO_TOKENS = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)


def _spend(
    recorder: LlmRecorder | VisionRecorder,
    running: TokenUsage,
    *,
    prompt: int,
    completion: int,
) -> TokenUsage:
    """Record one model call's tokens, and add them to the turn's running total.

    Both in one call so the two cannot drift. A fixture whose ``chat.turn``
    rollup disagreed with the ``llm.token_count.*`` beneath it would be teaching
    the wrong thing to every reader of the demo trace -- and would fail
    :meth:`~chip_chat.otel.testing.SpanRecorder.assert_token_counts_sum`, which
    is the check this schema asks its consumers to run.

    Args:
        recorder: The model call's recorder.
        running: The turn's total so far.
        prompt: Prompt tokens for this call.
        completion: Completion tokens for this call.

    Returns:
        The new running total.
    """
    recorder.record_usage(prompt_tokens=prompt, completion_tokens=completion)
    return running + TokenUsage(prompt_tokens=prompt, completion_tokens=completion)


def knowledge_turn(session_id: str, *, index: int = 0, pause: float = 0.0) -> None:
    """Emit a menu-knowledge turn: both guards, a retrieval, two model steps.

    Args:
        session_id: The session these turns share.
        index: Turn index within the session.
        pause: Seconds to spend inside each span; see :func:`_work`.
    """
    question = "Is the barbacoa spicy?"
    answer = (
        "Barbacoa is seasoned with chipotle and cumin -- warm rather than hot. "
        "The hottest thing on the menu is the red chili salsa."
    )
    with chat_turn(
        session_id=session_id,
        turn_index=index,
        message=question,
        persona_id=PERSONA_ID,
        demo_id=DEMO_ID,
    ) as turn:
        spent = _NO_TOKENS
        with budget_check() as budget:
            budget.record_budget(scope="session", tokens_used=812, tokens_limit=40_000)
            budget.allow()
            _work(pause)

        with content_safety() as safety:
            safety.allow()
            _work(pause)

        with agent_step(index=0) as step:
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_input_messages([Message(role="user", content=question)])
                spent = _spend(llm, spent, prompt=812, completion=64)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(
                ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": question}
            ) as tool:
                with retriever_search(query=question, index="menu-current") as search:
                    search.record_documents(_MENU_DOCUMENTS)
                    _work(pause)
                tool.record_result([document.id for document in _MENU_DOCUMENTS])
            step.record_output("searched the menu index")

        with agent_step(index=1):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_output_messages([Message(role="assistant", content=answer)])
                spent = _spend(llm, spent, prompt=1_240, completion=48)
                llm.record_finish_reason("stop")
                _work(pause)

        with render_response() as render:
            render.record_output(answer)
            _work(pause)
        turn.record_token_rollup(spent)
        turn.record_output(answer)


def account_turn(session_id: str, *, index: int = 1, pause: float = 0.0) -> None:
    """Emit an account turn: Cortex Analyst, plus a gold-mart read with no child.

    Args:
        session_id: The session these turns share.
        index: Turn index within the session.
        pause: Seconds to spend inside each span; see :func:`_work`.
    """
    question = "How many points do I have, and what's my usual?"
    answer = (
        "You have 1,340 points. Your usual is a chicken burrito bowl with white "
        "rice, black beans, mild salsa and guac."
    )
    with chat_turn(
        session_id=session_id,
        turn_index=index,
        message=question,
        persona_id=PERSONA_ID,
        demo_id=DEMO_ID,
    ) as turn:
        spent = _NO_TOKENS
        with budget_check() as budget:
            budget.record_budget(scope="session", tokens_used=2_164, tokens_limit=40_000)
            budget.allow()
            _work(pause)

        with agent_step(index=0):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_input_messages([Message(role="user", content=question)])
                spent = _spend(llm, spent, prompt=904, completion=72)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(
                ToolName.ASK_ACCOUNT_QUESTION, arguments={"question": "points balance"}
            ) as tool:
                with cortex_analyst_query(question="points balance") as analyst:
                    analyst.record_query(
                        sql="SELECT points_balance FROM gold.account_summary",
                        row_count=1,
                    )
                    _work(pause)
                tool.record_result({"points_balance": 1_340})

            # A gold-mart read has no child span: the tool span is the whole of
            # it. Worth seeing in the demo, so nobody concludes that every tool
            # is supposed to nest something.
            with tool_call(ToolName.GET_USUAL_ORDER) as tool:
                tool.record_result({"sku": "BOWL-CHK-001", "derived_at": "2026-08-25"})
                _work(pause)

        with agent_step(index=1):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_output_messages([Message(role="assistant", content=answer)])
                spent = _spend(llm, spent, prompt=1_388, completion=56)
                llm.record_finish_reason("stop")
                _work(pause)

        with render_response() as render:
            render.record_output(answer)
            _work(pause)
        turn.record_token_rollup(spent)
        turn.record_output(answer)


def vision_order_turn(session_id: str, *, index: int = 2, pause: float = 0.0) -> None:
    """Emit a photo-to-order turn: vision, the matcher, and a confirmed write.

    Args:
        session_id: The session these turns share.
        index: Turn index within the session.
        pause: Seconds to spend inside each span; see :func:`_work`.
    """
    question = "Make me what's in this photo."
    answer = "Ordered -- a chicken burrito bowl with guac. Confirmation is CC-4471."
    description = {
        "meals_visible": 1,
        "slots": {
            "format": "bowl",
            "protein": "chicken",
            "rice": "white",
            "beans": "black",
        },
        "notes": "display only",
    }
    with chat_turn(
        session_id=session_id,
        turn_index=index,
        message=question,
        persona_id=PERSONA_ID,
        demo_id=DEMO_ID,
    ) as turn:
        spent = _NO_TOKENS
        with budget_check() as budget:
            budget.record_budget(scope="session", tokens_used=3_616, tokens_limit=40_000)
            budget.allow()
            _work(pause)

        # Image moderation runs before inference, which is why the guard says
        # what it screened.
        with content_safety(subject="image") as safety:
            safety.allow()
            _work(pause)

        with agent_step(index=0):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                spent = _spend(llm, spent, prompt=1_020, completion=40)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(
                ToolName.MATCH_MEAL_FROM_PHOTO,
                arguments={"blob_ref": _PHOTO_REF},
            ) as tool:
                lane = _NO_TOKENS
                with vision_describe(image_ref=_PHOTO_REF, model=_VISION_MODEL) as vision:
                    # The image as OpenInference reads a multimodal input, so
                    # the demo trace shows the photograph the description came
                    # from rather than an opaque key.
                    vision.record_image(_PHOTO_REF, prompt="Describe this meal.")
                    lane = _spend(vision, lane, prompt=274, completion=91)
                    vision.record_description(description)
                    _work(pause)
                with matcher_resolve() as matcher:
                    matcher.record_slots(
                        {
                            "beans": ("black", 0.91),
                            "format": ("bowl", 0.98),
                            "protein": ("chicken", 0.94),
                            "rice": ("white", 0.88),
                        }
                    )
                    matcher.record_resolved_skus(["BOWL-CHK-001", "SIDE-GUAC-001"])
                    _work(pause)
                tool.record_result({"skus": ["BOWL-CHK-001", "SIDE-GUAC-001"]})
                # What the lane cost, on the span that contains it. Under our
                # own key, so summing the OpenInference ones stays exact.
                tool.record_token_rollup(lane)
                spent = spent + lane

        with agent_step(index=1):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                spent = _spend(llm, spent, prompt=1_402, completion=88)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(ToolName.PROPOSE_ORDER) as tool:
                tool.record_result({"draft_id": "draft-7c21"})
                _work(pause)

        with agent_step(index=2):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                spent = _spend(llm, spent, prompt=1_540, completion=32)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(
                ToolName.PLACE_ORDER, arguments={"draft_id": "draft-7c21"}
            ) as tool:
                with ops_write(OpsAction.PLACE_ORDER, reference_id="draft-7c21") as ops:
                    # The ops API is what enforces confirmation, not the prompt.
                    # `confirmed` here is the ops API's answer, recorded.
                    ops.record_confirmation(ConfirmationState.CONFIRMED)
                    ops.record_receipt({"order_id": "CC-4471"})
                    _work(pause)
                tool.record_result({"order_id": "CC-4471"})

        with render_response() as render:
            render.record_output(answer)
            _work(pause)
        turn.record_token_rollup(spent)
        turn.record_output(answer)


def emit_demo_session(session_id: str, *, pause: float = 0.0) -> str:
    """Emit the whole demo session -- three turns, every span name in the schema.

    Args:
        session_id: The session id all three turns carry.
        pause: Seconds to spend inside each span; see :func:`_work`.

    Returns:
        ``session_id``, so a caller can print what to search Phoenix for.
    """
    knowledge_turn(session_id, index=0, pause=pause)
    account_turn(session_id, index=1, pause=pause)
    vision_order_turn(session_id, index=2, pause=pause)
    return session_id


def main() -> int:
    """Send one demo session wherever the environment says spans go.

    Reads the same environment variables every component reads, so there is
    nothing here that knows which product is listening.

    Returns:
        A process exit code. Non-zero when no exporter is configured, because a
        smoke test that quietly exported nowhere is worse than a failure.
    """
    config = TelemetryConfig.from_env("otel")
    if not config.exports_anywhere:
        print(
            "No exporter is configured, so these spans would go nowhere.\n"
            "Set OTEL_EXPORTER_OTLP_ENDPOINT (the local stack answers on\n"
            "http://localhost:6006), or set CHIP_CHAT_OTEL_CONSOLE=1 to print\n"
            "the spans instead. `make dev` does the first of those for you.",
            file=sys.stderr,
        )
        return 1

    configure_tracing(config)
    try:
        session_id = emit_demo_session(new_session_id(), pause=_STEP_PAUSE_SECONDS)
    finally:
        # Flushes the batch processor. Without it a short-lived process exits
        # before the exporter has sent anything, which looks exactly like a
        # backend that is not listening.
        shutdown_tracing()

    destination = config.otlp_endpoint or "the configured exporters"
    print(f"Sent 3 turns as session {session_id} to {destination}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
