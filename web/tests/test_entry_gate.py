"""The name gate, and the reason GitHub #105 saw the greeting three times.

**What actually happened.** ``POST /api/entry`` is idempotent on purpose --
issue #9 decided a returning browser resumes its account rather than collecting
a second one -- so every submission of the gate answers with the same persona
and the same opening sentence. ``enter`` had no re-entrancy guard, the gate
stayed live until the response came back, and ``showPersona`` appended the
opening rather than replacing it. On a wired deployment the assignment is a
Snowflake checkout and a roster read, so that window is seconds wide; a held
Enter key or a double-tapped button fills it, and each answer draws another copy
of the identical sentence. The screenshot on #105 is three of them, in the
assistant's own bubble style, above the visitor's first message.

**So the test drives the handler rather than reading the file.** The page's
script runs in Node against :mod:`browser`'s small DOM, the gate's submit
handler is called three times in the window where the entry request is still in
flight, and what is asserted is that one greeting is on screen and one request
went out. It fails on the code #105 was filed against -- three calls, three
bubbles -- and passes on the code below it, which is the only property that
makes a regression test worth writing.

The structural assertions at the bottom run without a JavaScript runtime. They
cannot prove the behaviour; they fail if somebody puts the appending renderer
back.
"""

import json
import re
from pathlib import Path

import browser  # type: ignore[import-not-found]
import pytest

from chip_chat.web import chat_page

OPENING = "Hi Graham. You're new since March 2026 -- 19 orders so far."
"""The sentence #105 saw three copies of, shortened. The content is irrelevant;
what matters is that every answer carries the same one, as the server's does."""

_PRELUDE = """\
const OPENING = %(opening)s;
let entryCalls = 0;
globalThis.fetch = async (url) => {
  if (url !== '/api/entry') throw new Error('unexpected fetch: ' + url);
  entryCalls += 1;
  // The in-flight window. On the deployment this is a Snowflake checkout and a
  // roster read; here it only has to be long enough for a second submission to
  // land inside it, which is what #105's visitor did with the Enter key.
  await new Promise(resolve => setTimeout(resolve, 10));
  return {json: async () => ({
    visitor: {display_name: 'Graham', label: 'The Newcomer', points_balance: 0},
    opening: OPENING,
    chips: [],
    restarted: false,
  })};
};
"""

_MAIN = """\
async function main() {
  const submit = {preventDefault: () => {}};
  registry['name'].value = 'Graham';
  // Three submissions, none of them awaited: a held Enter key, or a button
  // tapped twice more while the first request is still out.
  registry['gate-form'].onsubmit(submit);
  registry['gate-form'].onsubmit(submit);
  registry['gate-form'].onsubmit(submit);
  await new Promise(resolve => setTimeout(resolve, 100));
  const log = registry['log'];
  console.log(JSON.stringify({
    entryCalls,
    reloaded,
    openings: log.children.filter(node => node.textContent === OPENING).length,
    messages: log.children.length,
    gateHidden: registry['gate'].hidden,
  }));
}

main();
"""


@pytest.fixture
def entered(tmp_path: Path) -> dict[str, object]:
    """Submit the name gate three times, and report what the page did."""
    return browser.run(
        _MAIN, prelude=_PRELUDE % {"opening": json.dumps(OPENING)}, tmp_path=tmp_path
    )


needs_node = pytest.mark.skipif(
    not browser.available,
    reason="the executed form of this test needs a JavaScript runtime; the "
    "structural assertions below cover the same regression without one",
)


# ---------------------------------------------------------------------------
# The bug, driven
# ---------------------------------------------------------------------------


@needs_node
def test_three_submissions_of_the_name_gate_leave_one_greeting(
    entered: dict[str, object],
) -> None:
    """GitHub #105, reproduced and then not reproduced.

    One greeting on screen is the whole acceptance criterion. The count of
    messages is asserted beside it so that a renderer which replaced the
    greeting and appended something else next to it does not pass.
    """
    assert entered["openings"] == 1
    assert entered["messages"] == 1


@needs_node
def test_the_gate_stops_issuing_requests_it_already_has_one_of(
    entered: dict[str, object],
) -> None:
    """The cause rather than the symptom.

    Rendering one greeting from three responses would hide #105 while still
    assigning a persona three times and spending three round trips on it. The
    guard is what stops the second request being made at all.
    """
    assert entered["entryCalls"] == 1


@needs_node
def test_the_gate_gives_way_to_the_conversation(entered: dict[str, object]) -> None:
    """And the guard did not simply refuse everybody.

    A re-entrancy guard that latched would be a worse bug than the one it fixed:
    the visitor would type their name and never leave the entry screen.
    """
    assert entered["gateHidden"] is True
    assert entered["reloaded"] is False


# ---------------------------------------------------------------------------
# And the structure that holds it, where there is no runtime to drive it
# ---------------------------------------------------------------------------


def test_the_opening_is_rendered_through_a_path_that_replaces() -> None:
    """The half of the fix that does not depend on the guard holding.

    ``bubble`` appends unconditionally and is right to -- it is what draws the
    transcript. The opening is not transcript: there is one of it per persona,
    so it is drawn by something that can only ever occupy one node.
    """
    page = chat_page()

    assert "bubble(body.opening" not in page
    assert "sayOpening(body.opening" in page
    assert "greeting.replaceWith(node)" in page


def test_the_entry_handler_refuses_to_run_twice() -> None:
    """And refuses before it reaches the network, not after."""
    body = chat_page().split("async function enter(name) {", 1)[1]

    assert body.index("if (entering) return;") < body.index("fetch('/api/entry'")


def test_the_gate_closes_itself_while_the_request_is_out() -> None:
    """The window #105 fell through, shut from the browser's side as well.

    The guard alone would be enough for the transcript and not enough for the
    visitor, who is entitled to see that their name was taken.
    """
    page = chat_page()

    assert 'id="gate-submit"' in page
    assert re.search(r"function gateBusy\(on\) \{[^}]*disabled = on", page)
    assert page.index("gateBusy(true)") < page.index("fetch('/api/entry'")
