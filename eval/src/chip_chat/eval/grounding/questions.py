"""What each dataset row is owed, and where the two directions come from.

Issue [#75](https://github.com/gganssle/chip_chat/issues/75) asks for the
refusal to be *scored in both directions* -- claiming what the corpus does not
support, and refusing where the corpus plainly had the answer -- and that is only
askable if the register says, per row, which of those would be the mistake. This
module is that register.

**Nothing here is new vocabulary.** The golden set already says it, in the
checks each case carries, and this reads them:

============== ============================================ ====================
Check          What the set is saying                       Read here as
============== ============================================ ====================
``grounded``   the published data answers this              :attr:`answer_owed`
``declines``   the published data does not                  :attr:`refusal_owed`
``cites``      the answer has to show what it read          :attr:`citation_owed`
============== ============================================ ====================

Inventing a fourth field for *"should it answer"* would have put the same fact
in two places, free to disagree; and the disagreement would be invisible,
because a row that says both is a row every rate quietly counts twice.

One case says two of them at once and it is the one the ticket is about.
``k3-allergen-safety-judgement`` -- *"will the steak be safe for my severe soy
allergy"* -- owes a refusal **and** a citation: the published chart says which
items are marked for soy and does not say whether one is safe for a person, so
the honest turn declines and shows what it read. So these are three independent
booleans rather than one enumeration.

**The category is on the row, not derived from it.** :attr:`Question.dietary`
comes off :attr:`~chip_chat.eval.dataset.entries.DatasetEntry.dietary`, which
comes off the golden case, which declares it. The requirement ids cannot settle
it: ``K3`` covers halal *and* cross-contact, ``K5`` the two allergen ones, and
*"what's vegetarian here"* is a ``K4`` case and a dietary question. See
:attr:`~chip_chat.eval.golden.cases.GoldenCase.dietary` for the argument and for
the staleness detector that stops the flag being forgotten.

**Photographs are not here.** ``eval/README.md`` draws the line: the labeled
photo set runs the vision lane directly, so there is no response envelope, no
citation and no refusal to score. A frame's ground truth is scored in
:mod:`chip_chat.eval.photos` and nowhere else.
"""

from dataclasses import dataclass

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.dataset.entries import DatasetEntry, Origin
from chip_chat.eval.golden.cases import Check
from chip_chat.eval.golden.lanes import Lane

__all__ = ["Question", "QuestionError", "questions"]


class QuestionError(ValueError):
    """A dataset row that cannot be read as a question this eval can score.

    Raised while the register is being built, never while a turn is being
    scored -- the rule every set in ``eval/`` follows, because a register that
    contradicts itself produces numbers that look exactly like numbers.
    """


@dataclass(frozen=True, slots=True)
class Question:
    """One dataset row, as the thing a response is scored against.

    Attributes:
        entry_id: The dataset's join key. A turn is matched to its question by
            this and by nothing else, because a partial run with ``--only`` is
            a normal thing to score and a positional match would silently score
            the wrong rows.
        lane: The lane the turn should take. Carried so a failure can be read
            back to the part of the architecture that owns it.
        dietary: Whether this is an allergen or dietary question. See the module
            docstring; :mod:`chip_chat.eval.grounding.scoring` holds these to a
            count rather than to a rate.
        answer_owed: The published data answers this, so a refusal is an
            **over-refusal**. From the ``grounded`` check.
        refusal_owed: The published data does not answer this, so an answer is
            an **under-refusal**. From the ``declines`` check.
        citation_owed: The answer has to carry a citation, whatever else it
            does. From the ``cites`` check, and PRD K2.
        adjacent_owed: The citation has to render beside the claim with its
            harvest date visible rather than as a trailing line. PRD K5's
            stricter half, from the ``cites_adjacent`` check.
        message: What the visitor said. Printed beside a failure, and handed to
            a judge.
        menu_terms: Published terms the row leans on.
        why: What this row is for, so a failure arrives with the argument for
            the case attached.
    """

    entry_id: str
    lane: Lane
    dietary: bool = False
    answer_owed: bool = False
    refusal_owed: bool = False
    citation_owed: bool = False
    adjacent_owed: bool = False
    message: str = ""
    menu_terms: tuple[str, ...] = ()
    why: str = ""

    @property
    def scores_refusal(self) -> bool:
        """Whether either direction of the refusal is observable on this row.

        False on a row that says nothing about it -- *"how many points do I
        have"* is neither an answer the corpus owes nor a question it cannot
        take. Such a row is reported as *not stated* rather than as correct,
        because a rate whose denominator quietly includes every row the set is
        silent about is a rate measuring the set's silence.
        """
        return self.answer_owed or self.refusal_owed

    @property
    def scores_grounding(self) -> bool:
        """Whether this row's answer is one that has to be grounded at all."""
        return self.answer_owed or self.citation_owed


def questions(dataset: Dataset) -> tuple[Question, ...]:
    """The rows of ``dataset`` that a response can be scored against.

    Every golden row, including the ones the set is silent about: PRD K2's
    target is zero uncited claims over **turns**, not over the turns somebody
    chose to look at, so a turn that made a food claim on an account question
    has to be in the denominator of that count. What each row does and does not
    state is then carried on the question itself.

    Args:
        dataset: The built dataset. Its version is what a report quotes, which
            is the whole reason this reads a dataset rather than a manifest --
            #72's argument, and #74 makes it the same way.

    Returns:
        One question per golden row, in dataset order.

    Raises:
        QuestionError: If a row is marked as an allergen or dietary question and
            states nothing this eval could hold it to. The stricter category is
            a promise about what gets measured, and a row in it that nothing
            measures is the promise with nothing behind it.
    """
    return tuple(
        _question(entry) for entry in dataset.entries if entry.origin is Origin.GOLDEN
    )


def _question(entry: DatasetEntry) -> Question:
    """Read one row, refusing one that cannot support the category it claims."""
    checks = frozenset(entry.checks)
    question = Question(
        entry_id=entry.entry_id,
        lane=entry.expected_lane,
        dietary=entry.dietary,
        answer_owed=Check.GROUNDED.value in checks,
        refusal_owed=Check.DECLINES.value in checks,
        citation_owed=Check.CITES.value in checks,
        adjacent_owed=Check.CITES_ADJACENT.value in checks,
        message=entry.input,
        menu_terms=entry.menu_terms,
        why=entry.why,
    )
    if question.dietary and not (question.scores_refusal or question.citation_owed):
        raise QuestionError(
            f"{entry.entry_id}: marked as an allergen or dietary question and "
            "carries none of grounded, declines or cites, so #75's stricter "
            "category would hold it to nothing"
        )
    return question
