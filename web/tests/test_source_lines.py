"""The source line under an answer: decision D9, drawn rather than described.

D9 settled two things and the second is the one with engineering in it:
citations are **inline by default** and they are **a field rather than a
sentence**. The consequence for this page is narrow and testable. The widget
draws a quiet trailing note under a food or policy answer, deduplicated by
source, expanding on a tap; an allergen answer gets the stricter rule -- the
harvest date visible without interaction and no deduplication, because in an
answer covering three items it has to stay unambiguous which source backs which
claim. And the widget never parses a citation out of the answer text, because a
source read off prose is a source a model could have invented.

Until bead ``chip-2ky`` there was nothing to draw. The model wrote its citation
field as a line of JSON after the answer, no layer between it and the browser
parsed it, and the visitor read
``{"claim_class":"food","citations":[...]}`` at the end of every food answer.
So the assertions here run one whole streamed turn through the page's own
``send`` -- frames in, DOM out -- rather than checking that a function exists.

Skips without a JavaScript runtime; see :mod:`browser`.
"""

import json
from pathlib import Path
from typing import Any

import browser  # type: ignore[import-not-found]
import pytest

from chip_chat.web import chat_page

# `source_url` is provenance and `public_url` is the page a person is sent to.
# They differ here the way they differ in production: the menu is read from the
# ordering API, so the first is an endpoint and the second is the published page
# `chip_chat.search.public_url` maps it to. A fixture that made them equal would
# test the one case that never needed fixing.
BARBACOA = {
    "id": "menu-barbacoa-1",
    "label": "Menu · Barbacoa",
    "source_url": "https://services.chipotle.com/menuinnovation/v1/items/barbacoa",
    "public_url": "https://www.chipotle.com/order/build-your-own",
    "harvested_at": "2026-08-24T03:11:00+00:00",
}
STEAK = {
    "id": "menu-steak-1",
    "label": "Menu · Steak",
    "source_url": "https://services.chipotle.com/menuinnovation/v1/items/steak",
    "public_url": "https://www.chipotle.com/order/build-your-own",
    "harvested_at": "2026-08-24T03:11:00+00:00",
}
UNPUBLISHED = {
    "id": "faq-1",
    "label": "FAQ · Delivery",
    "source_url": (
        "https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us"
    ),
    "public_url": "",
    "harvested_at": "2026-08-24T03:11:00+00:00",
}
BARBACOA_AGAIN = {
    "id": "menu-barbacoa-2",
    "label": "Menu · Barbacoa",
    "source_url": BARBACOA["source_url"],
    "public_url": BARBACOA["public_url"],
    "harvested_at": BARBACOA["harvested_at"],
}

_PRELUDE = """\
// One streamed turn, handed to the page as the route hands it: newline-delimited
// JSON frames over a reader. Written as bytes rather than as objects so the
// decoding is the page's own.
const FRAMES = %(frames)s;
globalThis.fetch = async (url) => {
  if (url !== '/api/chat') throw new Error('unexpected fetch: ' + url);
  const body = FRAMES.map(frame => JSON.stringify(frame) + '\\n').join('');
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {body: {getReader: () => ({
    read: async () => {
      if (sent) return {done: true};
      sent = true;
      return {done: false, value: bytes};
    },
  })}};
};
"""

_MAIN = """\
async function main() {
  await send('is the barbacoa spicy', {});
  const log = registry['log'];
  console.log(JSON.stringify({drawn: drawn(log)}));
}

main();
"""


def _frames(reply: str, citations: list[dict[str, str]], claim_class: str) -> list[Any]:
    """Build the frames ``POST /api/chat`` streams for one turn."""
    return [
        {"type": "open"},
        {"type": "text", "text": reply},
        {"type": "sources", "citations": citations, "claim_class": claim_class},
        {"type": "end", "stopped": False},
    ]


def _turn(
    tmp_path: Path,
    *,
    reply: str,
    citations: list[dict[str, str]],
    claim_class: str,
) -> dict[str, Any]:
    """Run one streamed turn through the page and return what it drew."""
    return browser.run(
        _MAIN,
        prelude=_PRELUDE % {"frames": json.dumps(_frames(reply, citations, claim_class))},
        tmp_path=tmp_path,
    )


def _answer(drawn: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the assistant's bubble out of the transcript."""
    return next(node for node in drawn if node["cls"] == "msg them")


def _sources(bubble: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the source rows inside one bubble."""
    box = next(node for node in bubble["children"] if node["cls"] == "sources")
    return box["children"]


needs_node = pytest.mark.skipif(
    not browser.available,
    reason="the executed form of this test needs a JavaScript runtime; the "
    "structural assertions below cover the same rule without one",
)


# ---------------------------------------------------------------------------
# The trailing line
# ---------------------------------------------------------------------------


@needs_node
def test_a_food_answer_shows_where_it_came_from_without_being_asked(
    tmp_path: Path,
) -> None:
    """*"Inline presence"*, which is the half of D9 that is not on demand.

    A visitor can see, without acting, that the answer came from somewhere. The
    label is the retriever's, not the model's.
    """
    drew = _turn(
        tmp_path,
        reply="Moderately. It's braised with chipotle chiles and cumin.",
        citations=[BARBACOA],
        claim_class="food",
    )
    rows = _sources(_answer(drew["drawn"]))

    assert len(rows) == 1
    assert "Menu · Barbacoa" in rows[0]["children"][0]["text"]


@needs_node
def test_an_answer_that_claims_nothing_draws_no_source_line(tmp_path: Path) -> None:
    """Account answers are grounded in Snowflake and have no page to point at.

    ``claim_class: account`` exists precisely so the rule does not fire where a
    source link would be decoration, and an empty ``citations`` array has to
    render as nothing rather than as an empty rule above the answer.
    """
    drew = _turn(
        tmp_path,
        reply="You have 1,340 points.",
        citations=[],
        claim_class="account",
    )
    bubble = _answer(drew["drawn"])

    assert not [node for node in bubble["children"] if node["cls"] == "sources"]


@needs_node
def test_two_passages_from_one_page_are_cited_once(tmp_path: Path) -> None:
    """D9's dedup rule, which is what keeps *"noisier"* from being true.

    An answer drawing on three chunks of the same page cites the page once.
    """
    drew = _turn(
        tmp_path,
        reply="It is braised, and it is one of the spicier fillings.",
        citations=[BARBACOA, BARBACOA_AGAIN],
        claim_class="food",
    )

    assert len(_sources(_answer(drew["drawn"]))) == 1


@needs_node
def test_the_detail_is_on_demand_and_the_presence_is_not(tmp_path: Path) -> None:
    """*"Inline presence, on-demand detail."* The date and the URL start hidden."""
    drew = _turn(
        tmp_path,
        reply="Moderately.",
        citations=[BARBACOA],
        claim_class="food",
    )
    row = _sources(_answer(drew["drawn"]))[0]
    detail = row["children"][1]

    assert detail["hidden"] is True
    # The page a person can read, never the endpoint it was harvested from.
    assert BARBACOA["public_url"] in detail["text"]
    assert BARBACOA["source_url"] not in detail["text"]


@needs_node
def test_a_source_with_no_published_page_is_shown_but_is_not_a_link(
    tmp_path: Path,
) -> None:
    """A citation the visitor cannot usefully be sent anywhere for.

    ``chip-at7``: the FAQ is harvested from a GraphQL endpoint that lives on the
    ordinary public host, answers 200, and serves ``application/json``. Linking
    it sent visitors to a JSON body, which is the bug. Dropping the citation
    would be worse -- the claim would lose its evidence -- so the row is drawn,
    dated, and simply does not click. That is the whole of the decision, and it
    is asserted here rather than left to the renderer's good intentions.
    """
    drew = _turn(
        tmp_path,
        reply="Delivery is handled by our partners.",
        citations=[UNPUBLISHED],
        claim_class="policy",
    )
    rows = _sources(_answer(drew["drawn"]))
    assert len(rows) == 1
    detail = rows[0]["children"][1]

    assert "no public page" in detail["text"].lower()
    assert UNPUBLISHED["source_url"] not in detail["text"]
    assert not _links(detail)


def _links(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every anchor drawn inside ``node``, at any depth."""
    found = []
    for child in node.get("children", []):
        if child.get("tag") == "a":
            found.append(child)
        found.extend(_links(child))
    return found


# ---------------------------------------------------------------------------
# The allergen rule, which is stricter for a reason
# ---------------------------------------------------------------------------


@needs_node
def test_an_allergen_answer_shows_its_harvest_date_without_interaction(
    tmp_path: Path,
) -> None:
    """*"As published on 24 August"* is a materially different claim.

    Published allergen data goes stale, the corpus is re-harvested weekly, and
    this is the one place D9 says a date earns permanent screen space.
    """
    drew = _turn(
        tmp_path,
        reply="The published chart does not mark the steak with dairy.",
        citations=[STEAK],
        claim_class="allergen",
    )
    line = _sources(_answer(drew["drawn"]))[0]["children"][0]

    assert "2026-08-24" in line["text"]


@needs_node
def test_allergen_sources_are_never_collapsed(tmp_path: Path) -> None:
    """The dedup rule above does not apply here, and D9 is explicit about it.

    In an answer covering three items it has to stay unambiguous which source
    backs which claim, and collapsing two rows of the same page destroys exactly
    that.
    """
    drew = _turn(
        tmp_path,
        reply="Two of the three are marked; the third is not.",
        citations=[BARBACOA, BARBACOA_AGAIN],
        claim_class="allergen",
    )

    assert len(_sources(_answer(drew["drawn"]))) == 2


# ---------------------------------------------------------------------------
# And the rule that holds without a runtime
# ---------------------------------------------------------------------------


def test_the_widget_never_reads_a_citation_out_of_the_answer_text() -> None:
    """D9: *"The widget never parses citations out of ``text``."*

    A source the model wrote as prose is a source the model could have invented,
    so the only route to a rendered citation is the ``citations`` field. The
    page has one call site for that and no parser at all.
    """
    page = chat_page()

    assert "frame.citations" in page
    assert "renderSources" in page
    # The two shapes a text-scraping renderer would have to take, neither of
    # which is here.
    assert "streamed.match" not in page
    assert "indexOf('\\u2014 ')" not in page
