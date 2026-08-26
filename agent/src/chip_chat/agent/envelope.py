"""The response format: what the model returns, and what a visitor is shown.

Decision D9 (``docs/decisions/citation-presentation.md``) settled two things at
once, and the second is the one with engineering in it: citations are **inline
by default** and they are **a field rather than a sentence**. The model does not
write "-- Menu - Barbacoa" into its answer. It names the ids of the passages it
used, and the app draws the rest.

That split is why a citation cannot be minted. Look at what crosses the boundary
from the model in :class:`ModelResponse`: a string of prose, a tuple of ids, and
a claim class. Not a label, not a URL, not a harvest date. Every field a visitor
reads as evidence comes off a :class:`Citation` the *retriever* returned, and
:func:`render` -- the only route from a :class:`ModelResponse` to a
:class:`ResponseEnvelope` -- takes that mapping as a required argument. An id
the retriever did not return on this turn has nothing to resolve against, so it
is dropped and recorded in :attr:`ResponseEnvelope.dropped_citation_ids`, which
is what issue #75 counts.

Same move as D3's constrained vocabulary for the vision model, for the same
reason: make the failure structurally impossible rather than statistically rare.

**Confirmation cards are next door.** RFC-001 section 06 puts confirmation in
the ops API, and the card is minted by :mod:`chip_chat.agent.orders` alongside
the draft it describes -- with ``SIMULATION_NOTICE`` on it, from the same tool
result as the total, so the prose and the card cannot disagree. Nothing here
duplicates that. What is worth saying about it belongs with this module's
subject anyway: a card carries no field through which anything could be marked
confirmed, because confirmation is a fact the app records against the draft id
when the visitor taps, and the ops API reads it there. An agent that fabricated
a card would have fabricated a picture, not an authorisation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "CITED_CLAIM_CLASSES",
    "Citation",
    "CitationPlacement",
    "ClaimClass",
    "ModelResponse",
    "ResponseEnvelope",
    "render",
]


class ClaimClass(StrEnum):
    """What kind of claim a response makes, which decides whether it must cite.

    ``ACCOUNT`` exists so the citation rule does not fire where there is no
    published page to point at: *"you have 1,250 points"* is grounded in
    Snowflake, and a source link on it would be decoration.
    """

    FOOD = "food"
    POLICY = "policy"
    ALLERGEN = "allergen"
    ACCOUNT = "account"
    NONE = "none"


CITED_CLAIM_CLASSES = frozenset({ClaimClass.FOOD, ClaimClass.POLICY, ClaimClass.ALLERGEN})
"""The classes PRD K2 requires a citation on. Everything else cites nothing."""


class CitationPlacement(StrEnum):
    """Where the app draws the citation, per D9.

    ``ADJACENT`` is the allergen rule and it is stricter in three ways: the
    source renders with the claim rather than after the response, the harvest
    date is visible without interaction, and sources are never deduplicated
    away.
    """

    ADJACENT = "adjacent"
    TRAILING = "trailing"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Citation:
    """One retrieved passage, as the visitor is shown it.

    Every field here comes from the retrieval payload. None of them is ever read
    off model output -- that is the whole reason the type exists separately from
    the ids in :class:`ModelResponse`.
    """

    id: str
    label: str
    source_url: str
    harvested_at: str

    def as_dict(self) -> dict[str, str]:
        """Return the wire form D9 specifies."""
        return {
            "id": self.id,
            "label": self.label,
            "source_url": self.source_url,
            "harvested_at": self.harvested_at,
        }


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Everything the model is permitted to contribute to a response.

    Three fields, and the narrowness is the design. Prose, the ids of the
    passages the prose leans on, and what kind of claim it is. A label or a URL
    here would be a source a model could invent.
    """

    text: str
    citation_ids: tuple[str, ...] = ()
    claim_class: ClaimClass = ClaimClass.NONE


@dataclass(frozen=True, slots=True)
class ResponseEnvelope:
    """What the app renders. The wire format of D9, plus what was refused."""

    text: str
    citations: tuple[Citation, ...]
    claim_class: ClaimClass
    dropped_citation_ids: tuple[str, ...] = ()
    """Ids the model named that the retriever did not return on this turn.

    A violation, not a nuisance: issue #75 counts these, and an agent minting
    sources should be visible rather than silently tidied up.
    """

    @property
    def placement(self) -> CitationPlacement:
        """Where the app draws these citations."""
        if self.claim_class is ClaimClass.ALLERGEN:
            return CitationPlacement.ADJACENT
        if self.claim_class in CITED_CLAIM_CLASSES:
            return CitationPlacement.TRAILING
        return CitationPlacement.NONE

    @property
    def deduplicate_by_source(self) -> bool:
        """Whether the app may collapse several passages from one page.

        True for the ordinary trailing line, and false for allergens, where D9
        forbids it -- in an answer covering three items it has to stay
        unambiguous which source backs which claim.
        """
        return self.placement is CitationPlacement.TRAILING

    @property
    def uncited_claim(self) -> bool:
        """True when a food, policy or allergen claim carries no citation.

        PRD K2's target for this is zero, and D9's point is that it is now a
        rule rather than a judgement: this property is the rule.
        """
        return self.claim_class in CITED_CLAIM_CLASSES and not self.citations

    def as_dict(self) -> dict[str, Any]:
        """Return the response envelope D9 specifies."""
        return {
            "text": self.text,
            "citations": [citation.as_dict() for citation in self.citations],
            "claim_class": self.claim_class.value,
        }


def render(
    response: ModelResponse, *, retrieved: Mapping[str, Citation]
) -> ResponseEnvelope:
    """Turn model output into a response envelope, resolving citations.

    The only route from a :class:`ModelResponse` to a :class:`ResponseEnvelope`,
    and it cannot be walked without the turn's retrieval results -- which is how
    "the model cannot mint a source" stops being a hope.

    Args:
        response: What the model returned.
        retrieved: The passages ``retriever.search`` actually returned on this
            turn, keyed by id. Empty on a turn that retrieved nothing, in which
            case every id the model named is dropped.

    Returns:
        The envelope, with unresolvable ids moved to
        :attr:`ResponseEnvelope.dropped_citation_ids`.
    """
    citations: list[Citation] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for citation_id in response.citation_ids:
        if citation_id in seen:
            continue
        seen.add(citation_id)
        resolved = retrieved.get(citation_id)
        if resolved is None:
            dropped.append(citation_id)
        else:
            citations.append(resolved)
    return ResponseEnvelope(
        text=response.text,
        citations=tuple(citations),
        claim_class=response.claim_class,
        dropped_citation_ids=tuple(dropped),
    )
