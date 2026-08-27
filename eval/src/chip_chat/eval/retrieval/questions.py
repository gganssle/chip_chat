"""The labeled retrieval set: a question, and the places that genuinely answer it.

Issue #50 opens with the sentence this whole package exists to act on --
*retrieval bugs are nearly impossible to diagnose once a model is paraphrasing
over them* -- and the shapes here are what make a label something a rebuild
cannot quietly invalidate.

**A label names a place, not a chunk id.** ``chunk_id`` is a content hash: the
gold layer derives it from the chunk's own text, so a chunking change gives
every chunk in the corpus a new one. A set keyed on ids would therefore go
uniformly, silently wrong on exactly the change #50's fourth acceptance
criterion exists to catch -- *the ablation is repeatable after any chunking
change, so a chunking regression is caught immediately*. A set that dies on the
change it is meant to detect detects nothing.

So a :class:`Label` is a **selector over published chunk metadata**: the kind,
plus whichever of ``item_id``, ``document_id``, ``heading`` or a published
phrase identifies the passage a person would point at. Re-chunk the corpus and
the selector still names the same place, because it names what the restaurant
published rather than how this repository sliced it. Delete that place and the
selector resolves to nothing, which is the regression, reported by name.

**One label is one place, and recall is counted over labels.** A question whose
answer is in three places carries three labels and its ``recall@3`` is *how many
of the three came back*. This is deliberate and it is not the only reading:
counting chunks instead would make a question whose label happens to match nine
near-identical menu rows dominate the mean, and would put a ceiling on any
question with more than three of them. See
:mod:`chip_chat.eval.retrieval.scoring`.

**A question that cannot be answered carries no labels, and that is a label.**
The negative set is #50's fourth scope bullet -- *questions the corpus genuinely
cannot answer, where the correct retrieval behaviour is to return nothing
confident*. It is not a sixth category: a negative allergen question and a
positive allergen question are about the same corpus surface and belong in the
same section of the report, scored differently. So :attr:`Question.answerable`
is a flag rather than a category, and a set that puts a label on an unanswerable
question is refused at load -- it would be asserting the corpus contains
something the question says it does not.

The manifest is JSON, one file, hand-edited::

    {
      "questions": [
        {
          "id": "alg-cheese-dairy",
          "question": "does the cheese have dairy in it",
          "category": "allergens",
          "answerable": true,
          "relevant": [{"kind": "MENU_ITEM", "item_id": "CMG-5252"}],
          "why": "..."
        }
      ]
    }
"""

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from chip_chat.search import chunks
from chip_chat.search.query import ALLERGENS

__all__ = [
    "MENU_FIELDS",
    "Category",
    "Constraint",
    "Label",
    "Question",
    "QuestionError",
    "RetrievalSet",
    "questions_of",
]


class Category(StrEnum):
    """#50's five categories, spelled as the report prints them.

    A closed vocabulary rather than free tags, for the reason
    :mod:`chip_chat.eval.photos.labels` gives about conditions: the coverage
    check turns the ticket's prose into clauses, and a clause cannot be written
    against a word somebody spells two ways.

    The division is by *what the visitor is asking about*, which is also the
    division by *which corpus surface has to be reached* -- ingredients and
    nutrition live on the menu rows, the two policy categories live in the
    published documents and the FAQ, and allergens straddle both. That is why
    the breakdown is worth having at all: an aggregate that was excellent
    because the FAQ is easy to retrieve from would say nothing about the
    category where a miss is a safety problem.

    Attributes:
        INGREDIENTS: What is in a thing, and what the restaurant sells.
        NUTRITION: Published figures. Calories, and comparisons over them.
        ALLERGENS: The published marks. **The category #50 sets the demo bar
            on**, and the one deliberately over-weighted in the set.
        REWARDS_POLICY: Points, redemption, and the rewards terms.
        ORDERING_POLICY: Cancelling, refunds, delivery, catering, gift cards.
    """

    INGREDIENTS = "ingredients"
    NUTRITION = "nutrition"
    ALLERGENS = "allergens"
    REWARDS_POLICY = "rewards_policy"
    ORDERING_POLICY = "ordering_policy"


MENU_FIELDS: Final[frozenset[str]] = frozenset(
    {chunks.ITEM_TYPE, chunks.PRIMARY_FILLING, "item_id"}
)
"""Selector fields that mean *the answer is under a menu item*.

RFC-001 §08's claim for hybrid retrieval is about these three and nothing else:
*item names are proper nouns that embeddings handle poorly*. ``barbacoa`` and
``sofritas`` are values of ``primary_filling``; ``Bowl`` and ``Burrito`` are
values of ``item_type``. So a question whose labels select on one of them is a
question the ablation's keyword arm is *supposed* to win, and
:mod:`chip_chat.eval.retrieval.coverage` requires the set to hold enough of them
for that to be visible rather than anecdotal.
"""

_SELECTABLE: Final[frozenset[str]] = frozenset(chunks.names()) - {
    chunks.CHUNK_ID,
    chunks.TEXT,
    chunks.CITATIONS,
    "character_count",
    "chunked_at",
}
"""Published fields a selector may constrain on.

Everything a chunk carries except the five that cannot identify a *place*:
``chunk_id`` and ``chunked_at`` are properties of the slicing rather than of the
source, ``character_count`` likewise, ``text`` is what ``contains`` is for, and
``citations`` is a struct.
"""


class QuestionError(ValueError):
    """A manifest that cannot be believed as ground truth.

    Raised at load, never at score time -- the same rule
    :class:`chip_chat.eval.photos.labels.LabelError` follows, for the same
    reason. A set that is wrong about itself produces numbers that look exactly
    like numbers.
    """


@dataclass(frozen=True, slots=True)
class Label:
    """One place in the corpus that genuinely contains a question's answer.

    Attributes:
        kind: The chunk kind, one of :data:`chip_chat.search.chunks.KINDS`.
            Always given: it is the coarsest thing about a passage that a
            re-chunk cannot change, and a selector without it would match
            across surfaces.
        fields: Published fields that must equal these values. ``heading`` is
            compared after stripping, because a heading is a line off a page
            and its whitespace is not ground truth.
        contains: A published phrase the chunk's text must contain,
            case-insensitively. For the passages that carry no heading -- plenty
            of published policy sections have none -- where a distinctive
            sentence is the only stable handle there is. A phrase survives
            re-chunking as long as the sentence does, which is the property
            wanted; a *position* would not.
        why: What a person would say if asked why this passage answers the
            question. Required, because a label without one is somebody's guess
            about relevance and cannot be argued with later.
    """

    kind: str
    fields: Mapping[str, str] = field(default_factory=dict)
    contains: str = ""
    why: str = ""

    def matches(self, published: Mapping[str, Any]) -> bool:
        """Whether one chunk, or one retrieved passage, is this place.

        Args:
            published: The chunk's fields. A raw row from the chunk export, or
                a passage's own fields as
                :func:`chip_chat.eval.retrieval.corpus.fields_of` assembles
                them -- the same shape either way, which is what lets the
                corpus and the index be compared at all.

        Returns:
            Whether every constraint holds. A field absent from ``published``
            never matches, even against the empty string: absent and empty are
            different claims and only one of them is about the source.
        """
        if str(published.get(chunks.KIND, "")) != self.kind:
            return False
        for name, wanted in self.fields.items():
            if name not in published:
                return False
            if str(published[name]).strip() != wanted:
                return False
        if self.contains:
            text = str(published.get(chunks.TEXT, "") or "")
            if self.contains.casefold() not in text.casefold():
                return False
        return True

    def describe(self) -> str:
        """A one-line rendering for the report, e.g. ``MENU_ITEM item_id=CMG-101``."""
        parts = [f"{name}={value}" for name, value in sorted(self.fields.items())]
        if self.contains:
            parts.append(f"contains={self.contains!r}")
        return f"{self.kind} " + " ".join(parts) if parts else self.kind


@dataclass(frozen=True, slots=True)
class Constraint:
    """What a question asks the retriever to narrow to, rather than to rank for.

    #49 handles the constrained case with an OData **filter**, and a filter is a
    different mechanism from a ranking -- so scoring *"what can I get without
    any dairy"* as a ranking question would measure the wrong half of the
    design. A question carrying a constraint is scored on whether the constraint
    was read out of the sentence at all and whether every passage that came back
    honours it; :mod:`chip_chat.eval.retrieval.scoring` keeps that in its own
    table.

    Attributes:
        without_allergens: Published allergen codes that must not appear on any
            returned passage. Codes, not labels: ``dair`` is what the chart
            publishes, and ``docs/decisions/allergen-absence.md`` is the rule
            that nothing here matches on a synonym nobody published.
    """

    without_allergens: tuple[str, ...] = ()

    def honoured_by(self, published: Mapping[str, Any]) -> bool:
        """Whether one returned passage satisfies this constraint.

        A passage with no published allergen data does **not** satisfy an
        exclusion, and that is the whole of the allergen decision rather than a
        quirk here: an item Chipotle publishes nothing about is one it declines
        to make a promise about, so returning it inside an answer to *"without
        dairy"* is the failure ``docs/decisions/allergen-absence.md`` exists to
        prevent. The same clause is the first one in
        :func:`chip_chat.search.query.filter_expression`.

        Args:
            published: The passage's fields.

        Returns:
            Whether it may appear in this question's answer.
        """
        if not self.without_allergens:
            return True
        if str(published.get(chunks.ALLERGEN_DISCLOSURE, "")) != "PUBLISHED":
            return False
        marks = published.get(chunks.ALLERGENS, ())
        if isinstance(marks, str):
            marks = (marks,)
        carried = {str(mark).strip().casefold() for mark in marks}
        return not carried & set(self.without_allergens)


@dataclass(frozen=True, slots=True)
class Question:
    """One question, its category, and what a correct retrieval has to reach.

    Attributes:
        question_id: Stable identifier, unique in the set. Appears in the
            report, so it should read like the question.
        text: The visitor's words, sent to the retriever verbatim. Lower case
            and unpunctuated where a visitor would type it that way -- the
            proper nouns are the part that matters and normalising the rest
            would be scoring a query nobody sends.
        category: See :class:`Category`.
        answerable: Whether the published corpus contains the answer at all.
            ``False`` puts the question in the negative set, where the correct
            behaviour is a retrieval that is *not* grounded.
        relevant: The places that answer it. Empty on a negative question, and
            permitted to be empty on a constrained one -- see :class:`Constraint`.
        constraint: What has to be narrowed rather than ranked, or ``None``.
        golden_case: The golden set case this question comes from, where it
            comes from one. #50 asks for the set to be *built from the knowledge
            portion of the golden set*, and carrying the id is what makes that
            checkable rather than claimed.
        why: Why this question is in the set. Required.
    """

    question_id: str
    text: str
    category: Category
    answerable: bool = True
    relevant: tuple[Label, ...] = ()
    constraint: Constraint | None = None
    golden_case: str = ""
    why: str = ""

    @property
    def ranked(self) -> bool:
        """Whether this question is scored on where its answers ranked."""
        return self.answerable and bool(self.relevant)

    @property
    def under_menu_item(self) -> bool:
        """Whether any label selects on a menu item's own fields.

        The proper-noun questions. See :data:`MENU_FIELDS`.
        """
        return any(
            set(label.fields) & MENU_FIELDS
            for label in self.relevant
            if label.kind == chunks.MENU_ITEM
        )


@dataclass(frozen=True, slots=True)
class RetrievalSet:
    """Every labeled question, in manifest order.

    Attributes:
        questions: The questions.
        source: Where they were loaded from, for the report.
    """

    questions: tuple[Question, ...]
    source: str = ""

    def __len__(self) -> int:
        return len(self.questions)

    def __iter__(self) -> Iterator[Question]:
        return iter(self.questions)

    def labels(self) -> tuple[tuple[Question, Label], ...]:
        """Every ``(question, label)`` pair in the set, in order."""
        return tuple(
            (question, label)
            for question in self.questions
            for label in question.relevant
        )

    def in_category(self, category: Category) -> tuple[Question, ...]:
        """The questions of one category, in set order."""
        return tuple(q for q in self.questions if q.category is category)

    @classmethod
    def load(cls, manifest: Path) -> "RetrievalSet":
        """Read a manifest and refuse one that contradicts itself.

        The labels are *not* resolved against a corpus here, because loading a
        set should not require one -- ``--check`` on a laptop that has never
        harvested anything is still worth having.
        :func:`chip_chat.eval.retrieval.corpus.resolve` is that step, and a run
        does it before it queries anything.

        Args:
            manifest: Path to the JSON file.

        Returns:
            The set.

        Raises:
            QuestionError: If the file is not readable as a manifest, or any
                question contradicts itself.
        """
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as error:
            raise QuestionError(f"could not read {manifest}: {error}") from error
        except json.JSONDecodeError as error:
            raise QuestionError(f"{manifest} is not valid JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("questions"), list
        ):
            raise QuestionError(f"{manifest} must be an object with a `questions` array")

        questions = tuple(
            _question(entry, index) for index, entry in enumerate(payload["questions"])
        )
        seen: set[str] = set()
        for question in questions:
            if question.question_id in seen:
                raise QuestionError(f"duplicate question id {question.question_id!r}")
            seen.add(question.question_id)
        return cls(questions=questions, source=str(manifest))


def _question(entry: object, index: int) -> Question:
    """Build one question from its manifest entry, refusing anything incoherent."""
    if not isinstance(entry, dict):
        raise QuestionError(f"questions[{index}] must be an object")
    where = entry.get("id", f"questions[{index}]")

    question_id = _text(entry, "id", where)
    text = _text(entry, "question", where)
    category = _category(entry.get("category"), where)
    answerable = _flag(entry, "answerable", where, default=True)
    relevant = tuple(
        _label(value, where, position)
        for position, value in enumerate(_list(entry, "relevant", where))
    )
    constraint = _constraint(entry.get("constraint"), where)
    why = _text(entry, "why", where)

    question = Question(
        question_id=question_id,
        text=text,
        category=category,
        answerable=answerable,
        relevant=relevant,
        constraint=constraint,
        golden_case=str(entry.get("golden_case", "")),
        why=why,
    )
    _coherent(question, where)
    return question


def _coherent(question: Question, where: str) -> None:
    """Refuse a question that contradicts itself, or the measurement it is in."""
    if not question.answerable and question.relevant:
        # The negative set's whole content is the claim that the corpus does not
        # answer this. A label on one would be the manifest disagreeing with
        # itself about the corpus, and the scorer would believe both halves.
        raise QuestionError(
            f"{where}: an unanswerable question may not name a passage that answers it"
        )
    if question.answerable and not question.relevant and question.constraint is None:
        raise QuestionError(
            f"{where}: an answerable question needs either a relevant passage "
            "or a constraint; otherwise there is nothing it could be scored on"
        )
    seen: set[tuple[str, tuple[tuple[str, str], ...], str]] = set()
    for label in question.relevant:
        key = (label.kind, tuple(sorted(label.fields.items())), label.contains)
        if key in seen:
            # Two identical labels would count the same place twice in the
            # denominator, which quietly halves the question's recall.
            raise QuestionError(f"{where}: names the same place twice")
        seen.add(key)


def _label(value: object, where: str, position: int) -> Label:
    if not isinstance(value, dict):
        raise QuestionError(f"{where}: relevant[{position}] must be an object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in chunks.KINDS:
        raise QuestionError(f"{where}: relevant[{position}] needs a known kind")
    contains = value.get("contains", "")
    if not isinstance(contains, str):
        raise QuestionError(f"{where}: relevant[{position}].contains must be a string")
    why = value.get("why", "")
    if not isinstance(why, str) or not why:
        raise QuestionError(f"{where}: relevant[{position}] needs a `why`")
    fields: dict[str, str] = {}
    for name, wanted in value.items():
        if name in {"kind", "contains", "why"}:
            continue
        if name not in _SELECTABLE:
            raise QuestionError(
                f"{where}: relevant[{position}] selects on {name!r}, which is "
                "not a published chunk field a place can be named by"
            )
        if not isinstance(wanted, str) or not wanted:
            raise QuestionError(f"{where}: relevant[{position}].{name} must be a term")
        fields[name] = wanted.strip()
    if not fields and not contains:
        raise QuestionError(
            f"{where}: relevant[{position}] names a whole chunk kind and nothing "
            "narrower, which is not a place"
        )
    return Label(kind=kind, fields=fields, contains=contains, why=why)


def _constraint(value: object, where: str) -> Constraint | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QuestionError(f"{where}: constraint must be an object")
    codes = value.get("without_allergens", [])
    if not isinstance(codes, list) or not codes:
        raise QuestionError(f"{where}: constraint.without_allergens must be a list")
    for code in codes:
        if code not in ALLERGENS:
            # The published codes and nothing else. A constraint on a word the
            # restaurant never published could not be honoured by any filter,
            # so scoring one would be scoring the eval's own vocabulary.
            raise QuestionError(
                f"{where}: {code!r} is not a published allergen code; "
                f"chip_chat.search.query.ALLERGENS holds the four"
            )
    return Constraint(without_allergens=tuple(codes))


def _category(value: object, where: str) -> Category:
    if isinstance(value, str):
        try:
            return Category(value)
        except ValueError:
            pass
    raise QuestionError(f"{where}: {value!r} is not one of #50's five categories")


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QuestionError(f"{where}: {key} is required")
    return value


def _flag(entry: Mapping[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise QuestionError(f"{where}: {key} must be true or false")
    return value


def _list(entry: Mapping[str, Any], key: str, where: str) -> Iterable[object]:
    value = entry.get(key, [])
    if not isinstance(value, list):
        raise QuestionError(f"{where}: {key} must be a list")
    return value


def questions_of(
    questions: Sequence[Question], *, answerable: bool | None = None
) -> tuple[Question, ...]:
    """The questions matching ``answerable``, or all of them.

    Args:
        questions: Any sequence of questions.
        answerable: ``True`` for the positive set, ``False`` for the negative
            set, ``None`` for both.

    Returns:
        The subset, in the order given.
    """
    if answerable is None:
        return tuple(questions)
    return tuple(q for q in questions if q.answerable is answerable)
