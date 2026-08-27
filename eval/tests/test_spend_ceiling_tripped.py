"""Issue #85: *"Not reasoned about. Tripped."*

The spend cap is the most thoroughly unit-tested thing in this repository and
until now it had never been **run into**. ``api/tests/test_guard.py`` and
``api/tests/test_ledger.py`` drive :class:`~chip_chat.api.guard.SpendGuard` and
:class:`~chip_chat.api.ledger.BudgetLedger` directly, on a
:class:`~chip_chat.api.testing.FakeClock`, and prove every branch. What they do
not do is what #85 asks for: take the assembled application, send real HTTP
requests at it until the door shuts, and then look at what a visitor and a trace
actually see.

The gap between those two is not pedantry. A cap that is correct in the object
and absent from the route is the failure this repository has already found once
-- ``api/src/chip_chat/api/app.py``'s own docstring says
:class:`~chip_chat.api.guard.SpendGuard` *"was correct, tested and had no caller;
a correct cap with no caller does not stop anybody spending anything."* This
module is the check that it still has one, made through the front door.

**Why a real socket and not a TestClient.** Every request below goes over TCP to
a ``uvicorn`` in a thread. ``TestClient`` calls the ASGI application directly,
which is enough to test routing and is not enough to test a ceiling: it shares
the caller's stack, so a middleware ordering bug, a per-worker ledger, or a
response the ASGI server buffers differently are all invisible to it. #85 says
*"in a real environment"*, and the smallest honest reading of that is a socket.

**Why a scripted model and not a real one.** The criterion is *"zero model
tokens consumed while tripped, confirmed in traces rather than inferred from the
absence of an error."* :class:`~chip_chat.api.testing.RecordingModel` makes the
first half assertable exactly -- ``model.calls`` is a list and the assertion is
that it stopped growing -- and the span recorder makes the second half assertable
exactly, because ``llm.completion`` spans are counted rather than assumed absent.
A real model would make both weaker: the numbers would come back from Azure and
the test would be measuring Azure. It would also cost money on every pull
request, and a test nobody can afford to run is a test that gets a ``skip``.

The one thing this cannot do from here is the public deployment's own ceiling,
and that is deliberate rather than an omission -- tripping it would take the demo
down for everybody. ``docs/red-team.md`` records what was done against the live
URL instead, and what remains unverified there.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import pytest
import uvicorn

from chip_chat.agent.model import ModelReply
from chip_chat.api.app import Service, create_app
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.ledger import BudgetLedger
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import STOP_STATE_MESSAGE
from chip_chat.api.testing import FakeClock
from chip_chat.api.turns import SpendGate
from chip_chat.otel.schema import SpanName
from chip_chat.otel.testing import SpanRecorder, span_recorder

_STARTUP_TIMEOUT_SECONDS: Final = 20.0
"""How long to wait for the server thread to bind. Generous; a CI box is slow."""

_TOKENS_PER_TURN: Final = 1_200
"""What the scripted model charges per turn: 1,000 prompt plus 200 completion.

Matched to :class:`~chip_chat.api.testing.RecordingModel`'s defaults so that the
arithmetic in each test is legible: a ceiling of 6,000 is five turns, and the
sixth is the one that must be refused.
"""


class MeteredModel:
    """A model that always answers, always costs the same, and counts its calls.

    Neither double in the tree fits this module.
    :class:`~chip_chat.api.testing.RecordingModel` is built for guard tests that
    assert it was *never* called and has no ``deployment``, so a turn that
    reaches it raises -- and a ceiling reached by exceptions rather than by spend
    is a measurement of the exception path.
    :class:`~chip_chat.agent.testing.ScriptedModel` answers a fixed number of
    times and then raises, which is the same problem with a longer fuse: this
    module's whole method is *keep talking until the door shuts*, and the number
    of turns that takes is what is being measured rather than something the test
    may pre-declare.

    So: unlimited answers, fixed cost, and a call count. The count is the
    evidence for #85's second criterion -- the assertion is that it stopped
    growing once the ceiling was reached, which is a fact about a list rather
    than an inference from a friendly reply.
    """

    deployment = "gpt-test-mini"

    def __init__(
        self,
        reply: str = "Chips are $2.95.",
        *,
        prompt_tokens: int = 1_000,
        completion_tokens: int = 200,
    ) -> None:
        self._reply = reply
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._lock = threading.Lock()
        self.calls: list[int] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelReply:
        with self._lock:
            self.calls.append(len(messages))
        return ModelReply(
            content=self._reply,
            tool_calls=(),
            finish_reason="stop",
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )


class Deployment:
    """The real application, on a real port, for the life of one test.

    Attributes:
        model: The stand-in the gate hands out. Its ``calls`` list is the
            evidence for *zero tokens while tripped* -- the assertion is that it
            stopped growing, which is a fact about a list rather than an
            inference from a reply.
        kill_switch: The circuit breaker, in the run position.
        ledger: The counter, so a test can drive the day boundary.
    """

    def __init__(
        self,
        limits: SpendLimits,
        *,
        clock: FakeClock | None = None,
    ) -> None:
        self.clock = clock or FakeClock()
        self.model = MeteredModel()
        self.kill_switch = ManualKillSwitch()
        self.ledger = BudgetLedger(limits, clock=self.clock)
        guard = SpendGuard(
            limits, kill_switch=self.kill_switch, clock=self.clock, ledger=self.ledger
        )
        self._app = create_app(Service(SpendGate(guard, lambda: self.model)))
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> "Deployment":
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=0, log_level="error", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            servers = getattr(self._server, "servers", None)
            if self._server.started and servers:
                self.port = servers[0].sockets[0].getsockname()[1]
                return self
            time.sleep(0.02)
        raise RuntimeError("the deployment never bound a port")

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=_STARTUP_TIMEOUT_SECONDS)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def say(
        self, message: str = "how much are chips", *, cookie: str | None = None
    ) -> tuple[Mapping[str, Any], str]:
        """One turn over the wire. Returns the body and the session cookie."""
        request = urllib.request.Request(
            f"{self.base}/api/chat",
            data=json.dumps({"message": message, "confirm_draft_id": None}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if cookie:
            request.add_header("Cookie", f"cc_session={cookie}")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
            handed = response.headers.get("Set-Cookie", "")
        found = re.search(r"cc_session=([^;]+)", handed)
        return body, (found.group(1) if found else (cookie or ""))

    def entry_page(self) -> str:
        """``GET /`` -- what a brand new visitor sees before typing anything."""
        with urllib.request.urlopen(f"{self.base}/", timeout=30) as response:
            return response.read().decode("utf-8")

    def health(self) -> int:
        """``GET /healthz`` -- deliberately outside the cap."""
        try:
            with urllib.request.urlopen(f"{self.base}/healthz", timeout=30) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Every span the deployment produced, in this process."""
    with span_recorder() as recorder:
        yield recorder


def _ceiling(turns: int) -> SpendLimits:
    """Limits whose daily ceiling is exactly ``turns`` turns of the scripted model.

    The reservation is set to one turn's real cost so that the arithmetic is the
    obvious one. In production the reservation is deliberately pessimistic --
    eight thousand tokens claimed up front against a turn that usually costs
    less -- and that is right there and wrong here, because a test whose ceiling
    is reached by the *reservation* rather than by the spend is a test of the
    reservation.
    """
    return SpendLimits(
        daily_token_ceiling=turns * _TOKENS_PER_TURN,
        session_turn_cap=1_000,
        session_token_cap=10_000_000,
        source_requests_per_window=10_000,
        source_window_seconds=60.0,
        turn_token_reservation=_TOKENS_PER_TURN,
    )


def test_the_daily_ceiling_is_reached_by_talking_and_the_door_shuts(
    spans: SpanRecorder,
) -> None:
    """#85's first criterion, and the one the ticket puts in italics.

    Five turns of real HTTP fit under the ceiling. The sixth is refused, and the
    refusal is the friendly copy rather than an error -- which is checked here
    over a socket rather than on the object, because the visitor's experience of
    a spend cap is a sentence in a chat window and nothing else.
    """
    with Deployment(_ceiling(5)) as app:
        replies = [app.say()[0] for _ in range(5)]
        assert not any(reply["stopped"] for reply in replies)
        tripped, _ = app.say()

    assert tripped["stopped"] is True
    assert tripped["reply"] == STOP_STATE_MESSAGE


def test_no_model_call_is_attempted_once_the_ceiling_is_reached(
    spans: SpanRecorder,
) -> None:
    """#85's second criterion, both halves of it.

    *"Zero tokens, confirmed in traces rather than assumed from the absence of an
    error."* Two independent assertions, deliberately, because each covers the
    other's blind spot: the model double's call list would still be empty if the
    app had died, and the span count would still be right if a completion were
    issued outside the tracer. Both have to hold.
    """
    with Deployment(_ceiling(3)) as app:
        for _ in range(3):
            app.say()
        calls_before = len(app.model.calls)
        completions_before = len(spans.llm_spans())

        for _ in range(6):
            refused, _ = app.say()
            assert refused["stopped"] is True

        assert len(app.model.calls) == calls_before
        assert len(spans.llm_spans()) == completions_before


def test_a_refused_turn_still_emits_the_guard_span_that_refused_it(
    spans: SpanRecorder,
) -> None:
    """The other half of *confirmed in traces*: silence is not evidence.

    A tripped turn that emitted nothing at all would satisfy "zero completion
    spans" and would leave nobody able to tell a working cap from an outage. The
    turn is still a turn: ``chat.turn`` opens, ``guard.budget_check`` says why it
    refused, and ``llm.completion`` is the one that must be missing.
    """
    with Deployment(_ceiling(1)) as app:
        app.say()
        spans.clear()
        refused, _ = app.say()
        assert refused["stopped"] is True

    names = spans.names()
    assert SpanName.GUARD_BUDGET_CHECK.value in names
    assert SpanName.CHAT_TURN.value in names
    assert SpanName.LLM_COMPLETION.value not in names


def test_the_stop_state_appears_on_entry_as_well_as_mid_conversation() -> None:
    """#85's third criterion. Two positions, and they are different code.

    Mid-conversation is ``POST /api/chat`` returning ``stopped``; on entry is
    ``GET /`` serving a different page entirely, with no composer on it, so that
    a visitor arriving after the ceiling is reached is not invited to type into
    something that cannot answer.
    """
    with Deployment(_ceiling(1)) as app:
        mid, _ = app.say()
        assert mid["stopped"] is False
        mid_tripped, _ = app.say()
        assert mid_tripped["stopped"] is True

        page = app.entry_page()

    assert STOP_STATE_MESSAGE in page
    assert "<form" not in page.lower(), "a stopped visitor was still invited to type"


def test_the_stop_state_is_never_an_error_status() -> None:
    """It is a designed state. An HTTP error would put it in somebody's alerting."""
    with Deployment(_ceiling(1)) as app:
        app.say()
        request = urllib.request.Request(
            f"{app.base}/api/chat",
            data=json.dumps({"message": "hello", "confirm_draft_id": None}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200


def test_the_health_probe_answers_while_the_door_is_shut() -> None:
    """A probe refused for spending money it never spends takes the app down.

    Container Apps restarts an instance whose liveness probe fails. If the cap
    covered ``/healthz``, reaching the daily ceiling would put the app into a
    restart loop -- and the restart would clear the in-memory ledger, so it would
    serve traffic again, spend to the ceiling again, and fail the probe again.
    """
    with Deployment(_ceiling(1)) as app:
        app.say()
        app.say()
        assert app.health() == 200


def test_one_session_cannot_take_the_day(spans: SpanRecorder) -> None:
    """#85's fourth criterion: the per-session cap, independent of the global one.

    The ceiling here is generous and the session cap is three turns, so what
    stops the fourth turn is the session and not the day. That separation is the
    whole point -- one visitor holding the demo open for an afternoon is the case
    the global ceiling does not cover, because the global ceiling is reached only
    after they have already had it.
    """
    limits = SpendLimits(
        daily_token_ceiling=10_000_000,
        session_turn_cap=3,
        session_token_cap=10_000_000,
        source_requests_per_window=10_000,
        turn_token_reservation=_TOKENS_PER_TURN,
    )
    with Deployment(limits) as app:
        _, cookie = app.say()
        app.say(cookie=cookie)
        app.say(cookie=cookie)
        capped, _ = app.say(cookie=cookie)
        assert capped["stopped"] is True

        # And the cap is the session's, not the app's: a new visitor is served.
        fresh, _ = app.say()
        assert fresh["stopped"] is False


def test_a_naive_loop_from_one_address_is_refused(spans: SpanRecorder) -> None:
    """#85's fifth criterion, run as the loop rather than as a unit test.

    The realistic abuse is not a clever attack, it is somebody's ``while true``.
    Four requests against a window of three, from one address, and the fourth is
    refused before a model is reached -- which is asserted on the model's call
    list rather than on the reply, because a refusal that still called the model
    would read identically to the visitor.
    """
    limits = SpendLimits(
        daily_token_ceiling=10_000_000,
        session_turn_cap=1_000,
        session_token_cap=10_000_000,
        source_requests_per_window=3,
        source_window_seconds=60.0,
        turn_token_reservation=_TOKENS_PER_TURN,
    )
    with Deployment(limits) as app:
        for _ in range(3):
            app.say()
        calls = len(app.model.calls)
        refused, _ = app.say()

    assert refused["stopped"] is True
    assert len(app.model.calls) == calls


def test_the_kill_switch_flips_the_whole_app_without_a_restart() -> None:
    """#85's kill-switch criterion, timed, against a running server.

    The switch is thrown while the process keeps serving, and the very next
    request over the same socket gets the stop state. The elapsed time is
    asserted to be small, because *"reachable without a deploy"* is a claim about
    latency as much as about mechanism: a switch that needed a restart would show
    up here as a connection error rather than as a slow pass, and one that needed
    a new revision would not show up in this process at all.

    Note what this does and does not cover. It proves the in-process switch is
    immediate. The deployed app's switch is
    :class:`~chip_chat.api.killswitch.EnvironmentKillSwitch` behind a
    :class:`~chip_chat.api.killswitch.CachedKillSwitch`, and changing an app
    setting on Container Apps creates a revision -- ``docs/deployment.md`` §3.8
    measured that at about forty seconds. That is the number for the runbook;
    this is the number for the code.
    """
    with Deployment(_ceiling(1_000)) as app:
        running, _ = app.say()
        assert running["stopped"] is False

        started = time.monotonic()
        app.kill_switch.throw()
        stopped, _ = app.say()
        elapsed = time.monotonic() - started

        assert stopped["stopped"] is True
        assert stopped["reply"] == STOP_STATE_MESSAGE
        assert elapsed < 5.0, f"the switch took {elapsed:.2f}s to take effect"

        # And the entry page follows it, so a visitor arriving after the flip is
        # not handed a composer either.
        assert STOP_STATE_MESSAGE in app.entry_page()


def test_the_counter_resets_at_the_configured_day_boundary_over_the_wire() -> None:
    """#85's sixth criterion, including the timezone question.

    The zone is deliberately not UTC. A ledger that computed the day with
    :func:`datetime.date.today` would pass a test written in UTC and reset at the
    wrong hour in production, and the wrong hour is the one the demo is being
    watched in. The clock is stepped past **Los Angeles** midnight while the
    server keeps running, and the visitor who was refused a moment ago is served.

    This is the *real rollover* the ticket asks for in the only form a test can
    have it: the boundary is crossed while the process is up and the ledger is
    the one that was already loaded, rather than by restarting into a new day.
    """
    limits = SpendLimits(
        daily_token_ceiling=2 * _TOKENS_PER_TURN,
        session_turn_cap=1_000,
        session_token_cap=10_000_000,
        source_requests_per_window=10_000,
        turn_token_reservation=_TOKENS_PER_TURN,
        reset_timezone="America/Los_Angeles",
    )
    # August is PDT, UTC-7. 06:00 UTC on the 27th is 23:00 on the 26th in Los
    # Angeles: a moment that is already the 27th in UTC and is not yet the 27th
    # where the cap resets, which is exactly the discrepancy the zone exists for.
    clock = FakeClock(datetime(2026, 8, 27, 6, 0, tzinfo=UTC))
    with Deployment(limits, clock=clock) as app:
        app.say()
        app.say()
        assert app.say()[0]["stopped"] is True

        # Half an hour on: 06:30 UTC, still 23:30 on the 26th in Los Angeles.
        # A ledger that reset on the UTC date would open here. Still shut.
        clock.advance(30 * 60)
        assert app.say()[0]["stopped"] is True

        # 07:30 UTC is 00:30 on the 27th in Los Angeles. Past the boundary the
        # cap is configured with. Open again, and nobody deployed anything.
        clock.advance(60 * 60)
        assert app.say()[0]["stopped"] is False
