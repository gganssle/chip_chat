"""Issue #50's scope, as clauses the set either meets or does not.

The scorer says how well the retriever did on the questions it was given. This
says whether those were the questions the ticket asked for, and the two are
independent in a way that matters: a set of twelve FAQ-heading-verbatim
questions would score beautifully under every arm and would prove nothing about
retrieval, and that failure is invisible to any recall figure.

The requirements below are #50's own prose turned into counts, plus two that are
not #50's and carry their source. Three deserve their argument stated here
rather than in a docstring nobody opens:

**The allergen category has a floor of its own.** The demo criterion is stated
on that category alone -- *top-3 recall on your allergen questions, measured,
with numbers* -- so a set that met the overall count with two allergen questions
would produce a headline computed over two questions. A rate over two questions
moves in halves and is not a measurement.

**The negative set has a floor too, and one of them must be an allergen
question.** A negative set made entirely of *"how much do you pay your crew"* --
questions with no lexical overlap with the corpus at all -- measures nothing:
any retriever refuses those. The one that is worth having is the question every
word of which matches the corpus and none of which the corpus answers, which is
what a safety-shaped negative is.

**Questions whose answer is in more than one place are required.** Without them
``recall@3`` and ``hit@3`` are the same column twice, and the reason for
counting recall over labels at all disappears.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.retrieval.questions import Category, Question, RetrievalSet

__all__ = [
    "MINIMUM_QUESTIONS",
    "REQUIREMENTS",
    "Coverage",
    "Requirement",
    "coverage",
]

MINIMUM_QUESTIONS: Final = 30
"""How many questions the set must hold before a rate over it means much.

Not a number #50 states -- it asks for a labeled set and per-category metrics
without sizing either. It follows from the categories: five of them, plus a
negative set, at a size where a single question moves a category's rate by no
more than a fifth. That is the same floor issue #56 put on the photo set, and it
is here for the same reason: below it, one question changes the headline.
"""


@dataclass(frozen=True, slots=True)
class Requirement:
    """One clause of the scope, and how to count the questions that satisfy it.

    Attributes:
        name: How the report names it.
        minimum: How many questions must satisfy it.
        source: Which document asks for it, so a reader deciding whether a
            requirement still applies can go and read the argument rather than
            guess at it.
        satisfied_by: The test one question either passes or does not.
    """

    name: str
    minimum: int
    source: str
    satisfied_by: Callable[[Question], bool]

    def met_by(self, questions: Sequence[Question]) -> tuple[str, ...]:
        """The ids of the questions satisfying this requirement, in set order."""
        return tuple(q.question_id for q in questions if self.satisfied_by(q))


def _answerable_in(category: Category) -> Callable[[Question], bool]:
    """A test for an answerable question of one category."""
    return lambda q: q.category is category and q.answerable


REQUIREMENTS: Final[tuple[Requirement, ...]] = (
    Requirement(
        name="answerable ingredient questions",
        minimum=4,
        source="#50 per-category metrics",
        satisfied_by=_answerable_in(Category.INGREDIENTS),
    ),
    Requirement(
        name="answerable nutrition questions",
        minimum=4,
        source="#50 per-category metrics",
        satisfied_by=_answerable_in(Category.NUTRITION),
    ),
    Requirement(
        name="answerable allergen questions",
        minimum=6,
        source="#50 demo criterion",
        # The headline is computed over these and nothing else. See the module
        # docstring: a rate over two questions moves in halves.
        satisfied_by=_answerable_in(Category.ALLERGENS),
    ),
    Requirement(
        name="answerable rewards-policy questions",
        minimum=4,
        source="#50 per-category metrics",
        satisfied_by=_answerable_in(Category.REWARDS_POLICY),
    ),
    Requirement(
        name="answerable ordering-policy questions",
        minimum=4,
        source="#50 per-category metrics",
        satisfied_by=_answerable_in(Category.ORDERING_POLICY),
    ),
    Requirement(
        name="questions the corpus cannot answer",
        minimum=5,
        source="#50 negative set",
        satisfied_by=lambda q: not q.answerable,
    ),
    Requirement(
        name="a negative question whose words all match the corpus",
        minimum=1,
        source="#50 negative set, PRD K3",
        # The safety-shaped negative. See the module docstring for why a
        # negative set of far-outside questions measures nothing.
        satisfied_by=lambda q: not q.answerable and q.category is Category.ALLERGENS,
    ),
    Requirement(
        name="questions answered in more than one published place",
        minimum=3,
        source="#50 top-3 recall",
        satisfied_by=lambda q: len(q.relevant) > 1,
    ),
    Requirement(
        name="questions whose answer is under a menu item",
        minimum=8,
        source="RFC-001 §08, #50 ablation",
        # The proper-noun claim the keyword arm exists for. Without enough of
        # these the ablation's three retrieval arms have nothing to disagree
        # about and the table is three copies of one column.
        satisfied_by=lambda q: q.under_menu_item,
    ),
    Requirement(
        name="questions whose answer is in a published document",
        minimum=8,
        source="RFC-001 §08, #50 ablation",
        # The other side of the same argument: paraphrase, where the vector
        # half is supposed to earn its place.
        satisfied_by=lambda q: any(
            label.kind in {"POLICY_SECTION", "FAQ_ENTRY", "DOCUMENT_BLOCK"}
            for label in q.relevant
        ),
    ),
    Requirement(
        name="questions carrying a constraint the index must filter on",
        minimum=2,
        source="#49 constrained cases",
        satisfied_by=lambda q: q.constraint is not None,
    ),
    Requirement(
        name="questions carried over from the golden set",
        minimum=8,
        source="#50 scope: built from the knowledge portion of #29",
        # The scope says the set is *built from* the golden set's knowledge
        # cases rather than invented beside it. Carrying the case id is what
        # makes that checkable, and this is the check.
        satisfied_by=lambda q: bool(q.golden_case),
    ),
)
"""Every clause of the scope. Order is the order the report prints them in."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """Whether a set is the set the ticket asked for.

    Attributes:
        questions: How many the set holds.
        met: Requirements it satisfies, with the ids that satisfy them.
        unmet: Requirements it does not, likewise -- the ids are carried for
            both, because "two of six" is more useful with the two named.
    """

    questions: int
    met: tuple[tuple[Requirement, tuple[str, ...]], ...]
    unmet: tuple[tuple[Requirement, tuple[str, ...]], ...]

    @property
    def enough_questions(self) -> bool:
        """Whether the set reaches :data:`MINIMUM_QUESTIONS`."""
        return self.questions >= MINIMUM_QUESTIONS

    @property
    def complete(self) -> bool:
        """Whether the set meets the count and every requirement."""
        return self.enough_questions and not self.unmet


def coverage(questions: RetrievalSet) -> Coverage:
    """Check a set against :data:`REQUIREMENTS`.

    Args:
        questions: The set, already loaded.

    Returns:
        The :class:`Coverage`. Never raises: an incomplete set is a fact to
        report next to the numbers rather than a reason to refuse to compute
        them, so long as nobody can read the numbers without the label.
    """
    met: list[tuple[Requirement, tuple[str, ...]]] = []
    unmet: list[tuple[Requirement, tuple[str, ...]]] = []
    for requirement in REQUIREMENTS:
        ids = requirement.met_by(questions.questions)
        (met if len(ids) >= requirement.minimum else unmet).append((requirement, ids))
    return Coverage(questions=len(questions), met=tuple(met), unmet=tuple(unmet))
