"""The golden set: one question, the lane it should take, and what counts as right.

Issue #29 is trap 6 with a deadline on it -- *"evaluating last. By then you've
made a hundred untested choices."* -- so this set is written in Phase 2, against
a slice that will fail most of it, and the failing is the point.

Four properties of the shapes here, each of which is a way a golden set stops
measuring anything.

**Every entry records the lane, and the tool inside it.** That is #29's second
acceptance criterion and the metric the whole five-lane architecture exists to
get right. Recording only the lane would make the confusable pairs unscoreable
-- ``get_points_balance`` and ``ask_account_question`` are one lane and two very
different answers -- so a case names the tool and :func:`~chip_chat.eval.golden.
lanes.lane_of` derives the lane, which is why a per-lane rate cannot drift from
what it was computed over.

**An expectation is a set of named checks, not prose.** :class:`Check` is closed,
and each member says what would have to be observed. "Answers correctly" is not
in it, because nothing can score that; "carries a citation the retriever actually
returned" is, because :mod:`chip_chat.agent.envelope` made it a rule rather than
a judgement.

**Some checks need a judge, and are unscored without one.** Whether an answer
*declines plainly* is not a property of a data structure. :data:`JUDGED` names
those checks, :mod:`chip_chat.eval.golden.scoring` reports them as unscored
rather than passed when no judge is supplied, and a set full of unscored checks
reads as the unmeasured thing it is. This is the shape #72 fills in when the set
becomes an Arize dataset with online evals behind it.

**A case names the menu terms it leans on, and they are checked.** RFC-001
section 07 wants stage 4's enums generated from the live catalogue; ``cc-z1i``
records that nothing wires that generation yet, so the vocabulary in the tree can
drift from the catalogue with nothing to say so. :meth:`GoldenSet.against` is a
cheap detector for the same class of failure on this side: a case asking about
barbacoa, run against a build that does not publish barbacoa, is a case the
deployment cannot pass for a reason that has nothing to do with the agent. Being
told that before the run costs nothing; finding it in a pass rate costs a day.

The manifest is JSON, one file, hand-edited -- the same argument as the labeled
photo set, plus one of its own: #72 promotes this into a versioned Arize dataset,
and a dataset that started life as test code has to be rewritten to get there.

.. code-block:: json

    {
      "cases": [
        {
          "id": "k1-ingredients-bowl",
          "message": "what's actually in a burrito bowl?",
          "persona": "any",
          "tool": "search_menu_knowledge",
          "requirements": ["K1", "K2"],
          "checks": ["cites", "grounded"],
          "menu_terms": ["burrito bowl", "white rice"],
          "why": "The plainest knowledge question there is."
        }
      ]
    }
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.eval.golden.requirements import OUT_OF_SCOPE, requirement
from chip_chat.otel.schema import WRITE_TOOLS, ToolName

__all__ = [
    "ANY_PERSONA",
    "DEFAULT_MANIFEST",
    "DIETARY_WORDS",
    "JUDGED",
    "CaseError",
    "Check",
    "GoldenCase",
    "GoldenSet",
]

DEFAULT_MANIFEST: Final = Path("eval/golden/cases.json")
"""Where the set lives, relative to the repository root."""

ANY_PERSONA: Final = "any"
"""The persona value for a case whose answer does not depend on who is asking."""


class Check(StrEnum):
    """One thing that has to be observed for a case to have passed.

    Routing is not here: every case with an expected tool is scored on routing,
    always, because that is the metric and making it opt-in would let a case
    quietly stop contributing to it.

    Attributes:
        CITES: The response carries at least one citation, resolved from what
            the retriever returned on this turn. PRD K2, and deterministic --
            :attr:`~chip_chat.agent.envelope.ResponseEnvelope.uncited_claim`
            is the rule this reads.
        CITES_ADJACENT: The citation renders beside the claim with its harvest
            date visible, rather than as a trailing line. PRD K5's stricter
            half, which applies to allergen answers.
        DECLINES: The answer says plainly that it cannot answer, rather than
            inferring. PRD K3 and A4. Judged.
        GROUNDED: Every food or policy claim in the prose is supported by the
            passages cited. PRD's groundedness target. Judged.
        EXPLAINS: The answer says briefly how it worked something out. PRD P1.
            Judged.
        CONFIRMS_FIRST: A confirmation card was rendered and no write executed
            on this turn. PRD T2, and the second launch gate.
        SIMULATED: The card says the action is simulated. PRD T5.
        RECEIPT: A receipt was returned. PRD T4.
        EDITABLE: The turn changed a draft already on screen rather than
            starting a new conversation. PRD T3.
        NO_WRITE: No write tool executed. What separates *proposed* from
            *placed* on a turn that asked for the second without the first.
    """

    CITES = "cites"
    CITES_ADJACENT = "cites_adjacent"
    DECLINES = "declines"
    GROUNDED = "grounded"
    EXPLAINS = "explains"
    CONFIRMS_FIRST = "confirms_first"
    SIMULATED = "simulated"
    RECEIPT = "receipt"
    EDITABLE = "editable"
    NO_WRITE = "no_write"


JUDGED: Final[frozenset[Check]] = frozenset(
    {Check.DECLINES, Check.GROUNDED, Check.EXPLAINS}
)
"""Checks no data structure can settle, which therefore need a judge.

Three, and they are the three the PRD writes as sentences about meaning rather
than as properties of a payload: *says so plainly*, *grounded in published data*,
*briefly how it worked that out*. Everything else in :class:`Check` is a field
that is either there or not.

Scoring reports these as unscored where no judge is supplied. That is deliberate
and it is the whole reason the distinction is drawn: a keyword list that looked
for "I don't know" would produce a number, and the number would measure the
keyword list.
"""


DIETARY_WORDS: Final[frozenset[str]] = frozenset(
    {
        "allergen",
        "allergens",
        "allergic",
        "allergy",
        "celiac",
        "dairy",
        "gluten",
        "halal",
        "kosher",
        "lactose",
        "milk",
        "nut",
        "nuts",
        "peanut",
        "peanuts",
        "pescatarian",
        "sesame",
        "shellfish",
        "soy",
        "vegan",
        "vegetarian",
        "wheat",
    }
)
"""Words that make a question an allergen or dietary one, for the marker check.

Hand-written and short, and used in exactly one direction: a case whose message
holds one of these and is *not* marked
:attr:`~GoldenCase.dietary` is refused at load. See :meth:`GoldenCase.dietary`
for why the flag is not derived from this list instead -- *"are the black beans
cooked in the same pot as the chicken"* is a cross-contact question and holds no
word here, so a derivation would silently drop it out of the category where a
wrong answer costs the most.
"""


class CaseError(ValueError):
    """A manifest that cannot be believed as a golden set.

    Raised at load, never at score time -- the same rule the labeled photo set
    follows, for the same reason. A set that contradicts itself produces
    numbers that look exactly like numbers.
    """


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One entry: what is asked, where it should go, and what counts as right.

    Attributes:
        case_id: Stable identifier, unique in the set, and the join key #72
            will carry into the Arize dataset. It should read as a description
            of the case rather than as a number.
        message: What the visitor says, in the register a visitor would use.
            Lower case and unpunctuated where that is what people type: a set
            written in polished English measures a population that does not
            exist.
        persona: Which persona the case presumes -- a ``persona_id`` from
            ``data-gen``'s ``population.toml``, or :data:`ANY_PERSONA`. Account
            and personalization answers depend on who is asking, and a case run
            against the wrong persona is scoring the wrong question.
        context: Prior assistant turns the case presupposes, in order. *"Yes,
            place it"* is not answerable without a draft on screen, and putting
            that state in the case rather than in the runner is what keeps the
            set a description of conversations rather than of a fixture.
        tool: The tool this turn should reach for, or ``None`` where it should
            reach for none.
        lane: Derived from :attr:`tool`; carried so a reader of the JSON does
            not have to know the mapping. Refused at load when the two
            disagree.
        requirements: The PRD identifiers this case covers. At least one, and
            every one of them real -- a typo here would leave a requirement
            looking covered by a case that does not exist.
        checks: What has to be observed. See :class:`Check`.
        forbidden_tools: Tools that must not be called on this turn. The
            confusable half of a boundary case: *"how do points work"* is
            wrong in a specific way, and naming that way is worth more than
            recording that it was wrong.
        confirmed: Whether the visitor has already pressed Confirm on the draft
            this turn acts on. The one piece of screen state a golden case has
            to carry, because PRD T2 is a statement about *ordering* -- a write
            is correct after a confirmation and a launch-gate failure before
            one -- and the same message is right or wrong depending on which
            side of it the turn sits. Never true outside the action lane.
        dietary: Whether this is an allergen or dietary question. Issue #75
            scores these as their own category and holds them to a bar that is
            a count rather than a rate, because a percentage of a safety
            property is the wrong shape for the thing being promised. Declared
            rather than derived: the requirement ids cannot settle it (K3
            covers halal *and* cross-contact, K5 the two allergen ones, and
            *"what's vegetarian here"* is a K4 case and a dietary question),
            and a word list cannot either. :data:`DIETARY_WORDS` is the
            staleness detector for the flag rather than a substitute for it.
        menu_terms: Published menu terms the case leans on, as a person would
            write them. Checked against a built catalogue by
            :meth:`GoldenSet.against`.
        why: What this case is for, printed beside a failure. The field is
            required, because a case nobody can explain is a case nobody can
            fix.
    """

    case_id: str
    message: str
    tool: ToolName | None
    lane: Lane
    requirements: tuple[str, ...]
    checks: frozenset[Check] = frozenset()
    persona: str = ANY_PERSONA
    context: tuple[str, ...] = ()
    forbidden_tools: frozenset[ToolName] = frozenset()
    confirmed: bool = False
    dietary: bool = False
    menu_terms: tuple[str, ...] = ()
    why: str = ""

    @property
    def writes(self) -> bool:
        """Whether the expected tool is one of the four that write."""
        return self.tool in WRITE_TOOLS

    @property
    def judged_checks(self) -> frozenset[Check]:
        """The checks on this case that need a judge."""
        return self.checks & JUDGED

    @property
    def deterministic_checks(self) -> frozenset[Check]:
        """The checks on this case that can be settled from the observation."""
        return self.checks - JUDGED


@dataclass(frozen=True, slots=True)
class GoldenSet:
    """Every case, and where it was loaded from.

    Attributes:
        cases: The set, in manifest order.
        source: The manifest's path, so a report can say which set it scored.
    """

    cases: tuple[GoldenCase, ...]
    source: Path

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[GoldenCase]:
        return iter(self.cases)

    def by_lane(self, lane: Lane) -> tuple[GoldenCase, ...]:
        """Every case expecting ``lane``, in set order."""
        return tuple(case for case in self.cases if case.lane is lane)

    def covering(self, requirement_id: str) -> tuple[GoldenCase, ...]:
        """Every case referencing ``requirement_id``, in set order."""
        return tuple(case for case in self.cases if requirement_id in case.requirements)

    @classmethod
    def load(cls, manifest: Path = DEFAULT_MANIFEST) -> "GoldenSet":
        """Read a manifest, and refuse one that contradicts itself.

        The menu terms are *not* checked here, for the same reason the photo
        set's labels are not: loading should not require a built catalogue, so
        that ``--check`` works on a laptop that has never built one.
        :meth:`against` is that check.

        Args:
            manifest: Path to the JSON file.

        Returns:
            The set.

        Raises:
            CaseError: If the file is not readable as a manifest, or any case
                contradicts itself or the design it is scoring.
        """
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as error:
            raise CaseError(f"could not read {manifest}: {error}") from error
        except json.JSONDecodeError as error:
            raise CaseError(f"{manifest} is not valid JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
            raise CaseError(f"{manifest} must be an object with a `cases` array")

        cases = tuple(_case(entry, index) for index, entry in enumerate(payload["cases"]))
        seen: set[str] = set()
        for case in cases:
            if case.case_id in seen:
                raise CaseError(f"duplicate case id {case.case_id!r}")
            seen.add(case.case_id)
        return cls(cases=cases, source=manifest)

    def against(self, catalog: MenuCatalog) -> None:
        """Check every named menu term against a built catalogue.

        The staleness detector. A case naming a term this build does not
        publish is a case the deployment cannot pass, for a reason that is not
        the agent's: either the catalogue moved and the set did not, or the set
        was written against a different build. Both are worth knowing before a
        run rather than after one.

        Args:
            catalog: The build the deployment serves. Item names and the
                generated vocabulary are both consulted, because a case may
                name an entree (*"steak burrito"*) or a component the vision
                vocabulary spells (*"white rice"*), and either is a published
                term.

        Raises:
            CaseError: Naming the first case and term that do not resolve. The
                set is refused rather than the case, on the photo set's
                argument: one term from another build means the manifest was
                written against a menu this deployment does not serve, and the
                rest of it is no more trustworthy.
        """
        published = _published_terms(catalog)
        for case in self.cases:
            for term in case.menu_terms:
                if _normalize(term) not in published:
                    raise CaseError(
                        f"{case.case_id}: the catalogue publishes no menu term {term!r}"
                    )


def _published_terms(catalog: MenuCatalog) -> frozenset[str]:
    """Every published name a case may lean on, normalized.

    Item names, vocabulary values and the published names those values were
    slugified from. Three spellings of the same fact, and a case may reasonably
    use any of them: a person writing *"white rice"* should not have to know
    whether the catalogue spells it ``white_rice`` or ``White Rice``.
    """
    terms = {_normalize(item.name) for item in catalog.menu_items}
    for term in catalog.vocabulary:
        terms.add(_normalize(term.value))
        terms.add(_normalize(term.name))
    return frozenset(terms)


_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def _normalize(term: str) -> str:
    """Fold a term to the form both sides of the comparison are folded to."""
    return _NOT_ALPHANUMERIC.sub(" ", term.lower()).strip()


def _case(entry: object, index: int) -> GoldenCase:
    """Build one case from its manifest entry, refusing anything incoherent."""
    if not isinstance(entry, dict):
        raise CaseError(f"cases[{index}] must be an object")
    where = entry.get("id", f"cases[{index}]")

    case_id = _text(entry, "id", where)
    message = _text(entry, "message", where)
    why = _text(entry, "why", where)
    tool = _tool(entry.get("tool"), where)
    lane = _lane(entry.get("lane"), where, tool)
    requirements = tuple(
        _requirement(value, where) for value in _list(entry, "requirements")
    )
    checks = frozenset(_check(value, where) for value in _list(entry, "checks"))
    forbidden = frozenset(
        _named_tool(value, where) for value in _list(entry, "forbidden_tools")
    )
    menu_terms = tuple(str(value) for value in _list(entry, "menu_terms"))
    context = tuple(str(value) for value in _list(entry, "context"))
    persona = str(entry.get("persona", ANY_PERSONA))
    confirmed = bool(entry.get("confirmed", False))
    dietary = bool(entry.get("dietary", False))

    case = GoldenCase(
        case_id=case_id,
        message=message,
        tool=tool,
        lane=lane,
        requirements=requirements,
        checks=checks,
        persona=persona,
        context=context,
        forbidden_tools=forbidden,
        confirmed=confirmed,
        dietary=dietary,
        menu_terms=menu_terms,
        why=why,
    )
    _coherent(case, where)
    return case


def _coherent(case: GoldenCase, where: str) -> None:
    """Refuse a case that contradicts itself, or the design it is scoring.

    Every rule here is a rule about what a *golden case* can be evidence for.
    """
    if not case.requirements:
        # A case covering nothing cannot fail the coverage check and cannot be
        # traded away for a better one either, because nobody can see what it
        # was buying.
        raise CaseError(f"{where}: a case must reference at least one requirement")
    if case.tool is not None and case.forbidden_tools & {case.tool}:
        raise CaseError(f"{where}: {case.tool.value} is both expected and forbidden")
    if case.writes and Check.CONFIRMS_FIRST not in case.checks:
        # PRD T2 is unconditional -- *every* action renders a card before it
        # happens -- so a write case that did not check for one would be a
        # case in which the second launch gate was optional.
        raise CaseError(
            f"{where}: a case expecting {case.tool} must check confirms_first; "
            "PRD T2 has no exceptions"
        )
    if case.confirmed and case.lane is not Lane.ACTION:
        raise CaseError(f"{where}: only an action turn can act on a confirmed draft")
    if case.confirmed and not case.context:
        # A confirmation the visitor gave to nothing. The draft has to be on
        # screen for Confirm to have been pressable, and the case's context is
        # where that screen state lives.
        raise CaseError(
            f"{where}: a confirmed draft needs the turn that put it on screen "
            "in `context`"
        )
    if Check.CITES_ADJACENT in case.checks and Check.CITES not in case.checks:
        raise CaseError(
            f"{where}: cites_adjacent without cites checks placement of nothing"
        )
    unmarked = _dietary_words(case.message)
    if unmarked and not case.dietary:
        # Silence is not absence, the same rule the photo set applies to a slot
        # that is neither read nor named unreadable. A case that asks about soy
        # and is not in the category is a case #75 scores against the ordinary
        # bar, and nothing anywhere would say so.
        raise CaseError(
            f"{where}: the message asks about {', '.join(sorted(unmarked))} but "
            "the case is not marked `dietary`; see #75's stricter category"
        )
    if not case.why.strip():
        raise CaseError(f"{where}: a case must say what it is for")


def _dietary_words(message: str) -> frozenset[str]:
    """The allergen and dietary words a message uses. See :data:`DIETARY_WORDS`."""
    words = _NOT_ALPHANUMERIC.sub(" ", message.lower()).split()
    return frozenset(word for word in words if word in DIETARY_WORDS)


def _tool(value: object, where: str) -> ToolName | None:
    """The expected tool, or ``None`` where the case expects no call."""
    if value is None:
        return None
    return _named_tool(value, where)


def _named_tool(value: object, where: str) -> ToolName:
    try:
        return ToolName(str(value))
    except ValueError as error:
        raise CaseError(f"{where}: {value!r} is not one of the eleven tools") from error


def _lane(value: object, where: str, tool: ToolName | None) -> Lane:
    """The lane, checked against the tool rather than trusted.

    The manifest carries the lane so that the JSON reads as the five-lane table
    it is scoring. Deriving it silently would make a typo invisible; refusing a
    disagreement makes the redundancy worth having.
    """
    derived = lane_of(tool)
    if value is None:
        return derived
    try:
        declared = Lane(str(value))
    except ValueError as error:
        raise CaseError(f"{where}: {value!r} is not one of the five lanes") from error
    if declared is not derived:
        raise CaseError(
            f"{where}: lane {declared.value} does not hold "
            f"{'no tool' if tool is None else tool.value}"
        )
    return declared


def _requirement(value: object, where: str) -> str:
    """A PRD identifier, checked against the register."""
    identifier = str(value)
    if identifier in OUT_OF_SCOPE:
        raise CaseError(
            f"{where}: {identifier} is an Entry requirement and has no "
            f"conversational turn to score -- {OUT_OF_SCOPE[identifier]}"
        )
    try:
        requirement(identifier)
    except KeyError as error:
        raise CaseError(f"{where}: the PRD has no requirement {identifier}") from error
    return identifier


def _check(value: object, where: str) -> Check:
    try:
        return Check(str(value))
    except ValueError as error:
        raise CaseError(f"{where}: {value!r} is not a check") from error


def _text(entry: Mapping[str, object], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise CaseError(f"{where}: {key} must be a non-empty string")
    return value


def _list(entry: Mapping[str, object], key: str) -> Sequence[object]:
    value = entry.get(key, [])
    if not isinstance(value, list):
        raise CaseError(f"{key} must be an array")
    return value
