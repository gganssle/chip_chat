"""Which lanes a deployment assembles, and what each one refuses to guess.

`build_lanes` used to answer a question narrower than its name: it returned the
two Snowflake-backed lanes or :data:`~chip_chat.agent.lanes.NO_LANES`, and the
knowledge and photo lanes were left as tickets in its docstring. The cost of
that was not theoretical and it is what GitHub #106 reported. With no knowledge
lane, ``search_menu_knowledge`` answers out of
:data:`chip_chat.agent.hardcoded.MENU` — which is three items — so a demo whose
Snowflake account held the whole published catalogue would still have told the
visitor the menu was a chicken bowl, a steak burrito and a side of chips.

These tests are about the *decisions* the two new builders make, not about
retrieval or about describing a photograph, both of which have their own suites
one layer down. There are three of them and they are the ones a deployment gets
wrong:

1. **An absent backing service is a ``None`` and never an exception.** Every
   builder here is on the start-up path of a container that must come up and
   serve ``/healthz`` even when half the estate is missing.
2. **An absent lane does not take its neighbours with it.** The pool check used
   to return the constant, so a deployment with no Snowflake credential got no
   knowledge lane either — for no reason beyond where the early return sat.
3. **Nothing touches the network to decide.** The measurement
   `chip_chat.api.app.build_service` records is three seconds from import to
   "Uvicorn running", and a credential chain resolved per lane would spend it.
"""

from typing import Any, cast

import pytest

from chip_chat.agent.lanes import Lanes
from chip_chat.agent.tools import offered_tools
from chip_chat.api import app as app_module
from chip_chat.api.app import build_knowledge_lane, build_lanes, build_photo_lane
from chip_chat.api.pool import VisitorPool
from chip_chat.catalog import MenuCatalog
from chip_chat.otel import ToolName
from chip_chat.search.lane import KnowledgeLane
from chip_chat.vision import PhotoLane

SEARCH_VARIABLES = ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_ALIAS")
PHOTO_VARIABLES = (
    "CHIP_CHAT_VISION_VOCABULARY",
    "CHIP_CHAT_FOUNDRY_ENDPOINT",
    "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT",
    "AZURE_STORAGE_ACCOUNT",
    "AZURE_UPLOADS_CONTAINER",
)


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a deployment that has been told nothing.

    Autouse because the failure this guards against is a test that passes on a
    laptop with a populated ``.env`` and fails in CI, or the reverse.
    """
    for name in SEARCH_VARIABLES + PHOTO_VARIABLES:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


def test_no_search_endpoint_is_no_knowledge_lane() -> None:
    """And a ``None`` rather than the ``ServiceError`` the reader raises.

    ``endpoint_from_env`` is right to raise: it is called by a rebuild, and a
    rebuild pointed at nothing should stop. This is the other caller, and here
    the honest answer is the week-one slice.
    """
    assert build_knowledge_lane() is None


def test_a_search_endpoint_is_a_knowledge_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")

    assert isinstance(build_knowledge_lane(), KnowledgeLane)


def test_the_lane_queries_the_alias_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """An alias, and never an index name — RFC-001 section 08's whole point."""
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")
    monkeypatch.setenv("AZURE_SEARCH_INDEX_ALIAS", "corpus-blue")

    assert _retriever_kwargs(monkeypatch)["alias"] == "corpus-blue"


def test_an_unset_alias_falls_back_to_the_schema_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the endpoint, this one has a defensible default and takes it.

    The alias is named by ``chip_chat.search.schema`` and created by the build;
    there is exactly one of it and guessing it wrong is not a failure mode a
    deployment can reach. The endpoint is an account name and guessing it is.
    """
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")

    assert _retriever_kwargs(monkeypatch)["alias"] == "corpus"


def test_an_empty_alias_is_read_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terraform writes ``""`` for a variable nobody set; that is not an alias."""
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")
    monkeypatch.setenv("AZURE_SEARCH_INDEX_ALIAS", "   ")

    assert _retriever_kwargs(monkeypatch)["alias"] == "corpus"


def test_building_the_lane_resolves_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token source defers its chain, and this is what holds it there.

    A ``DefaultAzureCredential`` built at assembly is a managed-identity round
    trip on the start-up path, per lane, on every cold start of an app that
    scales to zero.
    """
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("build_knowledge_lane resolved a credential")

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", refuse, raising=False)
    assert build_knowledge_lane() is not None


# ---------------------------------------------------------------------------
# Photo
# ---------------------------------------------------------------------------


def test_no_published_catalogue_is_no_photo_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """The matcher resolves against catalogue rows; there is nothing to fake."""
    monkeypatch.setattr(app_module, "build_catalog", lambda: None)

    assert build_photo_lane() is None


def test_a_catalogue_without_a_vocabulary_is_no_photo_lane(
    monkeypatch: pytest.MonkeyPatch, catalog: MenuCatalog
) -> None:
    """RFC-001 section 07 has no fallback vocabulary and this is where that lands.

    A describer built from a built-in list would be the hand-maintained
    vocabulary the generation exists to prevent, and it would be reached on
    exactly the deployment where the build step was forgotten. So the lane is
    withheld and ``match_meal_from_photo`` is never offered.
    """
    monkeypatch.setattr(app_module, "build_catalog", lambda: catalog)

    assert build_photo_lane() is None


def test_a_photo_lane_needs_every_one_of_its_three_parts(
    monkeypatch: pytest.MonkeyPatch, catalog: MenuCatalog
) -> None:
    monkeypatch.setattr(app_module, "build_catalog", lambda: catalog)
    monkeypatch.setattr(app_module, "Vocabulary", _Stub())
    monkeypatch.setattr(app_module, "AzureVisionModel", _Stub())
    monkeypatch.setattr(app_module, "AzureBlobStore", _Stub())

    assert isinstance(build_photo_lane(), PhotoLane)


# ---------------------------------------------------------------------------
# The four together
# ---------------------------------------------------------------------------


def test_no_snowflake_pool_no_longer_withdraws_the_other_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this rearrangement was for.

    ``build_lanes(None)`` returned :data:`~chip_chat.agent.lanes.NO_LANES`,
    which is right about the two lanes that need a connection and silently wrong
    about the two that do not. Nothing about retrieval needs Snowflake.
    """
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://srch.example.test")

    lanes = build_lanes(None)

    assert lanes.knowledge is not None
    assert lanes.account is None
    assert lanes.personalization is None


def test_a_deployment_told_nothing_wires_nothing_and_does_not_raise() -> None:
    """The week-one slice, still reachable, still an honest state."""
    lanes = build_lanes(None)

    assert lanes.describe() == {
        "knowledge": False,
        "account": False,
        "personalization": False,
        "photo": False,
    }


# ---------------------------------------------------------------------------
# The tool a wired lane cannot answer
# ---------------------------------------------------------------------------


def test_the_recommendations_tool_is_withheld_rather_than_left_declining() -> None:
    """``chip-znk``, at the one place that knows which tables exist.

    ``CHIP_CHAT.MARTS.recommendations`` is not on the account and RFC-001 §04 is
    why nothing publishes it, so a deployment that offered
    ``get_recommendations`` offered a name every call of which came back
    ``PERSONALIZATION_LANE_UNAVAILABLE``. The withdrawal is a fact about this
    deployment's data, which is why it is spelled in ``api/`` and not in the
    agent's lane vocabulary.
    """
    lanes = build_lanes(None)

    assert ToolName.GET_RECOMMENDATIONS in lanes.withheld
    assert ToolName.GET_RECOMMENDATIONS not in offered_tools(lanes)


def test_withholding_the_tool_does_not_withhold_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowness is the point, and it is what ``cc-lpy4`` bought.

    Wiring personalization is what moved ``get_usual_order`` off the hardcoded
    fixture -- half of ``docs/public-demo.md`` §9. A fix that took the lane away
    to take one tool away would hand that back.
    """
    lanes = _snowflake_backed(monkeypatch)

    assert lanes.personalization is not None
    assert ToolName.GET_USUAL_ORDER in offered_tools(lanes)
    assert ToolName.GET_RECOMMENDATIONS not in offered_tools(lanes)


def test_the_withdrawal_is_reported_rather_than_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the start-up log prints and ``GET /healthz/lanes`` renders.

    Offered-and-declining was at least visible in a trace. A tool that simply
    stopped existing, with nothing anywhere saying so, would be the same defect
    with the evidence removed.
    """
    lanes = _snowflake_backed(monkeypatch)

    assert lanes.withdrawn() == (ToolName.GET_RECOMMENDATIONS,)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Stub:
    """Stands in for a class whose ``from_env`` would reach for a credential.

    The photo lane's three parts each build an SDK client, and none of what
    those clients do is what these tests are about — the question here is
    whether the builder demanded all three before returning a lane.
    """

    content_version: str | None = None
    """Read by the start-up log line, which names both catalogue builds."""

    account: str = "acct.example"
    """Read by ``_analyst_host`` when it derives a REST hostname."""

    def from_env(self) -> "_Stub":
        return self

    def for_session(self, session_id: str) -> "_Stub":
        """Stand in for :meth:`chip_chat.api.pool.VisitorPool.for_session`."""
        del session_id
        return self


def _snowflake_backed(monkeypatch: pytest.MonkeyPatch) -> Lanes:
    """Return what ``build_lanes`` assembles on a deployment that has a pool.

    Everything between the pool and the two Snowflake-backed lanes is stubbed --
    the settings, the key, the JWT and the Analyst transport -- because none of
    it is what these two tests are about and all of it would want a credential.
    What is left real is the branch under test: which lanes come back, and which
    tool name is withheld from them.
    """
    monkeypatch.setattr(app_module, "SnowflakeSettings", _Stub())
    for name in ("PrivateKey", "KeyPairJwt", "HttpAnalystTransport"):
        monkeypatch.setattr(app_module, name, lambda *a, **k: _Stub())
    monkeypatch.setattr(app_module, "pooled_client", lambda: _Stub())
    monkeypatch.setattr(app_module, "AccountLane", lambda *a, **k: _Stub())
    monkeypatch.setattr(app_module, "PersonalizationLane", lambda *a, **k: _Stub())
    # The pool is a `for_session` and nothing else, which is
    # `chip_chat.snowflake.reads.SessionCheckout`'s whole shape.
    return build_lanes(cast(VisitorPool, _Stub()))


def _retriever_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return the keyword arguments ``build_knowledge_lane`` builds a retriever with."""
    captured: dict[str, Any] = {}
    real = app_module.Retriever

    def capture(service: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(service, **kwargs)

    monkeypatch.setattr(app_module, "Retriever", capture)
    build_knowledge_lane()
    return captured
