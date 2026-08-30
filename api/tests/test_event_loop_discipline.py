"""The chat and health routes must do no blocking work on the event loop.

This file exists because the property it holds is invisible in review and
expensive in production. The deployment runs **one** uvicorn worker on purpose
(``api/README.md``, "What is not here yet"): every counter and every draft is
process-local, so a second worker would be a second ledger and the daily
spend ceiling would quietly mean twice what it says. One worker means one event
loop, and one blocking call in an ``async def`` handler stops the process
answering anything at all -- including ``/healthz``, which is the route
Container Apps uses to decide whether the container is alive.

That is not hypothetical here. ``docs/deployment.md`` §3.11 records the Phase 8
incident in which ``POST /api/chat`` was an ``async def`` calling blocking work
inline, ``/healthz`` went unanswered, and the platform opened a restart loop
against an application that was merely busy. The fix at the time was
``run_in_threadpool`` and the probe timings now in ``infra/terraform/compute.tf``.
The fix was correct and it was applied by hand, so it protected the calls that
existed that day and nothing about the call added next -- which is exactly what
happened: ``visitors.admit`` went on being called inline on three routes
(``chip-sv6``), where on an expired ``DEFAULT_ROSTER_TTL_SECONDS`` it is a
Snowflake read taken inside a ``threading.Lock``.

So the rule is asserted rather than remembered. These tests read the source of
the handlers and require that the calls known to block are reached through
``run_in_threadpool``. A source-level assertion is a blunt instrument and it is
the right one here: the alternative -- driving a real turn and watching whether a
concurrent request is served -- needs a live event loop, real latency and a
tolerance, which makes it a flaky test of a property that is actually static.

**What this does not check.** It does not know whether a *newly* introduced call
blocks; it holds the calls we have already established block. A future blocking
dependency reached through a new name will pass this file and should be added
to :data:`BLOCKING_CALLS` when it lands.
"""

import ast
import inspect
from pathlib import Path

import pytest

from chip_chat.api import app as app_module

SOURCE = Path(inspect.getsourcefile(app_module) or "").read_text()
"""The module source, read once. Parsed rather than grepped so that a call in a
comment or a docstring cannot satisfy or break an assertion."""

BLOCKING_CALLS: tuple[str, ...] = (
    "resolved.visitors.admit",
    "probe",
)
"""Calls that reach a network or a lock and must never run on the event loop.

``admit`` can fall through to ``StaticRoster.fixtures()``, which on TTL expiry is
a Snowflake read inside a lock, bounded by the driver's 15s login and 30s network
timeouts. ``probe`` is one read per wired lane. Neither is slow enough on its own
to reach the forty-five seconds of unanswered probes a restart needs, which is
why this was a quiet defect rather than an outage -- but each is seconds in which
``/healthz`` cannot answer, on the one worker that answers it.
"""

ASYNC_HANDLERS: tuple[str, ...] = ("chat", "entry_gate", "lane_health", "switch")
"""The ``async def`` route handlers this file governs, by function name.

These are *function* names and not route paths, and the two do not match: the
handler for ``POST /api/entry`` is ``entry_gate``. A name that is merely wrong
would make its test skip rather than fail, so :func:`test_every_governed_handler_exists`
holds the list against the module.
"""


def _named(node: ast.AST) -> str:
    """Return a dotted name for a call target, or ``""`` for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _async_functions() -> dict[str, ast.AsyncFunctionDef]:
    """Return every ``async def`` in the module, by name."""
    return {
        node.name: node
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _bare_blocking_calls(node: ast.AST) -> list[str]:
    """Return the blocking calls made in ``node`` outside a threadpool hop.

    A call is exempt when it appears as an argument to ``run_in_threadpool`` --
    either directly, or wrapped in ``partial`` because it takes keyword
    arguments. That is the whole shape being enforced, so it is the whole
    exemption.
    """
    handed_off: set[int] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        if _named(inner.func) != "run_in_threadpool":
            continue
        for argument in ast.walk(inner):
            handed_off.add(id(argument))

    found: list[str] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call) or id(inner) in handed_off:
            continue
        name = _named(inner.func)
        if name in BLOCKING_CALLS:
            found.append(name)
    return found


@pytest.mark.parametrize("handler", ASYNC_HANDLERS)
def test_an_async_handler_makes_no_blocking_call_on_the_loop(handler: str) -> None:
    """Every known-blocking call in an async handler goes through the threadpool.

    The failure this prevents is not a wrong answer; it is ``/healthz`` timing
    out while the process is healthy, and the platform restarting a container
    that was merely doing its job. See ``docs/deployment.md`` §3.11.
    """
    offenders = _bare_blocking_calls(_async_functions()[handler])
    assert offenders == [], (
        f"{handler}() calls {', '.join(sorted(set(offenders)))} directly on the "
        "event loop. One uvicorn worker means one loop, so this blocks /healthz "
        "for as long as the call takes. Wrap it in run_in_threadpool (use "
        "functools.partial for keyword arguments)."
    )


def test_every_governed_handler_exists() -> None:
    """A handler named wrongly here would hold nothing and say nothing.

    This is the defence a source-level test needs most. ``entry_gate`` was
    written as ``entry`` first, and the parametrised test above skipped rather
    than failed -- so ``POST /api/entry`` went ungoverned while the file
    reported six passes. A skip is not a pass and the list must be checked
    against the module rather than trusted.
    """
    missing = sorted(set(ASYNC_HANDLERS) - set(_async_functions()))
    assert missing == [], (
        f"ASYNC_HANDLERS names {missing}, which app.py does not define. These "
        "are function names, not route paths -- POST /api/entry is entry_gate."
    )


def test_the_blocking_call_list_still_names_real_functions() -> None:
    """The list above is only useful while its names still exist.

    A rename that emptied :data:`BLOCKING_CALLS` would make every assertion in
    this file pass by describing nothing, which is the failure mode a source
    level test has to be defended against explicitly.
    """
    for dotted in BLOCKING_CALLS:
        # The dotted name, not `name(`: once a call is handed to the threadpool
        # it is passed as a reference and the parenthesis goes away, which is
        # the successful state rather than the missing one.
        leaf = dotted.rsplit(".", maxsplit=1)[-1]
        assert dotted in SOURCE or f"{leaf}(" in SOURCE, (
            f"{dotted} is no longer named in app.py. Either it moved and this "
            "list needs the new name, or it is gone and the entry should be."
        )


def test_the_chat_route_still_runs_its_turn_off_the_loop() -> None:
    """The §3.11 fix itself, held so it cannot be undone by a refactor.

    ``chat`` returns a :class:`StreamingResponse` over a *synchronous* generator,
    which Starlette iterates via ``iterate_in_threadpool``; the turn itself runs
    on the dedicated worker in :func:`_held_open`. If either ever became an
    ``async def`` generator awaited inline, the whole turn would be back on the
    loop and this file's other assertions would still pass.
    """
    functions = _async_functions()
    assert "chat" in functions, "no async chat handler; this file needs updating"
    assert "ThreadPoolExecutor" in SOURCE, (
        "_held_open no longer submits the turn to a worker thread; the turn is "
        "back on the event loop that serves /healthz."
    )
