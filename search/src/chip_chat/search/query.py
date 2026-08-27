"""One visitor sentence becomes one hybrid query, and three things it will not guess.

RFC-001 §08 does not leave the shape of this open: *hybrid retrieval with
semantic reranking; keyword recall matters here more than usual, because item
names are proper nouns that embeddings handle poorly.* ``barbacoa``,
``sofritas`` and ``lifestyle bowl`` are tokens a general-purpose embedding
places badly and a lexical index places perfectly, which is why
:data:`chip_chat.search.chunks.SEARCHABLE` exists and why every query this
module builds carries **both** halves — a ``search`` string that BM25 scores
over five fields, and a ``vectorQueries`` entry the service embeds for itself.
Neither half is optional and neither is a fallback for the other.

The rest of this module is the half the issue calls *"query construction that
handles the comparative and constrained cases"*, and almost all of its content
is what it declines to do.

**A filter only fires on a word the restaurant published.** ``dair`` is the
allergen code because Chipotle's chart publishes it as one, and *Dairy* is what
the chart calls it — see ``docs/decisions/allergen-absence.md``, whose rule is
that nothing here matches on the spelling of a code or on a synonym nobody
published. So :func:`read` recognises *dairy*, *gluten*, *soy* and *sulphites*,
and does not recognise *milk*, *lactose* or *wheat*. That is not a gap to fill
later: a visitor who says "no milk" gets the vector half of the query, which is
what paraphrase is for, and does not get a **filter** — because a filter is
exact, and an exact answer to a question the restaurant never published is the
one kind of wrong this lane cannot be.

**An exclusion filter says "not marked", never "free of".** Chipotle's own
caveat is that an unmarked item is one it declines to make a promise about, and
that there are items it publishes nothing about at all. So the filter this
module writes for *"made without dairy"* is::

    allergen_disclosure eq 'PUBLISHED' and not allergens/any(a: a eq 'dair')

and the first clause is the load-bearing one. Without it the answer set would
include every chunk with no published allergen data — napkins, policy sections,
anything new — under a heading the visitor reads as *safe*. With it, the answer
is exactly "items whose published allergen marks do not include dairy", which is
a sentence somebody at Chipotle has actually written down. :attr:`Constraints.notes`
carries the caveat forward so the agent says it out loud.

**What the index cannot express, it says so rather than approximating.**
*Vegetarian* is a published tag — ``vege`` — and it is not a column of the chunk
schema, so no filter here can honour it. *"Fewer calories"* with no figure in it
is a comparison against something this layer cannot see. Both land in
:attr:`Constraints.unapplied`, travel to the agent as a note, and are recorded on
the ``retriever.search`` span. The alternative is a filter that quietly matches
on something adjacent, which is the *plausible near-miss* #49 exists to make
impossible.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from chip_chat.search import chunks
from chip_chat.search.schema import SEMANTIC_CONFIGURATION, VECTOR_FIELD

__all__ = [
    "ALLERGENS",
    "ALLERGEN_SPELLINGS",
    "CAPTIONS",
    "DIETS",
    "PUBLISHED_DISCLOSURE",
    "TOP",
    "VECTOR_CANDIDATES",
    "Bound",
    "Constraints",
    "body",
    "filter_expression",
    "overlap",
    "read",
    "terms",
]

TOP: Final = 5
"""Passages returned to the caller.

Five rather than three: D9's renderer deduplicates citations by source, so an
answer drawn from three sections of the rewards terms cites one page, and a
top-3 that spent all three slots inside one document would have cited one page
from three passages. Five is also inside the reranker's window either way — the
semantic ranker scores up to 50 candidates however many are returned.
"""

VECTOR_CANDIDATES: Final = 50
"""``k`` for the vector half: how many nearest neighbours join the fusion.

The reranker considers up to 50 documents, so a larger ``k`` buys candidates
nothing downstream can read. On a 31-chunk corpus this is "all of them", which
is the honest description of what recall means at this size.
"""

CAPTIONS: Final = "extractive"
"""Semantic captions, on. The reranker returns the sentences it scored.

They are free — part of the same semantic request — and they are what D9's
*on-demand detail* expands to when a visitor taps a citation. Semantic
**answers** are deliberately *not* requested: an extractive answer is a second
answer, written by the service, competing with the one the agent is about to
write from the same passages, and two answers on one turn is a groundedness
question nobody needs to have.
"""

PUBLISHED_DISCLOSURE: Final = "PUBLISHED"
"""The ``allergen_disclosure`` value that means Chipotle published marks for an
item. The other is ``NOT_PUBLISHED``; ``silver.py`` constrains the column to the
two, and the difference between them is the whole of the allergen decision."""

ALLERGENS: Final[Mapping[str, str]] = {
    "dair": "dairy",
    "glut": "gluten",
    "soy": "soy",
    "sulp": "sulphites",
}
"""Published allergen code to the published label, lower-cased for matching.

Read off Chipotle's own tag vocabulary rather than chosen here: the chart sorts
every code it uses into an allergens list and a diets list, ``harvest`` parses
that into ``dietary_tags``, and these four are what the allergens half holds.
``search/tests/test_query.py`` asserts this mapping against the harvest fixture,
which is the same convention :mod:`chip_chat.search.chunks` uses for the chunk
schema: restated so this module can be read on its own, and checked equal by a
test rather than imported.

A fifth allergen added to the published group next month arrives in the fixture,
fails that test, and is added here. It does not arrive silently.
"""

ALLERGEN_SPELLINGS: Final[Mapping[str, str]] = {"sulfites": "sulphites"}
"""Spellings of a published label, which are not synonyms for it.

One entry. Chipotle spells it *Sulphites*; an American visitor types *sulfites*.
That is the same published word in the other orthography, not a second word
meaning roughly the same thing — which is the line this module holds everywhere
else. *Milk* is not in here, and should not be.
"""

DIETS: Final[Mapping[str, str]] = {
    "vege": "vegetarian",
    "vega": "vegan",
    "pale": "paleo",
    "keto": "keto",
}
"""Published diet codes, here only so a query can be *refused* precisely.

The chunk schema carries no diet column — :data:`chip_chat.search.chunks.FIELDS`
is the whole of it — so *"vegetarian"* is a constraint this index cannot apply,
and #49 lists it as one of the cases to handle. Handling it means saying so:
:func:`read` puts it in :attr:`Constraints.unapplied` and the agent is told the
passages are unfiltered. Guessing at it from ingredient text would be inventing
a dietary claim about food, which is the one place this demo cannot be creative.

There is a second reason not to reach for the published marks even where they
exist. The two documents Chipotle publishes agree exactly about allergens and
**disagree about diets** — the chart marks nine foods Whole30 under ``whol`` and
the menu metadata marks two under ``wh30``, and nothing published says those are
the same diet. See ``docs/decisions/allergen-absence.md``.
"""

_ALLERGEN_OF_LABEL: Final[Mapping[str, str]] = {
    label: code for code, label in ALLERGENS.items()
}

_NEGATION_CUE = re.compile(
    r"\b(?:without|no|nothing|none|not|non|free|avoid|avoiding|allergic|allergy"
    r"|intolerant|intolerance|cannot|can't|cant|skip|hold|leave|exclude|excluding"
    r"|minus|off)\b"
)
"""What turns a mention of an allergen into an exclusion.

Required, and that is the point. *"Does the cheese have dairy in it"* mentions
dairy and asks the opposite question; a filter built from the mention alone
would answer it backwards and confidently. A cue has to appear within
:data:`_CUE_WINDOW` characters before the label, or the label has to be followed
by *free* — *"dairy free"*, *"gluten-free"*.

Matched on **word boundaries** rather than as substrings, which is not
fastidiousness: *no* as a substring fires on *nothing*, *normally* and
*nutrition*, and one of those three appears in a great many menu questions.
"""

_CUE_WINDOW: Final = 30
"""How far before a label a negation cue counts. Roughly a clause."""

_FREE_AFTER: Final = re.compile(r"^[\s-]*free\b")

_UPPER_BOUNDS: Final[tuple[tuple[re.Pattern[str], bool], ...]] = (
    (
        re.compile(r"\b(?:under|below|less than|fewer than)\s+(\d{2,5})[\s-]*cal", re.I),
        False,
    ),
    (
        re.compile(
            r"\b(?:no more than|at most|up to|max(?:imum)? of|maximum)"
            r"\s+(\d{2,5})[\s-]*cal",
            re.I,
        ),
        True,
    ),
    (re.compile(r"\b(\d{2,5})[\s-]*calories?\s+or\s+(?:less|fewer|under)\b", re.I), True),
)
"""Upper calorie bounds, and whether each phrasing includes its own number.

*"Under 500 calories"* excludes a 500-calorie item and *"500 calories or less"*
includes it. The two differ on exactly one published figure, which is a small
thing to get right and a strange thing to get wrong on purpose.
"""

_LOWER_BOUNDS: Final[tuple[tuple[re.Pattern[str], bool], ...]] = (
    (re.compile(r"\b(?:over|above|more than)\s+(\d{2,5})[\s-]*cal", re.I), False),
    (
        re.compile(r"\b(?:at least|min(?:imum)? of|minimum)\s+(\d{2,5})[\s-]*cal", re.I),
        True,
    ),
    (re.compile(r"\b(\d{2,5})[\s-]*calories?\s+or\s+more\b", re.I), True),
)

_COMPARATIVE = re.compile(
    r"\b(?:fewer|fewest|less|least|lower|lowest|more|most|higher|highest|low|light)\b"
    r"[^.?!]{0,24}?\bcal",
    re.I,
)
"""A calorie comparison with no figure in it — *"which bowl has fewer calories"*.

There is nothing to filter on. The referent is a menu item the retrieval layer
cannot see, and inventing a bound would answer a different question. Every
passage carries its published ``calories``, so the comparison is the agent's to
make over what comes back; :attr:`Constraints.unapplied` is how it is told that.
"""

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "cant",
        "could",
        "did",
        "do",
        "does",
        "doesnt",
        "doing",
        "dont",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "us",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
        "you",
        "your",
    }
)
"""Words too common to be evidence that a passage matched.

Used only by :func:`overlap`, which is a floor rather than a ranker — see its
docstring. Deliberately short: a long list is a language model nobody trained.
"""

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Bound:
    """A numeric limit and whether it includes its own value.

    Attributes:
        value: The figure the visitor said.
        inclusive: Whether the phrasing included it — *at most 500* does,
            *under 500* does not.
    """

    value: float
    inclusive: bool

    def operator(self, *, upper: bool) -> str:
        """Return the OData comparison operator for this bound.

        Args:
            upper: Whether this is an upper bound.

        Returns:
            One of ``le``, ``lt``, ``ge``, ``gt``.
        """
        if upper:
            return "le" if self.inclusive else "lt"
        return "ge" if self.inclusive else "gt"


@dataclass(frozen=True, slots=True)
class Constraints:
    """What a query asked for beyond words, and what could not be honoured.

    Attributes:
        max_calories: An upper calorie bound, if the query stated one.
        min_calories: A lower one.
        without_allergens: Published allergen **codes** the visitor asked to
            avoid, in :data:`ALLERGENS` order.
        notes: Caveats that apply *because* a filter fired — chiefly that an
            unmarked item is not a safe item.
        unapplied: Constraints the query stated and this index cannot express.
            The agent has to be told, or it will read an unfiltered answer as a
            filtered one.
    """

    max_calories: Bound | None = None
    min_calories: Bound | None = None
    without_allergens: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    unapplied: tuple[str, ...] = field(default_factory=tuple)

    @property
    def filtered(self) -> bool:
        """Whether anything here narrows the answer set."""
        return bool(self.max_calories or self.min_calories or self.without_allergens)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready form, for the span and for the eval harness."""
        return {
            "max_calories": None
            if self.max_calories is None
            else self.max_calories.value,
            "min_calories": None
            if self.min_calories is None
            else self.min_calories.value,
            "without_allergens": list(self.without_allergens),
            "notes": list(self.notes),
            "unapplied": list(self.unapplied),
        }


def _bound(text: str, patterns: Iterable[tuple[re.Pattern[str], bool]]) -> Bound | None:
    """Return the first calorie bound in ``text`` matching any of ``patterns``."""
    for pattern, inclusive in patterns:
        found = pattern.search(text)
        if found is not None:
            return Bound(float(found.group(1)), inclusive)
    return None


def _excluded_allergens(text: str) -> tuple[str, ...]:
    """Return the published allergen codes ``text`` asks to avoid."""
    found: list[str] = []
    for spelling, published in ALLERGEN_SPELLINGS.items():
        text = text.replace(spelling, published)
    for code, label in ALLERGENS.items():
        for match in re.finditer(rf"\b{re.escape(label)}\b", text):
            before = text[max(0, match.start() - _CUE_WINDOW) : match.start()]
            after = text[match.end() :]
            negated = _NEGATION_CUE.search(before) is not None or bool(
                _FREE_AFTER.match(after)
            )
            if negated:
                found.append(code)
                break
    return tuple(code for code in ALLERGENS if code in found)


def read(query: str) -> Constraints:
    """Read the constraints a visitor's sentence states.

    Deterministic, and deliberately literal. Nothing here infers a constraint
    from a word the restaurant did not publish, and nothing approximates one the
    index cannot express — see the module docstring for why both matter more
    here than the recall they cost.

    Args:
        query: The visitor's words, as the tool received them.

    Returns:
        The constraints, with everything it declined to apply named in
        :attr:`Constraints.unapplied`.
    """
    text = query.casefold()
    maximum = _bound(text, _UPPER_BOUNDS)
    minimum = _bound(text, _LOWER_BOUNDS)
    allergens = _excluded_allergens(text)

    notes: list[str] = []
    unapplied: list[str] = []

    if allergens:
        names = ", ".join(ALLERGENS[code] for code in allergens)
        notes.append(
            f"Filtered to items whose published allergen marks do not include "
            f"{names}, and which publish allergen data at all. Chipotle's own "
            f"caveat is that an unmarked item is not a guaranteed-free item: say "
            f"'not marked', never 'free of'."
        )
    if maximum is None and minimum is None and _COMPARATIVE.search(text) is not None:
        unapplied.append(
            "A calorie comparison with no figure in it, so nothing was filtered. "
            "Every passage carries its published calories — compare them."
        )
    for label in DIETS.values():
        if re.search(rf"\b{re.escape(label)}\b", text) is not None:
            unapplied.append(
                f"'{label}' is a published dietary tag, and the corpus chunks "
                f"carry allergen marks but no dietary marks — so these passages "
                f"are NOT filtered to {label} items. Do not present them as if "
                f"they were."
            )
    return Constraints(
        max_calories=maximum,
        min_calories=minimum,
        without_allergens=allergens,
        notes=tuple(notes),
        unapplied=tuple(unapplied),
    )


def filter_expression(constraints: Constraints) -> str | None:
    """Return the OData ``$filter`` for ``constraints``, or ``None`` for none.

    Two of the three clauses restrict the answer to menu items, and that is
    correct rather than incidental. ``calories`` and ``allergens`` are populated
    only on ``MENU_ITEM`` chunks, and an OData comparison against a null is
    false — so *"under 500 calories"* cannot return a policy section, which is
    what the question asked.

    Args:
        constraints: What the query stated.

    Returns:
        The filter, or ``None`` if nothing narrows the answer set.
    """
    clauses: list[str] = []
    if constraints.max_calories is not None:
        bound = constraints.max_calories
        clauses.append(f"{chunks.CALORIES} {bound.operator(upper=True)} {bound.value:g}")
    if constraints.min_calories is not None:
        bound = constraints.min_calories
        clauses.append(f"{chunks.CALORIES} {bound.operator(upper=False)} {bound.value:g}")
    if constraints.without_allergens:
        # The disclosure clause is the one that keeps this honest: without it,
        # every chunk Chipotle publishes no allergen data about would arrive
        # inside an answer the visitor reads as "safe". See
        # docs/decisions/allergen-absence.md.
        clauses.append(f"{chunks.ALLERGEN_DISCLOSURE} eq '{PUBLISHED_DISCLOSURE}'")
        clauses.extend(
            f"not {chunks.ALLERGENS}/any(a: a eq '{code}')"
            for code in constraints.without_allergens
        )
    return " and ".join(clauses) if clauses else None


def body(
    query: str,
    *,
    constraints: Constraints | None = None,
    top: int = TOP,
    k: int = VECTOR_CANDIDATES,
    rerank: bool,
) -> dict[str, Any]:
    """Return the request body for one hybrid query.

    Both halves, always. The ``search`` string is scored by BM25 over the five
    searchable fields with the index's ``heading``-weighted profile; the
    ``vectorQueries`` entry is ``kind: "text"``, so the **service** embeds it
    with the deployment named on the index — the application never embeds a
    query, which is the one thing integrated vectorization exists to prevent it
    doing (``docs/retrieval-index.md`` §3).

    No ``select``. Every field of this index is retrievable except the vector,
    which is also ``stored: false``, so "everything retrievable" is exactly the
    chunk — and naming the fields here would be a second copy of the chunk
    schema, free to drift from the first.

    Args:
        query: The visitor's words. Sent verbatim: proper nouns are the part
            keyword search gets right, and normalising them away is how
            *barbacoa* stops being findable.
        constraints: What to narrow the answer set to. ``None`` means none.
        top: Passages to return.
        k: Nearest neighbours the vector half contributes to the fusion.
        rerank: Whether to ask for semantic reranking. ``False`` is the degrade
            path of :mod:`chip_chat.search.allowance`, not an error path — the
            query is still hybrid and still answers.

    Returns:
        A JSON-ready search request.
    """
    request: dict[str, Any] = {
        "search": query,
        "queryType": "semantic" if rerank else "simple",
        "top": top,
        "vectorQueries": [
            {"kind": "text", "text": query, "fields": VECTOR_FIELD, "k": k}
        ],
    }
    if rerank:
        request["semanticConfiguration"] = SEMANTIC_CONFIGURATION
        request["captions"] = CAPTIONS
    narrowing = filter_expression(constraints) if constraints is not None else None
    if narrowing is not None:
        request["filter"] = narrowing
    return request


def terms(text: str) -> frozenset[str]:
    """Return the content words of ``text``, lower-cased.

    Args:
        text: Any string.

    Returns:
        Words of three characters or more that are not stopwords.
    """
    return frozenset(
        word
        for word in _WORD.findall(text.casefold())
        if len(word) >= 3 and word not in _STOPWORDS
    )


def overlap(query_terms: frozenset[str], text: str) -> float:
    """Return the fraction of ``query_terms`` that appear in ``text``.

    A **floor**, not a ranker, and the distinction is the reason it exists.
    BM25 is what ranks the lexical half, and it does so inside the service with
    an analyzer this has no access to. What this answers is one cruder question:
    *did the keyword half have anything at all to match here* — because a
    passage sharing no content word with the query is a vector neighbour and
    nothing more, and RFC-001 §08 is explicit that a vector neighbour is exactly
    what a proper-noun query gets wrong.

    That question is worth answering separately because of the degrade path.
    When the reranker is off, the fused ``@search.score`` is a **rank** score —
    reciprocal rank fusion gives the top hit of a hopeless query the same number
    as the top hit of a perfect one — so it cannot separate a good answer from a
    near-miss, and this can. See :mod:`chip_chat.search.retrieve`.

    Args:
        query_terms: The query's content words, from :func:`terms`.
        text: The passage's searchable text.

    Returns:
        A fraction in ``[0, 1]``. Zero when the query had no content words,
        which is the honest answer for a query that was all stopwords.
    """
    if not query_terms:
        return 0.0
    haystack = terms(text)
    return sum(1 for word in query_terms if _present(word, haystack)) / len(query_terms)


def _present(word: str, haystack: frozenset[str]) -> bool:
    """Whether ``word`` appears in ``haystack``, allowing one trailing ``s``.

    The whole of the stemming, and it is deliberately the whole of it. *Points*
    should find *point*; anything past that is a stemmer, and a hand-rolled
    stemmer competing with the analyzer inside the service is a way to disagree
    with BM25 rather than to approximate it.
    """
    singular = word[:-1] if word.endswith("s") else word
    return word in haystack or f"{singular}s" in haystack or singular in haystack
