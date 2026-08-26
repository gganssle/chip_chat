"""The PRD's requirements, as identifiers a case can be tied to.

Issue #29's first acceptance criterion is *every PRD requirement has at least one
golden-set entry referencing it*, and that is only checkable if the requirements
are data rather than a section of a document. So they are transcribed here, one
:class:`Requirement` each, with the lane they belong to.

**Two of them postdate the ticket.** #29 lists ``K1-K4`` and ``V1-V6``; the PRD
now also carries ``K5`` (where citations render) and ``V7`` (several meals in one
photograph), both added by decisions Q3 and Q4 after the ticket was written. They
are in the register, because a coverage check written against the ticket's list
would go on reporting complete while the product grew requirements nobody was
scoring.

**Entry is deliberately absent.** ``E1``-``E7`` are properties of the screen a
visitor lands on -- one screen to a conversation, an opening message that names
the persona, chips, a demo notice, three editable fields. None of them is a
question with an expected answer, so none can be a golden-set entry, and
inventing one would mean scoring a conversational turn for something no
conversational turn does. :data:`OUT_OF_SCOPE` records that decision with its
reason rather than leaving the gap for a reader to find.

**Some requirements are covered somewhere else, and say where.** The vision
lane's accuracy is the labeled photo set's job (#56), the two launch gates belong
to the adversarial suite (#30), and rate limiting and the spend ceiling are
already tested where they are enforced. A requirement covered elsewhere is not
uncovered, but it is also not covered *here* -- :data:`DELEGATIONS` names the
target and the reason, and :mod:`chip_chat.eval.golden.coverage` prints both
kinds separately so nobody reads a delegation as a case.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.golden.lanes import Lane

__all__ = [
    "DELEGATIONS",
    "OUT_OF_SCOPE",
    "REQUIREMENTS",
    "Delegation",
    "Requirement",
    "requirement",
]


@dataclass(frozen=True, slots=True)
class Requirement:
    """One numbered requirement of PRD section 07.

    Attributes:
        id: The identifier the PRD gives it, e.g. ``K3``. Cases reference this
            string, so it is the join key between a product document and a
            score.
        lane: Which of the five lanes it belongs to. ``Lane.NONE`` for the
            trust-and-safety requirements, which are properties of every lane
            rather than of one.
        text: The requirement, abbreviated to one line. Not the authority --
            ``docs/cilantro-prd.md`` is -- but enough that a coverage report
            reads as sentences rather than as a list of identifiers.
    """

    id: str
    lane: Lane
    text: str


@dataclass(frozen=True, slots=True)
class Delegation:
    """A requirement measured somewhere other than the golden set.

    Attributes:
        requirement_id: What is being covered elsewhere.
        target: Where, named so a reader can go and look: a module, a test
            file, or an issue that owns the measurement.
        reason: Why it does not belong here. The field exists because a
            delegation with no argument behind it is indistinguishable from a
            gap somebody labeled to make a report go green.
    """

    requirement_id: str
    target: str
    reason: str


REQUIREMENTS: Final[tuple[Requirement, ...]] = (
    # --- Menu knowledge -----------------------------------------------------
    Requirement(
        "K1",
        Lane.KNOWLEDGE,
        "Answers menu, ingredient, nutrition, allergen, rewards and policy "
        "questions from published data.",
    ),
    Requirement(
        "K2",
        Lane.KNOWLEDGE,
        "Every food or policy claim carries a citation to its source page.",
    ),
    Requirement(
        "K3",
        Lane.KNOWLEDGE,
        "Says plainly when the published data does not contain an answer, and "
        "does so unconditionally for allergen and dietary questions.",
    ),
    Requirement(
        "K4",
        Lane.KNOWLEDGE,
        "Handles comparative and constrained questions from the published data.",
    ),
    Requirement(
        "K5",
        Lane.KNOWLEDGE,
        "Citations render inline; allergen answers cite adjacent to the claim "
        "with the harvest date visible.",
    ),
    # --- Account ------------------------------------------------------------
    Requirement(
        "A1",
        Lane.ACCOUNT,
        "Answers questions about the visitor's own history, spend, points and "
        "store visits.",
    ),
    Requirement(
        "A2",
        Lane.ACCOUNT,
        "Handles aggregates and time ranges.",
    ),
    Requirement(
        "A3",
        Lane.ACCOUNT,
        "Never returns another visitor's data, under any phrasing.",
    ),
    Requirement(
        "A4",
        Lane.ACCOUNT,
        "Says so when an account question cannot be answered reliably, rather "
        "than producing a plausible number.",
    ),
    # --- Personalization ----------------------------------------------------
    Requirement(
        "P1",
        Lane.PERSONALIZATION,
        "Can state the visitor's usual order and briefly how it worked that out.",
    ),
    Requirement(
        "P2",
        Lane.PERSONALIZATION,
        "Recommends untried items grounded in actual behaviour rather than "
        "generic popularity.",
    ),
    Requirement(
        "P3",
        Lane.PERSONALIZATION,
        "Surfaces unredeemed value without being asked, where the persona has "
        "meaningful stored value.",
    ),
    # --- Action -------------------------------------------------------------
    Requirement(
        "T1",
        Lane.ACTION,
        "Supports place, reorder, modify, cancel, redeem, and update preferences.",
    ),
    Requirement(
        "T2",
        Lane.ACTION,
        "Every action renders a structured confirmation card before it happens.",
    ),
    Requirement(
        "T3",
        Lane.ACTION,
        "The card is editable in place, without restarting the conversation.",
    ),
    Requirement(
        "T4",
        Lane.ACTION,
        "Every completed action returns a receipt the visitor can refer back to.",
    ),
    Requirement(
        "T5",
        Lane.ACTION,
        "Actions are simulated, and the confirmation card says so.",
    ),
    # --- Vision -------------------------------------------------------------
    Requirement(
        "V1",
        Lane.VISION,
        "Accepts a photo upload inline, on desktop and mobile.",
    ),
    Requirement(
        "V2",
        Lane.VISION,
        "Returns a proposed order matching the photo, composed only of real menu items.",
    ),
    Requirement(
        "V3",
        Lane.VISION,
        "States what it believes it saw, so the visitor can correct it.",
    ),
    Requirement(
        "V4",
        Lane.VISION,
        "Says so when the photo is not Chipotle-style food, and offers the "
        "closest thing that is.",
    ),
    Requirement(
        "V5",
        Lane.VISION,
        "Asks a clarifying question when confidence on a component is low.",
    ),
    Requirement(
        "V6",
        Lane.VISION,
        "Never names a menu item that does not exist.",
    ),
    Requirement(
        "V7",
        Lane.VISION,
        "Says how many meals it saw and asks which one, rather than choosing.",
    ),
    # --- Trust and safety ---------------------------------------------------
    Requirement(
        "S1",
        Lane.NONE,
        "Content moderation runs on inbound text and on every image before "
        "anything else processes it.",
    ),
    Requirement(
        "S2",
        Lane.NONE,
        "Instructions embedded in retrieved documents are treated as data, "
        "never as direction.",
    ),
    Requirement(
        "S3",
        Lane.NONE,
        "Rate limited per session and per source address.",
    ),
    Requirement(
        "S4",
        Lane.NONE,
        "Degrades to a friendly limit message when the daily budget is exhausted.",
    ),
)
"""Every requirement a golden-set entry may reference. Order is PRD order."""


_BY_ID: Final[Mapping[str, Requirement]] = {item.id: item for item in REQUIREMENTS}


DELEGATIONS: Final[tuple[Delegation, ...]] = (
    Delegation(
        "A3",
        "the adversarial suite, #30",
        "A launch gate, and gates are measured by attack rather than by a "
        "question with a right answer. The golden set holds one plain-phrasing "
        "case so the ordinary path is covered; every other phrasing, and the "
        "concurrency case RFC-001 section 05 asks for, belong to the suite.",
    ),
    Delegation(
        "V1",
        "api/tests/test_upload_limits.py and vision/tests/test_intake.py",
        "An upload route and a browser control. There is no visitor message "
        "whose answer is 'the file arrived', so this is measured where the "
        "bytes are.",
    ),
    Delegation(
        "V2",
        "the labeled photo set, #56",
        "Component-level precision and recall over real photographs. The "
        "golden set covers whether a photo turn reaches the vision lane at "
        "all, which the photo set cannot see -- it runs the lane directly.",
    ),
    Delegation(
        "V3",
        "the labeled photo set, #56",
        "What the model believed it saw is stage 4's slot output, scored "
        "there against a person's reading of the same frame.",
    ),
    Delegation(
        "V4",
        "the labeled photo set, #56",
        "Measured over the not_chipotle frames as a detection score. It needs "
        "photographs, not sentences.",
    ),
    Delegation(
        "V5",
        "the labeled photo set, #56",
        "The clarify outcome, measured over frames with a required slot the "
        "photograph does not answer.",
    ),
    Delegation(
        "V6",
        "chip_chat.vision.matcher, and RFC-001 D3",
        "Structural rather than statistical: stage 4's enums are generated "
        "from the catalogue and stage 5 resolves rows, so there is no path by "
        "which a name that is not on the menu reaches a draft. A pass rate "
        "here would measure how hard the set tried.",
    ),
    Delegation(
        "V7",
        "the labeled photo set, #56, and docs/decisions/multi-meal-photos.md",
        "Measured over the multi_meal frames, as the declined outcome.",
    ),
    Delegation(
        "S1",
        "api/tests/test_guard.py and vision/tests/test_moderation.py",
        "Moderation runs before anything else processes the input, which "
        "means before the turn a golden case would describe. Whether it ran is "
        "a property of the request path.",
    ),
    Delegation(
        "S2",
        "the adversarial suite, #30",
        "Needs an injection planted in the harvested corpus. The golden set "
        "holds one case for the ordinary shape; the corpus-resident attacks "
        "are the suite's, with the traces that show where each one died.",
    ),
    Delegation(
        "S3",
        "api/tests/test_limits.py and api/tests/test_source_ratelimit.py",
        "Enforced in the request path, per session and per address. A single "
        "turn cannot observe it.",
    ),
    Delegation(
        "S4",
        "api/tests/test_killswitch.py and api/tests/test_spend_gate.py",
        "The stop state is reached by exhausting a ledger, not by asking a "
        "question. What a golden case could check -- that the message is "
        "friendly and the turn is still HTTP 200 -- is asserted there.",
    ),
)
"""Requirements measured elsewhere, with the target and the argument."""


OUT_OF_SCOPE: Final[Mapping[str, str]] = {
    "E1": "Entry is one screen before a conversation exists.",
    "E2": "Persona assignment happens before the first turn.",
    "E3": "The opening message is composed by the app, not answered by a lane.",
    "E4": "Chips are a UI affordance with no question behind them.",
    "E5": "Switching persona is a control, and starts a new conversation.",
    "E6": "A notice on a page, checked by web/tests.",
    "E7": "Three editable fields on a form. The lane behind one of them, "
    "update_preferences, is covered by T1.",
}
"""PRD section 07's Entry requirements, and why none of them is a golden case.

Kept as data so that :mod:`chip_chat.eval.golden.coverage` can state the
exclusion out loud. A requirement nobody scores and nobody mentions is
indistinguishable from one somebody forgot.
"""


def requirement(requirement_id: str) -> Requirement:
    """Look one up by id.

    Args:
        requirement_id: e.g. ``"K3"``.

    Returns:
        The requirement.

    Raises:
        KeyError: If the PRD has no such requirement. Cases are validated
            against this at load, so a typo in a manifest is caught before a
            run rather than showing up as a requirement nothing covers.
    """
    return _BY_ID[requirement_id]
