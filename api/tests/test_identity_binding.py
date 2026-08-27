"""The edge that does not exist, at the tier where a request arrives.

RFC-001 §05's whole argument is one sentence about absence:

    the edge that would let the model choose a visitor does not exist -- not
    blocked at runtime, simply absent from every signature.

That claim has been held structurally at three tiers already: the tool surface
(``agent/tests/test_surface.py``), the stored procedures
(``snowflake/tests/test_procedure_layout.py``) and the ops API
(``api/tests/test_ops.py``, against
:data:`~chip_chat.snowflake.procedures.IDENTITY_VOCABULARY`). This module is the
tier that was missing: the **request**. Issue #66's third acceptance criterion
is *no endpoint accepts a* ``demo_id`` *from a client or from a tool result*,
and a criterion about what an endpoint accepts is a criterion about the schema
rather than about the handler.

So these tests fail when the *shape* changes and not when the output does.
Deleting ``extra="forbid"``, adding a visitor field to a request model, or
growing :meth:`~chip_chat.api.visitors.VisitorDesk.admit` a ``demo_id``
parameter each fail something here while leaving every happy path green.

The one word that is allowed, and why: ``session_id``. It is in
``IDENTITY_VOCABULARY`` because a *procedure* argument spelled that way would
be an identity a caller supplies, but at this tier the session id is the thing
the app minted and the cookie carried, and resolving a visitor from it is
precisely the trusted path. What must never appear is the resolved identity
itself.
"""

import inspect
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from chip_chat.agent.testing import ScriptedModel, answer
from chip_chat.api import app as app_module
from chip_chat.api.app import (
    SESSION_COOKIE,
    ChatRequest,
    EntryRequest,
    ReviseLine,
    ReviseRequest,
    Service,
    SwitchRequest,
    create_app,
)
from chip_chat.api.guard import SpendGuard
from chip_chat.api.limits import SpendLimits
from chip_chat.api.pool import VisitorPool
from chip_chat.api.testing import FakeAccount, FakeClock, OrderRow
from chip_chat.api.turns import SpendGate
from chip_chat.api.visitors import (
    ROSTER_COLUMNS,
    PersonaFixture,
    SnowflakeRoster,
    StaticRoster,
    VisitorDesk,
    VisitorSessionStore,
)
from chip_chat.snowflake.procedures import IDENTITY_VOCABULARY

REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    ChatRequest,
    EntryRequest,
    SwitchRequest,
    ReviseRequest,
    ReviseLine,
)
"""Every model FastAPI parses a request body into. The list a new route grows.

``ReviseLine`` is nested inside ``ReviseRequest`` rather than parsed on its own,
and it is here anyway: a nested model is still a place a field could be added,
and the point of this list is that every such place is checked rather than the
top-level ones.
"""

FORBIDDEN = frozenset(IDENTITY_VOCABULARY) - {"session_id"}
"""Every word :data:`IDENTITY_VOCABULARY` names, less the cookie's own value.

``session_id`` is exempt for the reason the module docstring gives: at this tier
it is what the app minted, not what a caller asserted, and resolving a visitor
from it *is* the trusted path.
"""


def populated(demo_id: str, persona_id: str) -> PersonaFixture:
    return PersonaFixture(
        demo_id=demo_id,
        persona_id=persona_id,
        label=persona_id.title(),
        home_store=679,
        home_store_name="Ballard",
        points_balance=1_340,
        usual_item_id="CMG-1",
        order_count=42,
        narrative="Same bowl, same store, nearly every week.",
    )


def roster_row(item: PersonaFixture) -> tuple[object, ...]:
    values = {
        "demo_id": item.demo_id,
        "persona_id": item.persona_id,
        "label": item.label,
        "rank": item.rank,
        "home_store": item.home_store,
        "home_store_name": item.home_store_name,
        "points_balance": item.points_balance,
        "usual_item_id": item.usual_item_id,
        "order_count": item.order_count,
        "lifetime_spend": item.lifetime_spend,
        "narrative": item.narrative,
    }
    return tuple(values[column] for column in ROSTER_COLUMNS)


@pytest.fixture
def service() -> Service:
    return Service(
        SpendGate(
            SpendGuard(SpendLimits(), kill_switch=None, clock=FakeClock()),
            lambda: ScriptedModel(*[answer("Sure thing.")] * 20),
        ),
        visitors=VisitorDesk(
            StaticRoster(
                [populated("dm-000001", "regular"), populated("dm-000002", "explorer")]
            ),
            clock=FakeClock(),
        ),
    )


@pytest.fixture
def client(service: Service) -> Iterator[TestClient]:
    with TestClient(create_app(service)) as running:
        yield running


# ---------------------------------------------------------------------------
# The schema. What a body may say.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda item: item.__name__)
def test_no_request_model_has_a_field_that_names_a_visitor(
    model: type[BaseModel],
) -> None:
    """The same absence the tool surface and the procedures are held to."""
    for name in model.model_fields:
        assert name.lower() not in FORBIDDEN


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda item: item.__name__)
def test_every_request_model_refuses_unknown_fields(model: type[BaseModel]) -> None:
    """Ignoring an extra field means somebody has to prove nothing reads it."""
    assert model.model_config.get("extra") == "forbid"


def test_a_chat_body_carrying_a_demo_id_is_refused(client: TestClient) -> None:
    refused = client.post("/api/chat", json={"message": "hello", "demo_id": "dm-000002"})

    assert refused.status_code == 422


def test_a_switch_body_carrying_a_demo_id_is_refused(client: TestClient) -> None:
    """The switcher is the request that chooses a persona. It has no fields."""
    refused = client.post("/api/switch", json={"demo_id": "dm-000002"})

    assert refused.status_code == 422
    assert not SwitchRequest.model_fields


def test_a_revise_body_carrying_a_demo_id_is_refused(client: TestClient) -> None:
    refused = client.post(
        "/api/draft/revise",
        json={
            "draft_id": "draft-1",
            "lines": [{"item_id": "BOWL-CHICKEN", "quantity": 1}],
            "demo_id": "dm-000002",
        },
    )

    assert refused.status_code == 422


def test_an_entry_body_carrying_a_demo_id_is_refused(client: TestClient) -> None:
    refused = client.post("/api/entry", json={"name": "Sam", "demo_id": "dm-000002"})

    assert refused.status_code == 422


def test_the_assigned_account_is_not_handed_back_to_the_browser(
    client: TestClient,
) -> None:
    """A payload that never carries the identity cannot be replayed as one."""
    admitted = client.post("/api/entry", json={"name": "Sam"})

    assert admitted.status_code == 200
    body = admitted.json()
    assert body["visitor"] is not None
    assert "demo_id" not in body["visitor"]
    assert "dm-" not in admitted.text


# ---------------------------------------------------------------------------
# The signatures. What the app tier can be told.
# ---------------------------------------------------------------------------


def test_the_desk_cannot_be_told_which_visitor_to_assign() -> None:
    """``admit`` takes a session and a name. There is no third thing to get wrong."""
    parameters = set(inspect.signature(VisitorDesk.admit).parameters) - {"self"}

    assert parameters == {"session_id", "display_name"}


def test_the_desk_cannot_be_told_which_visitor_to_switch_to() -> None:
    """Two session ids and a name -- and neither id names anybody.

    The archetype a switch moves *away* from is read out of the store inside
    the desk rather than passed in, so there is no parameter through which a
    request body could steer who the visitor becomes next.
    """
    parameters = set(inspect.signature(VisitorDesk.switch).parameters) - {"self"}

    assert parameters == {"old_session_id", "new_session_id", "display_name"}


@pytest.mark.parametrize(
    "handler",
    [
        "_run_turn",
        "_profile",
        "_persona",
        "_entry_reply",
        "_revise",
        "_with_photo",
        "_session_id",
        "_source_address",
    ],
)
def test_no_request_helper_accepts_a_visitor_identifier(handler: str) -> None:
    signature = inspect.signature(getattr(app_module, handler))

    for name in signature.parameters:
        assert name.lower() not in FORBIDDEN


def test_the_only_identity_input_to_the_pool_is_a_session_id() -> None:
    """``for_session`` is the seam, and it is spelled that way on purpose."""
    parameters = set(inspect.signature(VisitorPool.for_session).parameters) - {"self"}

    assert parameters == {"session_id"}


# ---------------------------------------------------------------------------
# The behaviour the absence buys: two sessions, two accounts, no crossing.
# ---------------------------------------------------------------------------


def test_two_sessions_see_only_their_own_rows() -> None:
    """Issue #66's second criterion, all the way down to the connection."""
    account = FakeAccount(
        orders=[
            OrderRow("dm-000001", "ord-1"),
            OrderRow("dm-000002", "ord-2"),
        ],
        fixtures=[
            roster_row(populated("dm-000001", "regular")),
            roster_row(populated("dm-000002", "explorer")),
        ],
    )
    store = VisitorSessionStore()
    pool = VisitorPool(account.connect, sessions=store, size=2)
    desk = VisitorDesk(SnowflakeRoster(pool), store=store)

    first = desk.admit("session-a")
    second = desk.admit("session-b")
    assert first is not None
    assert second is not None
    assert first.demo_id != second.demo_id

    with pool.for_session("session-a") as connection:
        mine = [row[0] for row in connection.execute("SELECT order_id FROM orders")]
    with pool.for_session("session-b") as connection:
        theirs = [row[0] for row in connection.execute("SELECT order_id FROM orders")]

    assert set(mine).isdisjoint(theirs)
    assert pool.stats.stale_discarded == 0


def test_a_second_browser_does_not_inherit_the_first_ones_account(
    client: TestClient,
) -> None:
    """Two cookies, two personas, through the real request path."""
    first = client.post("/api/entry", json={"name": "Sam"})
    client.cookies.delete(SESSION_COOKIE)
    second = client.post("/api/entry", json={"name": "Alex"})

    assert first.json()["visitor"]["persona_id"] != second.json()["visitor"]["persona_id"]


def test_a_switch_hands_back_no_identity_either(client: TestClient) -> None:
    """The reply that says who you have become still cannot say who that is."""
    client.post("/api/entry", json={"name": "Sam"})

    switched = client.post("/api/switch", json={})

    assert switched.status_code == 200
    assert "demo_id" not in switched.json()["visitor"]
    assert "dm-" not in switched.text


def test_a_returning_cookie_resumes_rather_than_reassigns(client: TestClient) -> None:
    """#9 decided visitor state persists between visits."""
    first: dict[str, Any] = client.post("/api/entry", json={"name": "Sam"}).json()
    second: dict[str, Any] = client.post("/api/entry", json={}).json()

    assert second["visitor"]["persona_id"] == first["visitor"]["persona_id"]
    assert second["visitor"]["display_name"] == "Sam"
