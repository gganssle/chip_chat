"""One turn, two processes, one trace.

:mod:`chip_chat.otel.smoke` emits the whole span tree from a single process,
which is the right shape for checking that a backend renders every node. It is
the wrong shape for checking the thing decision D8 actually made risky: the tree
of RFC-001 section 09 is emitted either side of a process boundary, under two
different ``service.name`` values, and nothing joins the halves unless
:mod:`chip_chat.otel.propagation` is wired in.

So this module emits one turn the way the deployed system will:

.. code-block:: text

    chip-chat-api      chat.turn
    chip-chat-api      |- guard.budget_check
    chip-chat-api      |- guard.content_safety
                       |     ... traceparent + baggage cross the wire ...
    chip-chat-agent    |- agent.step
    chip-chat-agent    |  |- llm.completion
    chip-chat-agent    |  `- tool.search_menu_knowledge
    chip-chat-agent    |     `- retriever.search
    chip-chat-agent    |- agent.step
    chip-chat-agent    |  `- llm.completion
    chip-chat-api      `- render.response

.. code-block:: bash

    make trace-boundary

**What to look for in the backend.** One trace, not two. Twelve spans under one
root, with the service name changing in the middle and changing back. If you see
two traces -- a ``chat.turn`` with three children and an orphan ``agent.step`` --
the propagation is not wired in, and every Phase 9 trajectory evaluation would
have been scoring nothing.

Two processes are simulated with two ``TracerProvider``s rather than two
interpreters, because what has to be proved is that the *carrier* carries the
turn: the agent half is opened from the extracted context and from nothing else,
and :func:`chip_chat.otel.propagation.continue_turn` starts from an empty
:class:`~opentelemetry.context.Context` precisely so that an in-process shortcut
cannot make this look like it works when it does not.

No model is called and no service is contacted; every value is invented, as in
:mod:`chip_chat.otel.smoke`.
"""

import argparse
import dataclasses
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from opentelemetry.sdk.trace import TracerProvider

from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.propagation import (
    carrier_to_environment,
    continue_turn,
    turn_context_headers,
)
from chip_chat.otel.schema import ToolName
from chip_chat.otel.service import (
    AGENT_COMPONENT,
    APP_COMPONENT,
    agent_service_name,
    turn_service_names,
)
from chip_chat.otel.smoke import DEMO_ID, PERSONA_ID, new_session_id
from chip_chat.otel.spans import (
    Document,
    Message,
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    llm_completion,
    render_response,
    retriever_search,
    tool_call,
)
from chip_chat.otel.tracing import build_tracer_provider, use_tracer_provider

__all__ = [
    "agent_configuration",
    "agent_half",
    "app_configuration",
    "boundary_turn",
    "main",
]

_MODEL = "gpt-4o"
_PROVIDER = "azure"

_STEP_PAUSE_SECONDS = 0.04
"""What :func:`main` waits inside each span, so the waterfall is legible."""

_QUESTION = "Is the barbacoa spicy?"
_ANSWER = (
    "Barbacoa is seasoned with chipotle and cumin -- warm rather than hot. "
    "The hottest thing on the menu is the red chili salsa."
)

_DOCUMENTS: Sequence[Document] = (
    Document(
        id="menu-barbacoa-0",
        content="Barbacoa: beef seasoned with chipotle adobo, cumin, cloves and bay.",
        score=0.83,
        metadata={
            "source_url": "https://www.chipotle.com/",
            "harvested_at": "2026-08-25",
        },
    ),
)


def app_configuration(env: Mapping[str, str] | None = None) -> TelemetryConfig:
    """The telemetry configuration the FastAPI tier runs under."""
    return TelemetryConfig.from_env(APP_COMPONENT, env)


def agent_configuration(env: Mapping[str, str] | None = None) -> TelemetryConfig:
    """The telemetry configuration the agent container runs under.

    ``service.name`` is pinned to :func:`~chip_chat.otel.service.agent_service_name`
    rather than derived from the component, because in the deployed system it is
    not ours to derive: Foundry forces it to the agent resource's name and
    ignores ``OTEL_SERVICE_NAME``. Overriding it here is this module standing in
    for Foundry, so that the demo shows the two names a real turn carries instead
    of two names of our own choosing.
    """
    base = TelemetryConfig.from_env(AGENT_COMPONENT, env)
    return dataclasses.replace(
        base, extra_resource_attributes={"service.name": agent_service_name(env)}
    )


def _work(seconds: float) -> None:
    if seconds:
        time.sleep(seconds)


def boundary_turn(
    session_id: str,
    *,
    app_provider: TracerProvider,
    agent_provider: TracerProvider,
    index: int = 0,
    pause: float = 0.0,
    call_agent: Callable[[Mapping[str, str]], None] | None = None,
) -> Mapping[str, str]:
    """Emit one turn across the app-to-agent boundary.

    Args:
        session_id: The session this turn belongs to.
        app_provider: Provider standing in for the FastAPI tier's process.
        agent_provider: Provider standing in for the agent container's process.
        index: Turn index within the session.
        pause: Seconds to spend inside each span, so the waterfall is legible.
        call_agent: What to do with the carrier instead of emitting the agent
            half here. This is the seam where a *real* second process goes --
            :option:`--agent-command` runs the agent container through it -- and
            passing something that never returns to this interpreter is the
            point, not a limitation.

    Returns:
        The headers that crossed the boundary. Returned rather than discarded
        because "what was actually on the wire" is the first question asked when
        a trace has split, and printing it costs nothing.
    """
    with (
        use_tracer_provider(app_provider),
        chat_turn(
            session_id=session_id,
            turn_index=index,
            message=_QUESTION,
            persona_id=PERSONA_ID,
            demo_id=DEMO_ID,
        ) as turn,
    ):
        with budget_check() as budget:
            budget.record_budget(scope="session", tokens_used=812, tokens_limit=40_000)
            budget.allow()
            _work(pause)

        with content_safety() as safety:
            safety.allow()
            _work(pause)

        # The app is about to call the agent. Everything the agent needs to
        # stay inside this turn is in these headers and nowhere else.
        headers = turn_context_headers()

        if call_agent is not None:
            call_agent(headers)
        else:
            with use_tracer_provider(agent_provider):
                agent_half(headers, pause=pause)

        with render_response() as render:
            render.record_output(_ANSWER)
            _work(pause)
        turn.record_output(_ANSWER)

    return headers


def agent_half(headers: Mapping[str, str], *, pause: float = 0.0) -> None:
    """Everything the agent container emits, opened from the carrier alone.

    Public because the agent image runs exactly this, from
    ``python -m chip_chat.agent.container agent-half``. That is what turns the
    in-process demo below into the two-container proof issue #103 asks for: the
    spans come from the same code either way, and only the process changes.

    Args:
        headers: The carrier the app sent. Nothing else links these spans to the
            turn -- there is no ambient context to fall back on in a container.
        pause: Seconds to spend inside each span, so the waterfall is legible.
    """
    with continue_turn(headers):
        with agent_step(index=0) as step:
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_input_messages([Message(role="user", content=_QUESTION)])
                llm.record_usage(prompt_tokens=812, completion_tokens=64)
                llm.record_finish_reason("tool_calls")
                _work(pause)

            with tool_call(
                ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": _QUESTION}
            ) as tool:
                with retriever_search(query=_QUESTION, index="menu-current") as search:
                    search.record_documents(_DOCUMENTS)
                    _work(pause)
                tool.record_result([document.id for document in _DOCUMENTS])
            step.record_output("searched the menu index")

        with agent_step(index=1):
            with llm_completion(model=_MODEL, provider=_PROVIDER) as llm:
                llm.record_output_messages([Message(role="assistant", content=_ANSWER)])
                llm.record_usage(prompt_tokens=1_240, completion_tokens=48)
                llm.record_finish_reason("stop")
                _work(pause)


def _subprocess_agent(command: str) -> Callable[[Mapping[str, str]], None]:
    """Run ``command`` as the agent's process, with the carrier in its environment.

    A shell command rather than an argument vector because what goes here is a
    ``docker run`` line with its own quoting, and asking the caller to pre-split
    it would buy nothing. The command is the operator's own; there is no untrusted
    input on this path.
    """

    def call(headers: Mapping[str, str]) -> None:
        environment = {**os.environ, **carrier_to_environment(headers)}
        print(f"  agent      {command}")
        completed = subprocess.run(command, shell=True, env=environment, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"the agent command exited {completed.returncode}, so the turn's "
                "middle is missing and the trace in the backend is incomplete."
            )

    return call


def main(argv: Sequence[str] | None = None) -> int:
    """Send one boundary-crossing turn wherever the environment says spans go.

    Returns:
        A process exit code. Non-zero when no exporter is configured, because a
        smoke test that quietly exported nowhere is worse than a failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.otel.boundary",
        description="Emit one turn across the app-to-agent boundary.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=_STEP_PAUSE_SECONDS,
        help="seconds to spend inside each span, so the waterfall is legible",
    )
    parser.add_argument(
        "--agent-command",
        default=os.environ.get("CHIP_CHAT_AGENT_COMMAND", ""),
        help=(
            "shell command to run as the agent's process, with the carrier in "
            "TRACEPARENT/TRACESTATE/BAGGAGE. Point it at the agent image and the "
            "boundary is a real one. Defaults to $CHIP_CHAT_AGENT_COMMAND, and to "
            "emitting the agent half in this process when neither is set."
        ),
    )
    options = parser.parse_args(argv)

    app_config = app_configuration()
    if not app_config.exports_anywhere:
        print(
            "No exporter is configured, so these spans would go nowhere.\n"
            "Set OTEL_EXPORTER_OTLP_ENDPOINT (the local stack answers on\n"
            "http://localhost:6006), or set CHIP_CHAT_OTEL_CONSOLE=1 to print\n"
            "the spans instead. `make trace-boundary` does the first of those.",
            file=sys.stderr,
        )
        return 1

    app_provider = build_tracer_provider(app_config)
    agent_provider = build_tracer_provider(agent_configuration())
    session_id = new_session_id()
    call_agent = (
        _subprocess_agent(options.agent_command) if options.agent_command else None
    )
    try:
        headers = boundary_turn(
            session_id,
            app_provider=app_provider,
            agent_provider=agent_provider,
            pause=options.pause,
            call_agent=call_agent,
        )
    finally:
        # Both, and in a finally. A short-lived process that exits before the
        # batch processors flush looks exactly like a backend that is not
        # listening -- and here it would look like half a backend.
        app_provider.shutdown()
        agent_provider.shutdown()

    app_service, agent_service = turn_service_names()
    destination = app_config.otlp_endpoint or "the configured exporters"
    print(f"Sent 1 turn as session {session_id} to {destination}.")
    print(f"  services   {app_service}, {agent_service}")
    print(f"  on the wire {dict(headers)}")
    print(
        "\nExpect ONE trace. A chat.turn with three children and an orphaned "
        "agent.step\nis the split trace, and it means the headers above did not "
        "reach the agent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
