"""The public app tier, end to end: entry, switching, editing, photos, framing.

``test_app.py`` holds the request path to the spend cap and
``test_identity_binding.py`` holds it to the absence RFC-001 §05 is built on.
This module holds it to the four Phase 8 issues that a stranger with the link
actually experiences -- #66's assignment, #67's opening message, #68's card and
#69's switch -- plus the two of #70's criteria that are properties of the
server rather than of the page.

Every test here is one acceptance criterion, and each names it.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chip_chat.agent.testing import ScriptedModel, answer, calls_tool
from chip_chat.api.app import (
    SESSION_COOKIE,
    Service,
    build_visitors,
    create_app,
)
from chip_chat.api.guard import SpendGuard
from chip_chat.api.limits import SpendLimits
from chip_chat.api.turns import SpendGate
from chip_chat.api.uploads import PhotoRegistry
from chip_chat.api.visitors import (
    SHIPPED_ROSTER_PATH,
    PersonaFixture,
    StaticRoster,
    VisitorDesk,
    shipped_roster,
)
from chip_chat.otel import ToolName

ARCHETYPES = ("regular", "lapsed", "explorer")
"""The three PRD §02 personas #69's last criterion asks be visibly different."""


def fixture(demo_id: str, persona_id: str) -> PersonaFixture:
    return PersonaFixture(
        demo_id=demo_id,
        persona_id=persona_id,
        label=persona_id.title(),
        home_store=679,
        home_store_name="Ballard",
        points_balance=1_250,
        usual_item_id="CMG-1",
        order_count=64,
        narrative=f"a {persona_id} at Ballard, 1,250 points on the card.",
    )


@pytest.fixture
def limits() -> SpendLimits:
    return SpendLimits(
        daily_token_ceiling=200_000,
        session_turn_cap=40,
        session_token_cap=100_000,
        source_requests_per_window=200,
        source_window_seconds=60.0,
    )


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel(*[answer("Sure thing.")] * 40)


def build(model: ScriptedModel, limits: SpendLimits, **extra: Any) -> Service:
    """A service with a three-archetype roster behind it."""
    return Service(
        SpendGate(SpendGuard(limits), lambda: model),
        visitors=VisitorDesk(
            StaticRoster(
                [fixture(f"demo-{n:04d}", p) for n, p in enumerate(ARCHETYPES, start=1)]
            )
        ),
        **extra,
    )


@pytest.fixture
def client(model: ScriptedModel, limits: SpendLimits) -> Iterator[TestClient]:
    with TestClient(create_app(build(model, limits))) as running:
        yield running


# ---------------------------------------------------------------------------
# #66 -- assignment, and the roster that makes it possible
# ---------------------------------------------------------------------------


def test_the_shipped_roster_is_the_real_export_and_every_row_is_populated() -> None:
    """An empty account is how this demo dies, so the fallback is not empty.

    These are the rows ``ACCOUNTS.persona_fixtures`` holds, not invented ones:
    ``StaticRoster`` drops anything without order history, a home store and a
    points balance, so a count that survives is a count of usable accounts.
    """
    roster = shipped_roster()
    fixtures = roster.fixtures()

    assert SHIPPED_ROSTER_PATH.exists()
    assert len(fixtures) == 28
    assert len({row.persona_id for row in fixtures}) == 7
    assert all(row.populated and row.narrative for row in fixtures)


def test_a_deployment_with_no_connection_still_assigns_a_loaded_persona() -> None:
    """``build_visitors(None)`` is every deployment today. It must not be empty."""
    desk, pool = build_visitors(None)

    assert pool is None
    assert desk.roster.fixtures()


def test_name_to_persona_to_conversation_is_one_request(client: TestClient) -> None:
    """#66's first criterion: one screen, and the opening message comes with it."""
    body = client.post("/api/entry", json={"name": "Sam"}).json()

    assert body["visitor"]["label"]
    assert body["opening"].startswith("Hi Sam.")
    assert len(body["chips"]) >= 3
    assert len({chip["lane"] for chip in body["chips"]}) >= 3


def test_two_concurrent_sessions_get_different_personas(
    model: ScriptedModel, limits: SpendLimits
) -> None:
    """#66's second criterion, through the real request path and two cookies."""
    service = build(model, limits)
    application = create_app(service)
    seen = set()
    for name in ("Sam", "Alex", "Robin"):
        with TestClient(application) as visitor:
            seen.add(
                visitor.post("/api/entry", json={"name": name}).json()["visitor"][
                    "persona_id"
                ]
            )

    assert seen == set(ARCHETYPES)


def test_an_unpopulated_deployment_says_so_rather_than_inventing_an_account(
    model: ScriptedModel, limits: SpendLimits
) -> None:
    """``None`` is a decided state, and the copy is what makes it decided."""
    service = Service(
        SpendGate(SpendGuard(limits), lambda: model),
        visitors=VisitorDesk(StaticRoster()),
    )
    with TestClient(create_app(service)) as visitor:
        body = visitor.post("/api/entry", json={"name": "Sam"}).json()

    assert body["visitor"] is None
    assert "no synthetic accounts loaded" in body["opening"]


# ---------------------------------------------------------------------------
# #69 -- the switch
# ---------------------------------------------------------------------------


def test_switching_starts_a_new_session_and_a_different_archetype(
    client: TestClient,
) -> None:
    """*A new* ``demo_id`` *on a clean connection, not a mutation.*"""
    first = client.post("/api/entry", json={"name": "Sam"}).json()
    before = client.cookies.get(SESSION_COOKIE)

    second = client.post("/api/switch", json={}).json()
    after = client.cookies.get(SESSION_COOKIE)

    assert after != before
    assert second["visitor"]["persona_id"] != first["visitor"]["persona_id"]
    assert second["restarted"] is True


def test_the_switch_says_the_conversation_restarted(client: TestClient) -> None:
    """#69's second criterion, in the sentence the visitor reads."""
    client.post("/api/entry", json={"name": "Sam"})

    opening = client.post("/api/switch", json={}).json()["opening"]

    assert opening.startswith("Starting over.")
    assert "belonged to somebody else" in opening


def test_the_old_binding_is_released_rather_than_reassigned(
    model: ScriptedModel, limits: SpendLimits
) -> None:
    """The store is what the pool resolves against, so release has to be real."""
    service = build(model, limits)
    with TestClient(create_app(service)) as visitor:
        visitor.post("/api/entry", json={"name": "Sam"})
        leaving = visitor.cookies.get(SESSION_COOKIE) or ""
        visitor.post("/api/switch", json={})
        arriving = visitor.cookies.get(SESSION_COOKIE) or ""

    store = service.visitors.store
    assert store.session(leaving) is None
    assert store.demo_id_for(leaving) is None
    assert store.session(arriving) is not None


def test_no_conversation_survives_a_switch(
    model: ScriptedModel, limits: SpendLimits
) -> None:
    """#69: *no data from the previous persona survives into the new one*."""
    service = build(model, limits)
    with TestClient(create_app(service)) as visitor:
        visitor.post("/api/entry", json={"name": "Sam"})
        leaving = visitor.cookies.get(SESSION_COOKIE)
        visitor.post("/api/chat", json={"message": "what is my usual?"})
        assert len(service.sessions) == 1

        visitor.post("/api/switch", json={})

    # Not "unreachable" -- gone. The transcript the old persona built is not
    # sitting in the store waiting for a cookie that could be replayed at it.
    assert len(service.sessions) == 0
    assert leaving is not None


def test_the_carried_name_survives_but_the_persona_does_not(
    client: TestClient,
) -> None:
    """The visitor is changing who they shop as, not who they are."""
    client.post("/api/entry", json={"name": "Sam"})

    switched = client.post("/api/switch", json={}).json()

    assert switched["visitor"]["display_name"] == "Sam"
    assert switched["opening"].startswith("Starting over.")


# ---------------------------------------------------------------------------
# #68 -- the card, edited in place
# ---------------------------------------------------------------------------


def ordering_model() -> ScriptedModel:
    return ScriptedModel(
        calls_tool(ToolName.PROPOSE_ORDER, {"items": [{"item_id": "BOWL-CHICKEN"}]}),
        answer("That is $10.70 -- press Place order."),
    )


def test_editing_a_card_produces_a_new_priced_draft(limits: SpendLimits) -> None:
    """#68's second criterion, and the reason an edit is not a mutation."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        first = visitor.post("/api/chat", json={"message": "a chicken bowl"}).json()
        card = first["card"]

        edited = visitor.post(
            "/api/draft/revise",
            json={
                "draft_id": card["draft_id"],
                "lines": [{"item_id": "BOWL-CHICKEN", "quantity": 2}],
            },
        ).json()

    assert edited["card"]["draft_id"] != card["draft_id"]
    assert edited["card"]["total"] != card["total"]
    assert edited["card"]["requires_confirmation"] is True


def test_an_edited_card_is_unconfirmed_and_places_the_edited_order(
    limits: SpendLimits,
) -> None:
    """Confirming the *new* draft is what places it, and it places the edit."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        card = visitor.post("/api/chat", json={"message": "a chicken bowl"}).json()[
            "card"
        ]
        edited = visitor.post(
            "/api/draft/revise",
            json={
                "draft_id": card["draft_id"],
                "lines": [{"item_id": "BOWL-CHICKEN", "quantity": 2}],
            },
        ).json()["card"]
        model._replies.extend(
            [
                calls_tool(ToolName.PLACE_ORDER, {"draft_id": edited["draft_id"]}),
                answer("Ordered. Simulated, of course."),
            ]
        )
        placed = visitor.post(
            "/api/chat",
            json={"message": "yes", "confirm_draft_id": edited["draft_id"]},
        ).json()

    assert placed["receipt"] is True
    assert placed["card"]["lines"][0]["quantity"] == 2


def test_an_edit_that_does_not_price_up_leaves_the_visitor_a_sentence(
    limits: SpendLimits,
) -> None:
    """A rejection is a normal answer, not a fault."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        card = visitor.post("/api/chat", json={"message": "a chicken bowl"}).json()[
            "card"
        ]
        refused = visitor.post(
            "/api/draft/revise",
            json={
                "draft_id": card["draft_id"],
                "lines": [{"item_id": "NOT-ON-THE-MENU", "quantity": 1}],
            },
        ).json()

    assert refused["card"] is None
    assert "could not re-price" in refused["reply"]


def test_an_edit_calls_no_model(limits: SpendLimits) -> None:
    """An edit is a lookup and an arithmetic. It costs nothing and waits on nothing."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        card = visitor.post("/api/chat", json={"message": "a chicken bowl"}).json()[
            "card"
        ]
        before = model.call_count
        visitor.post(
            "/api/draft/revise",
            json={
                "draft_id": card["draft_id"],
                "lines": [{"item_id": "BOWL-CHICKEN", "quantity": 3}],
            },
        )

    assert model.call_count == before


def test_every_card_and_every_receipt_says_it_is_simulated(
    limits: SpendLimits,
) -> None:
    """#68's fourth criterion, on the payload rather than only in the markup."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        card = visitor.post("/api/chat", json={"message": "a chicken bowl"}).json()[
            "card"
        ]
        model._replies.extend(
            [
                calls_tool(ToolName.PLACE_ORDER, {"draft_id": card["draft_id"]}),
                answer("Ordered."),
            ]
        )
        receipt = visitor.post(
            "/api/chat",
            json={"message": "yes", "confirm_draft_id": card["draft_id"]},
        ).json()["card"]

    assert "simulated" in card["notice"].lower()
    assert "simulated" in receipt["notice"].lower()


# ---------------------------------------------------------------------------
# #68 -- streaming
# ---------------------------------------------------------------------------


def frames(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_a_turn_can_be_read_as_frames(limits: SpendLimits) -> None:
    """The transport a two-second turn paints into."""
    model = ordering_model()
    with TestClient(create_app(build(model, limits))) as visitor:
        response = visitor.post(
            "/api/chat",
            json={"message": "a chicken bowl"},
            headers={"Accept": "application/x-ndjson"},
        )

    kinds = [frame["type"] for frame in frames(response)]
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert kinds[0] == "open"
    assert "card" in kinds
    assert kinds[-1] == "end"


def test_the_object_and_the_frames_are_the_same_turn(limits: SpendLimits) -> None:
    """Two renderings, one definition. A caller that does not ask still gets JSON."""
    model = ScriptedModel(answer("Barbacoa is the spicier one."), answer("Again."))
    with TestClient(create_app(build(model, limits))) as visitor:
        plain = visitor.post("/api/chat", json={"message": "is barbacoa spicy?"}).json()
        streamed = visitor.post(
            "/api/chat",
            json={"message": "and again?"},
            headers={"Accept": "application/x-ndjson"},
        )

    text = "".join(frame["text"] for frame in frames(streamed) if frame["type"] == "text")
    assert plain["reply"] == "Barbacoa is the spicier one."
    assert text == "Again."


# ---------------------------------------------------------------------------
# #68 -- photographs
# ---------------------------------------------------------------------------


def test_a_deployment_with_no_photo_intake_declines_rather_than_loses_the_photo(
    client: TestClient,
) -> None:
    body = client.post("/api/photo", content=b"not-an-image").json()

    assert body["photo"] is None
    assert "not wired up" in body["reply"]


def test_a_reference_this_session_did_not_upload_names_no_photograph() -> None:
    """A well-formed reference belonging to somebody else is a not-found."""
    registry = PhotoRegistry()
    registry.record("session-a", "uploads/2026-08-27/mine.jpg")

    assert registry.holds("session-a", "uploads/2026-08-27/mine.jpg")
    assert not registry.holds("session-b", "uploads/2026-08-27/mine.jpg")


def test_a_switch_forgets_the_old_sessions_photographs() -> None:
    registry = PhotoRegistry()
    registry.record("session-a", "uploads/2026-08-27/mine.jpg")

    registry.release("session-a")

    assert not registry.holds("session-a", "uploads/2026-08-27/mine.jpg")


def test_a_borrowed_reference_never_reaches_the_model(limits: SpendLimits) -> None:
    """The reference is checked before it is attached to the visitor's message."""
    model = ScriptedModel(answer("I cannot see a photo."))
    with TestClient(create_app(build(model, limits))) as visitor:
        visitor.post(
            "/api/chat",
            json={"message": "what is this?", "photo": "uploads/2026-08-27/theirs.jpg"},
        )

    sent = model.requests[0]
    assert not any("theirs.jpg" in str(message) for message in sent)


# ---------------------------------------------------------------------------
# #70 -- the framing that is the server's job rather than the page's
# ---------------------------------------------------------------------------


def test_noindex_is_asserted_by_the_header_as_well_as_the_tag(
    client: TestClient,
) -> None:
    """Both halves, because something that only fetches never runs the tag."""
    for path in ("/", "/robots.txt", "/healthz"):
        assert client.get(path).headers["x-robots-tag"] == "noindex, nofollow"
    assert "Disallow: /" in client.get("/robots.txt").text


def test_no_endpoint_serves_a_bulk_export_of_the_corpus(client: TestClient) -> None:
    """#70: *the menu data is cached for the demo, not republished as a dataset.*

    Asserted as an absence of routes rather than as a 404 on a list somebody
    thought of: the application has five POST routes and three GETs, and the
    GETs are a page, a robots file and a probe.
    """
    application = client.app
    readable = {
        route.path
        for route in application.routes  # type: ignore[attr-defined]
        if "GET" in (getattr(route, "methods", None) or ())
    }

    assert readable == {"/", "/robots.txt", "/healthz"}
