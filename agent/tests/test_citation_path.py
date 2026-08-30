"""The whole of decision D9, driven end to end on the request path.

``agent/tests/test_envelope.py`` asserts the pieces --
:func:`~chip_chat.agent.envelope.parse` reading a reply,
:func:`~chip_chat.agent.envelope.render` resolving an id. This file asserts that
they are *joined up*, which is what bead ``chip-2ky`` was about: every piece was
correct, tested and reachable only from ``eval/``, and the consequence on the
deployment was that every food answer ended with a visible line of JSON.

So what is driven here is a real turn. A real
:class:`~chip_chat.search.lane.KnowledgeLane` over one hit, a scripted model that
searches and then answers the way the deployed one did, and assertions on the
two things a visitor and an auditor respectively care about: the prose has no
JSON in it, and every citation under it came off the retrieval rather than off
the model.

**The security half is asserted here as well as in ``test_sabotage.py``**, and
deliberately not by constructing a :class:`~chip_chat.agent.envelope.ModelResponse`
by hand. A parser is a new way for a minted id to arrive, so the test that a
minted id is dropped has to run through the parser to be worth anything.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from chip_chat.agent.envelope import ClaimClass
from chip_chat.agent.lanes import Lanes
from chip_chat.agent.loop import Conversation, TurnResult, run_turn
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.testing import ScriptedModel, answer, calls_tool
from chip_chat.agent.tools import offered_tools
from chip_chat.otel import ToolName, chat_turn
from chip_chat.search.chunks import (
    CHUNK_ID,
    HARVESTED_AT,
    HEADING,
    KIND,
    SOURCE_URL,
    TEXT,
)
from chip_chat.search.lane import KnowledgeLane
from chip_chat.search.retrieve import Retriever

SESSION = "sess-cite"
BARBACOA_ID = "menu-barbacoa-1"
BARBACOA_URL = "https://www.chipotle.com/menu/barbacoa"
HARVESTED = "2026-08-24T03:11:00+00:00"


class _OneMenuHit:
    """A search service that returns one citable passage and nothing else.

    Narrow on purpose: the eight methods a rebuild would call raise, so a test
    that wandered onto the write path fails loudly rather than doing nothing.
    """

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        del target, query
        return {
            "value": [
                {
                    CHUNK_ID: BARBACOA_ID,
                    KIND: "MENU_ITEM",
                    TEXT: "Barbacoa is braised with chipotle chiles and cumin.",
                    HEADING: "Barbacoa",
                    SOURCE_URL: BARBACOA_URL,
                    HARVESTED_AT: HARVESTED,
                    "@search.score": 0.031,
                    "@search.rerankerScore": 2.7,
                }
            ]
        }

    def index_names(self) -> list[str]:
        raise NotImplementedError

    def create_index(self, definition: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def delete_index(self, name: str) -> None:
        raise NotImplementedError

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        raise NotImplementedError

    def document_count(self, index: str) -> int:
        raise NotImplementedError

    def alias_target(self, alias: str) -> str | None:
        raise NotImplementedError

    def set_alias(self, alias: str, index: str) -> None:
        raise NotImplementedError

    def delete_alias(self, alias: str) -> None:
        raise NotImplementedError


@pytest.fixture
def lanes() -> Lanes:
    """A deployment with the knowledge lane wired, which is the one that cites."""
    return Lanes(knowledge=KnowledgeLane(Retriever(_OneMenuHit())))


@pytest.fixture
def desk() -> OrderDesk:
    return OrderDesk()


def _turn(model: ScriptedModel, lanes: Lanes, desk: OrderDesk) -> TurnResult:
    """Run one turn the way the request handler does, inside a ``chat.turn``."""
    conversation = Conversation(
        session_id=SESSION, tools=offered_tools(lanes, desk), lanes=lanes
    )
    with chat_turn(session_id=SESSION, turn_index=0, message="is the barbacoa spicy"):
        return run_turn(
            conversation,
            "is the barbacoa spicy",
            model=model,
            desk=desk,
            lanes=lanes,
        )


def _searched_then_said(text: str) -> ScriptedModel:
    """A model that searches the corpus and then answers ``text``.

    Two round trips, which is the shape every cited answer has: there is nothing
    to cite until ``search_menu_knowledge`` has returned.
    """
    return ScriptedModel(
        calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "barbacoa spicy"}),
        answer(text),
    )


# ---------------------------------------------------------------------------
# What the visitor reads
# ---------------------------------------------------------------------------


def test_the_envelope_json_never_reaches_the_reply(lanes: Lanes, desk: OrderDesk) -> None:
    """``chip-2ky``, as the visitor met it.

    The reply string is what the widget paints and what ``render.response``
    records, so this assertion is the bug.
    """
    model = _searched_then_said(
        "Moderately. It's braised with chipotle chiles and cumin.\n"
        f'{{"claim_class":"food","citations":["{BARBACOA_ID}"]}}'
    )

    result = _turn(model, lanes, desk)

    assert result.reply == "Moderately. It's braised with chipotle chiles and cumin."
    assert "claim_class" not in result.reply
    assert "{" not in result.reply


def test_the_citation_carries_the_retriever_s_fields_and_not_the_model_s(
    lanes: Lanes, desk: OrderDesk
) -> None:
    """The turn hands the app four fields, none of which crossed from the model.

    D9's *"the model names ids and the app draws the citation"*, asserted on the
    object the API serialises rather than on a unit somewhere upstream.
    """
    model = _searched_then_said(
        f'Moderately.\n{{"claim_class":"food","citations":["{BARBACOA_ID}"]}}'
    )

    result = _turn(model, lanes, desk)

    assert [citation.as_dict() for citation in result.citations] == [
        {
            "id": BARBACOA_ID,
            "label": "Menu · Barbacoa",
            "source_url": BARBACOA_URL,
            "harvested_at": HARVESTED,
        }
    ]
    assert result.claim_class is ClaimClass.FOOD


def test_a_source_the_retriever_never_returned_is_dropped_and_counted(
    lanes: Lanes, desk: OrderDesk
) -> None:
    """A parser is a new way for a minted id to arrive, so it is tested through one.

    The claim survives, uncited, and the violation is recorded rather than
    tidied away -- which is what issue #75 counts.
    """
    model = _searched_then_said(
        'Moderately.\n{"claim_class":"food","citations":["chunk_never_retrieved"]}'
    )

    result = _turn(model, lanes, desk)

    assert result.reply == "Moderately."
    assert result.citations == ()
    assert result.envelope is not None
    assert result.envelope.dropped_citation_ids == ("chunk_never_retrieved",)
    assert result.envelope.uncited_claim


def test_a_reply_that_is_not_an_envelope_is_still_a_reply(
    lanes: Lanes, desk: OrderDesk
) -> None:
    """Most turns, and the one that must never become a crash or a blank.

    A model that ignores the citation instruction has written a worse answer.
    It has not broken the conversation, and the loop does not let it.
    """
    model = _searched_then_said("It's on the milder side of the menu.")

    result = _turn(model, lanes, desk)

    assert result.reply == "It's on the milder side of the menu."
    assert result.citations == ()
    assert result.claim_class is ClaimClass.NONE


def test_a_turn_that_never_searched_can_cite_nothing(desk: OrderDesk) -> None:
    """No retrieval, no mapping, so every id the model names resolves to nothing.

    The deployment this is really about is the week-one slice, whose hardcoded
    passages carry no ``source_url`` and therefore cannot be shown as evidence
    at all -- which the runtime context already tells the model, and which this
    makes true whether it listens or not.
    """
    model = ScriptedModel(
        answer(
            "You have 1,340 points.\n"
            f'{{"claim_class":"food","citations":["{BARBACOA_ID}"]}}'
        )
    )

    result = _turn(model, Lanes(), desk)

    assert result.reply == "You have 1,340 points."
    assert result.citations == ()
    assert result.envelope is not None
    assert result.envelope.dropped_citation_ids == (BARBACOA_ID,)


def test_the_step_ceiling_reply_carries_no_envelope(
    lanes: Lanes, desk: OrderDesk
) -> None:
    """The app speaking is not the model speaking, and it cites nothing.

    An empty envelope here would assert ``claim_class: none`` about a sentence
    no model wrote, which is a different and worse claim than saying nothing.
    """
    model = ScriptedModel(
        *[calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "barbacoa"})] * 2
    )

    conversation = Conversation(
        session_id=SESSION, tools=offered_tools(lanes, desk), lanes=lanes
    )
    with chat_turn(session_id=SESSION, turn_index=0, message="barbacoa"):
        result = run_turn(
            conversation,
            "barbacoa",
            model=model,
            desk=desk,
            lanes=lanes,
            max_steps=2,
        )

    assert result.envelope is None
    assert result.citations == ()
