"""Every sentence the visitor reads, in one place.

Copy is separated from markup because several of these strings are requirements
rather than wording, and a requirement buried in an f-string in a page builder
is a requirement nobody can test.

:data:`BANNER` is the first of them and the strictest. Issue #70 is a launch
criterion and it quotes the sentence it wants, word for word:

    Unofficial demo, not affiliated with Chipotle Mexican Grill. All orders are
    simulated.

It appears on the entry screen **and** persists in the chat header, and it is
not dismissible. A notice a visitor can close is a notice that is absent for
every visitor who closed it, which for a publicly reachable bot using a real
company's menu is the whole of the problem.

:data:`SIMULATED` is the second. PRD Flow 3 puts the word on the confirmation
card itself -- ``[ Edit ]  [ Place order ]  · simulated`` -- rather than in a
footnote, and issue #68's fourth acceptance criterion is that it is visible on
every card *and every receipt*. So it is one constant, used in both places, and
``web/tests/test_page.py`` counts the places.

The rest is ordinary product copy, kept here for the ordinary reason: the
opening message and the persona sentence are the interface, and an interface
should be reviewable without reading a template.
"""

__all__ = [
    "BANNER",
    "NAME_GATE_HINT",
    "NAME_GATE_PLACEHOLDER",
    "NAME_GATE_SUBMIT",
    "NAME_GATE_TITLE",
    "PHOTO_RETENTION",
    "SIMULATED",
    "STOP_STATE_HEADING",
    "SWITCH_CONFIRM",
    "SWITCH_LABEL",
    "TITLE",
]

TITLE = "Cilantro — an unofficial demo assistant"

BANNER = (
    "Unofficial demo, not affiliated with Chipotle Mexican Grill. All orders are "
    "simulated."
)
"""Issue #70's disclosure, verbatim, on every screen and in the chat header.

Not a toast, not a footer, and not dismissible. The longer explanation of what
the demo reads and what it invents lives in the README and in
``docs/public-demo.md``; this is the sentence that has to be in front of
somebody who arrived from a link and will read one line.
"""

NAME_GATE_TITLE = "What should I call you?"

NAME_GATE_HINT = (
    "Invent a first name. There is no account to sign in to and nothing is kept "
    "beyond the demo — you will be assigned one of twenty-eight synthetic "
    "customers with real order history behind them."
)
"""Why the gate is a greeting and not a login, said before the visitor types.

Asking a stranger for a name is a small thing to explain and a large thing to
get wrong. This says the name is invented, that it buys them a loaded account,
and that the account is not theirs — all three before the cursor blinks.
"""

NAME_GATE_PLACEHOLDER = "Sam"

NAME_GATE_SUBMIT = "Start"

SIMULATED = "simulated"
"""The word on every card and every receipt. PRD Flow 3, issue #68."""

SWITCH_LABEL = "Switch persona"
"""The switcher, on the chat surface. One tap, never a settings screen."""

SWITCH_CONFIRM = "Start again as somebody else?"

PHOTO_RETENTION = "Photos are deleted after 48 hours."
"""What is promised beside an uploaded photograph.

The same promise ``chip_chat.vision.retention`` makes to the storage account,
said to the person it is about rather than only to the lifecycle policy.
"""

STOP_STATE_HEADING = "Cilantro is resting"
"""Heading above :data:`~chip_chat.api.outcome.STOP_STATE_MESSAGE`.

Not "error", not "quota", not "limit". A visitor who hits the daily ceiling has
done nothing wrong and should not be told they have.
"""
