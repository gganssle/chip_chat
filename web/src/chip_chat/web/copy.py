"""Every sentence the visitor reads, in one place.

Copy is separated from markup because two of these strings are requirements
rather than wording. :data:`BANNER` is the unaffiliated-demo disclosure the
README insists on, and :data:`OPENING_MESSAGE` carries the one decision the
public demo turns on:

    A visitor with an empty account has nothing to ask. So assign each new name
    a loaded persona on entry *and say so in the opening message*.

Which is why the opening message names the persona's balance and usual order.
It is not scene-setting; it is what tells a cold visitor that "how many points
do I have?" is a question worth asking.

:data:`SUGGESTIONS` is the other half of that. Clickable chips do more for a
visitor who has never seen the app than any amount of prompt engineering, and
these three are the three interactions issue #16 asks to be demonstrated end to
end: a menu question, an account question, an order.
"""

from chip_chat.web.persona import ACCOUNT_SUMMARY

__all__ = [
    "BANNER",
    "OPENING_MESSAGE",
    "STOP_STATE_HEADING",
    "SUGGESTIONS",
    "TITLE",
]

TITLE = "Cilantro — a demo assistant"

BANNER = (
    "A proof of concept on publicly published menu data. Not affiliated with or "
    "endorsed by Chipotle Mexican Grill. Orders are simulated — nothing is "
    "cooked, charged or sent to a restaurant."
)

OPENING_MESSAGE = ACCOUNT_SUMMARY

SUGGESTIONS = (
    "Is the barbacoa spicy?",
    "How many points do I have?",
    "Order me a chicken bowl with a side of guac",
)
"""The three interactions, one click each. Menu, account, order — in that order."""

STOP_STATE_HEADING = "Cilantro is resting"
"""Heading above :data:`~chip_chat.api.outcome.STOP_STATE_MESSAGE`.

Not "error", not "quota", not "limit". A visitor who hits the daily ceiling has
done nothing wrong and should not be told they have.
"""
