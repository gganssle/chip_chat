"""What the browser is handed when an answer has a source, per decision D9.

The agent's half of this is ``agent/tests/test_citation_path.py``: a turn parses
the model's declared field and resolves the ids against what was actually
retrieved. This file is the other end of the same wire -- whether the route
carries the result, in both shapes it answers in, and whether the JSON the model
wrote is still anywhere a visitor can read it.

Bead ``chip-2ky`` was not a bug in any one of these layers. Every layer was
correct; the route did not carry the field and the widget did not draw it, so
the model's own packaging arrived as prose. That is why the assertions here are
about the *response body* rather than about a function: the body is what the
deployment showed people.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chip_chat.agent.model import ModelReply
from chip_chat.agent.testing import ScriptedModel
from chip_chat.api.app import Service, create_app
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.testing import FakeClock
from chip_chat.api.turns import SpendGate

ENVELOPE_LINE = '{"claim_class":"food","citations":["menu-barbacoa-1"]}'
PROSE = "Moderately. It's braised with chipotle chiles and cumin."


@pytest.fixture
def limits() -> SpendLimits:
    return SpendLimits(
        daily_token_ceiling=100_000,
        session_turn_cap=20,
        session_token_cap=60_000,
        source_requests_per_window=50,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def model() -> ScriptedModel:
    """A model that answers the way the deployed one did: prose, then the field.

    No knowledge lane is wired on this service, so nothing can resolve the id it
    names -- which is the point. The visitor must not read the JSON *whether or
    not* the citation survives, and separating the two failures is what makes
    this test about the route rather than about retrieval.
    """
    return ScriptedModel(
        *[
            ModelReply(
                content=f"{PROSE}\n{ENVELOPE_LINE}",
                finish_reason="stop",
                prompt_tokens=1_100,
                completion_tokens=60,
            )
        ]
        * 8
    )


@pytest.fixture
def client(limits: SpendLimits, model: ScriptedModel) -> Iterator[TestClient]:
    service = Service(
        SpendGate(
            SpendGuard(limits, kill_switch=ManualKillSwitch(), clock=FakeClock()),
            lambda: model,
        )
    )
    with TestClient(create_app(service)) as running:
        yield running


def _frames(client: TestClient, message: str) -> list[dict[str, Any]]:
    """Run one turn in the streamed shape and return its frames."""
    response = client.post(
        "/api/chat",
        json={"message": message},
        headers={"Accept": "application/x-ndjson"},
    )
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_the_object_shape_carries_the_prose_without_its_packaging(
    client: TestClient,
) -> None:
    """The bug, at the surface a ``curl`` and an eval harness read."""
    body = client.post("/api/chat", json={"message": "is the barbacoa spicy"}).json()

    assert body["reply"] == PROSE
    assert "claim_class" not in body["reply"]
    assert "citations" not in body["reply"]


def test_the_object_shape_carries_the_envelope_as_fields(client: TestClient) -> None:
    """Two fields, in the shape D9 specifies, on every reply.

    Empty here because no knowledge lane is wired to resolve the id against --
    an unresolvable id is dropped rather than believed, which is the mechanism
    and not a gap in the test.
    """
    body = client.post("/api/chat", json={"message": "is the barbacoa spicy"}).json()

    assert body["claim_class"] == "food"
    assert body["citations"] == []


def test_the_streamed_shape_sends_the_sources_as_their_own_frame(
    client: TestClient,
) -> None:
    """The widget asks for frames, so the frames have to carry it too.

    Order matters and is asserted: the source line belongs under the answer it
    supports and above any confirmation card, which is the order they are read
    in.
    """
    frames = _frames(client, "is the barbacoa spicy")
    kinds = [frame["type"] for frame in frames]

    assert "sources" in kinds
    assert kinds.index("sources") > kinds.index("text")
    assert kinds.index("sources") < kinds.index("end")

    sources = frames[kinds.index("sources")]
    assert sources["claim_class"] == "food"
    assert sources["citations"] == []


def test_no_frame_of_the_stream_carries_the_models_own_json(
    client: TestClient,
) -> None:
    """The assertion a screenshot of the bug would have failed.

    Reassembled from the text frames rather than read off one, because a chunk
    boundary is free to land in the middle of the line that must not be there.
    """
    frames = _frames(client, "is the barbacoa spicy")
    streamed = "".join(frame["text"] for frame in frames if frame["type"] == "text")

    assert streamed == PROSE
    assert "claim_class" not in streamed


def test_the_two_shapes_agree_about_the_same_turn(client: TestClient) -> None:
    """One definition, two renderings. They drift the moment nobody checks."""
    body = client.post("/api/chat", json={"message": "is the barbacoa spicy"}).json()
    frames = _frames(client, "is the barbacoa spicy")
    sources = next(frame for frame in frames if frame["type"] == "sources")

    assert sources["citations"] == body["citations"]
    assert sources["claim_class"] == body["claim_class"]


def test_a_stopped_turn_claims_nothing(client: TestClient, limits: SpendLimits) -> None:
    """The stop state is the app speaking, and the app cites nothing.

    ``claim_class`` defaulting to ``none`` here is not a formality: a stop
    message carrying ``food`` would be counted by PRD K2's uncited-claim rule as
    a food claim with no source, which is a metric measuring the guardrail
    rather than the assistant.
    """
    for _ in range(limits.session_turn_cap):
        client.post("/api/chat", json={"message": "hello"})

    body = client.post("/api/chat", json={"message": "hello"}).json()

    assert body["stopped"] is True
    assert body["claim_class"] == "none"
    assert body["citations"] == []
