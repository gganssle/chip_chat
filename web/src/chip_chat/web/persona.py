"""Who the visitor has become, written out as a sentence somebody would say.

PRD §06 and issue #67 make the same argument twice, and it is the argument this
module exists to answer:

    The single largest threat to this demo is not accuracy. It is that a visitor
    types their name, arrives at an empty account, asks the only question that
    occurs to them, and is told they have zero points and no order history.

So the app assigns a loaded synthetic customer on entry, and this is where it
says so out loud. Issue #67 is specific about the standard the sentence is held
to -- *not a template with holes, a sentence that reads like someone wrote it* --
and about what has to be in it: the persona's **narrative, home store, points
balance and characteristic order**.

**All four are already in one field, and that is not an accident.**
``docs/decisions/persona-fixtures.md`` shipped ``ACCOUNTS.persona_fixtures``
precisely so the opening message would have a sentence to quote rather than four
numbers to assemble::

    a regular at NC Town 1 Mall, 1,288 points on the card, and 99% of 79 orders
    the same Chicken Bowl with guacamole, white rice, black beans and cheese

That fragment was written by ``data-gen``'s per-archetype template, reviewed as
twenty-eight rows a person can read, and measured from the customer's real
order history. Re-deriving the same sentence here from ``points_balance`` and
``home_store_name`` would be a second, worse copy of it that can disagree with
the first -- and disagreeing with the account tool is exactly the failure the
opening message is supposed to prevent.

**What this module adds is the grammar around it.** The narratives are fragments
in two different shapes: some are noun phrases (*"a regular at ..."*, *"42
orders across 13 stores ..."*) and some are verb phrases (*"puts in the floor's
lunch order at ..."*, *"turns up every couple of months ..."*). One lead-in
cannot carry both -- *"I've set you up as puts in the floor's lunch order"* --
so :data:`_FRAMES` gives each archetype the clause that fits its own sentence,
and :data:`_INVITATIONS` gives each one a closing line that points at what is
worth asking *this* customer. A Lapsed Regular is invited to ask what their
unredeemed points are worth; a Weekend Family is invited to feed four people.

**And the chips.** Issue #67 asks for tappable suggestions spanning at least
three capabilities, on the grounds that they *"do more for a cold visitor than
any amount of prompt engineering"*. :func:`suggestions` returns four, tagged
with the lane each one exercises, and the archetype decides the wording rather
than the set: every persona gets a menu question, an account question, an order
and a photo, because those are the four things the demo can actually do.

Nothing here reaches a database or an environment variable. It takes a
:class:`Persona` -- a plain value the app fills in from the binding it made --
and returns strings. ``web/`` is where the copy lives and ``api/`` is where the
identity is; keeping the two apart is why this module has no imports of its own.
"""

from dataclasses import dataclass

__all__ = [
    "DEFAULT_INVITATION",
    "Chip",
    "Persona",
    "opening_message",
    "restart_message",
    "suggestions",
    "unbound_opening_message",
]


@dataclass(frozen=True, slots=True)
class Persona:
    """The assigned customer, as the copy needs them.

    A deliberately narrow projection of
    :class:`chip_chat.api.visitors.VisitorSession`. Note the field that is not
    here: there is no ``demo_id``. The identity is the server's, it never
    reaches the browser, and copy that cannot name it cannot leak it.

    Attributes:
        persona_id: Which archetype this customer is an exemplar of. Chooses
            the grammar and the invitation, and nothing else.
        label: What the demo calls that archetype out loud -- *"The Weekly
            Regular"*. Shown in the header, so a visitor can see at a glance
            who they are and that switching would change it.
        display_name: The invented first name the visitor typed, if they typed
            one. Optional, because the name gate is a greeting and not a login.
        narrative: The measured sentence from ``persona_fixtures``. The spine of
            the opening message.
        home_store_name: The restaurant they actually order from.
        points_balance: Loyalty points.
        order_count: How many orders are behind them.
        usual_order: Their characteristic order in words, where the deployment
            can name it. Usually ``None``: the catalogue lookup that turns
            ``usual_item_id`` into words is not wired in the app tier, and the
            narrative names the order anyway.
    """

    persona_id: str
    label: str
    display_name: str | None = None
    narrative: str | None = None
    home_store_name: str | None = None
    points_balance: int | None = None
    order_count: int = 0
    usual_order: str | None = None


@dataclass(frozen=True, slots=True)
class Chip:
    """One tappable opening prompt, and the lane it exercises.

    The lane is not decoration. Issue #67's criterion is that the chips *"cover
    at least three lanes and are genuinely answerable by the assigned persona"*,
    and a criterion about coverage needs the coverage to be a value something
    can count -- ``web/tests/test_page.py`` counts it.

    Attributes:
        prompt: What is sent when the chip is tapped, verbatim.
        lane: Which capability it exercises: ``menu``, ``account``, ``order`` or
            ``photo``.
    """

    prompt: str
    lane: str


_FRAMES: dict[str, str] = {
    "regular": "You're {narrative}",
    "lapsed": "You were {narrative}",
    "explorer": "You've got {narrative}",
    "newcomer": "You're {narrative}",
    "office_manager": "You're the one who {narrative}",
    "weekend_family": "You're the one who {narrative}",
    "occasional": "You're the one who {narrative}",
}
"""The clause each archetype's narrative fragment wants in front of it.

Keyed by ``persona_id`` because that is what ``population.toml`` keys the
narrative template by, so the two halves of one sentence are written against the
same key. An archetype with no entry gets :data:`_DEFAULT_FRAME`, which is the
noun-phrase reading -- the shape five of the seven use.
"""

_DEFAULT_FRAME = "You're {narrative}"

_INVITATIONS: dict[str, str] = {
    "regular": (
        "Ask me for your usual, ask what's actually in it, or send me a photo of "
        "something you'd rather have."
    ),
    "lapsed": (
        "Those points are still sitting there. Ask me what they're worth, ask "
        "what's changed since you last came in, or send me a photo of something "
        "that looks good."
    ),
    "explorer": (
        "Ask me what you've never tried, ask about anything on the menu, or send "
        "me a photo of something you saw and want."
    ),
    "newcomer": (
        "Ask me what you've been ordering, ask anything at all about the menu, or "
        "send me a photo of something you want."
    ),
    "office_manager": (
        "Ask me what the floor usually gets, ask about allergens before you order "
        "for eleven people, or send me a photo of a spread you want repeated."
    ),
    "weekend_family": (
        "Ask me what the table usually has, ask what's mild enough for everybody, "
        "or send me a photo of what you want."
    ),
    "occasional": (
        "Ask me what you had last time, ask anything about the menu, or send me a "
        "photo of something you want."
    ),
}
"""What is worth asking *this* customer, in the customer's own terms.

A generic invitation is a worse invitation. "Ask me anything" is what a visitor
already assumed and is exactly the blank prompt PRD §06 says loses them; "those
points are still sitting there" is a reason to type.
"""

DEFAULT_INVITATION = (
    "Ask me anything about the menu, about your account, or send me a photo of "
    "something you want."
)
"""The invitation for an archetype nothing here has an opinion about.

Issue #67's own example sentence, kept verbatim so a new archetype arriving from
``population.toml`` gets a serviceable line rather than an empty one.
"""

_CHIPS: dict[str, tuple[Chip, ...]] = {
    "regular": (
        Chip("What's my usual?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("Is the barbacoa spicy?", "menu"),
        Chip("Order my usual for pickup", "order"),
    ),
    "lapsed": (
        Chip("What are my points worth?", "account"),
        Chip("What did I used to order?", "account"),
        Chip("What's new on the menu?", "menu"),
        Chip("Order what I used to get", "order"),
    ),
    "explorer": (
        Chip("What haven't I tried yet?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("What's in a burrito bowl?", "menu"),
        Chip("Order me something different", "order"),
    ),
    "newcomer": (
        Chip("What have I ordered so far?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("What's the difference between a bowl and a burrito?", "menu"),
        Chip("Order me a chicken bowl", "order"),
    ),
    "office_manager": (
        Chip("What does the floor usually order?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("Which items have no dairy?", "menu"),
        Chip("Order four chicken bowls for delivery", "order"),
    ),
    "weekend_family": (
        Chip("What does the table usually get?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("Which salsa is the mild one?", "menu"),
        Chip("Order three bowls and chips", "order"),
    ),
    "occasional": (
        Chip("What did I have last time?", "account"),
        Chip("How many points do I have?", "account"),
        Chip("Is the barbacoa spicy?", "menu"),
        Chip("Order me a chicken bowl with guac", "order"),
    ),
}
"""Four chips per archetype, and the same four *kinds* of chip every time.

The wording changes and the coverage does not. A visitor who switches from the
Regular to the Lapsed Regular should see the demo ask a different question, not
offer a different feature -- that is what makes the switch legible as a change
of customer rather than a change of app.
"""

_PHOTO_CHIP = Chip("Send a photo of a meal", "photo")
"""The fourth lane, appended to every set.

Not written per archetype because there is nothing archetype-specific about
holding up a photograph. It is a lane the chips have to cover and a capability a
cold visitor will never guess at.
"""


def opening_message(persona: Persona) -> str:
    """Return the sentence that tells a visitor who they have become.

    Args:
        persona: The assigned customer.

    Returns:
        Two sentences and a greeting: who they are, and what is worth asking.
        Built from the archetype's own narrative where there is one, and from
        the row's measurements where there is not -- which is the shape a
        deployment whose fixtures carry no narrative would get, and is still a
        sentence rather than a shrug.
    """
    greeting = f"Hi {persona.display_name}." if persona.display_name else "Hi there."
    return f"{greeting} {_who(persona)} {_invitation(persona)}"


def restart_message(persona: Persona) -> str:
    """Return the line that says the conversation has started over as somebody else.

    Issue #69's second acceptance criterion is that the conversation *visibly*
    restarts *and the message says so*. Silently continuing a thread whose
    history now belongs to a different synthetic customer would be the same
    cross-visitor confusion the identity binding exists to prevent, showing up
    in the transcript instead of in the database.

    Args:
        persona: The customer the visitor has just become.

    Returns:
        The restart notice, then the ordinary opening message for the new
        persona -- because a switch gets the same treatment as an entry.
    """
    return (
        f"Starting over. Everything above belonged to somebody else, so I have "
        f"put it down. {_who(persona)} {_invitation(persona)}"
    )


def suggestions(persona: Persona) -> tuple[Chip, ...]:
    """Return the tappable opening prompts for this persona.

    Args:
        persona: The assigned customer.

    Returns:
        Chips spanning the menu, account, order and photo lanes, worded for the
        archetype. An unrecognised archetype gets the Regular's set, which is
        answerable by any populated account.
    """
    return (*_CHIPS.get(persona.persona_id, _CHIPS["regular"]), _PHOTO_CHIP)


def unbound_opening_message() -> str:
    """Return the opening message for a deployment with no synthetic population.

    The honest state, and one the app is told about rather than discovers: when
    :meth:`chip_chat.api.visitors.VisitorDesk.admit` returns ``None`` there is no
    account to describe, and describing one anyway would be the invented account
    issue #66 refuses. So the demo says what it is and offers the menu, which is
    the half of it that works without a customer.
    """
    return (
        "Hi there. This deployment has no synthetic accounts loaded, so there is "
        "no order history or points balance to show you -- I can still answer "
        "questions about the menu and price up an order."
    )


def _who(persona: Persona) -> str:
    """Return the clause naming the customer, ending in a full stop."""
    narrative = (persona.narrative or "").strip()
    if narrative:
        frame = _FRAMES.get(persona.persona_id, _DEFAULT_FRAME)
        return _stop(frame.format(narrative=narrative.rstrip(".")))
    return _stop(_composed(persona))


def _composed(persona: Persona) -> str:
    """Describe the customer from the row when no narrative was written.

    Deliberately not the primary path. It exists so a fixture whose narrative
    column is empty produces a sentence with the store, the balance and the
    order count in it rather than a greeting that says nothing -- the four facts
    issue #67 asks for, assembled rather than quoted.
    """
    parts: list[str] = []
    if persona.home_store_name:
        parts.append(f"a regular at the {persona.home_store_name} store")
    if persona.order_count:
        parts.append(f"{persona.order_count} orders behind you")
    if persona.points_balance is not None:
        parts.append(f"{persona.points_balance:,} points on the card")
    if persona.usual_order:
        parts.append(f"and a standing order of {persona.usual_order}")
    if not parts:
        return f"You're set up as {persona.label or 'a returning customer'}"
    return "You're " + ", ".join(parts)


def _invitation(persona: Persona) -> str:
    return _INVITATIONS.get(persona.persona_id, DEFAULT_INVITATION)


def _stop(sentence: str) -> str:
    """End a clause with exactly one full stop."""
    trimmed = sentence.rstrip()
    return trimmed if trimmed.endswith((".", "!", "?")) else f"{trimmed}."
