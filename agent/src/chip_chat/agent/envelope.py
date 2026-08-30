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

**And the model has to be read before any of that can happen**, which is what
:func:`parse` is for and what bead ``chip-2ky`` was. This module was correct,
tested, and reachable only from ``eval/`` -- so on the deployment the model
appended ``{"claim_class":"food","citations":[...]}`` to its answer, nothing
turned that line into a field, and the visitor read it. ``api/`` now runs the
whole path on every turn: the loop collects the citations
``retriever.search`` returned, :func:`parse` separates the prose from the
declared field, :func:`render` resolves the ids against what was actually
retrieved, and the widget draws the source line. A reply that is not an envelope
at all is prose, unchanged -- see :func:`parse` for why every failure here has
to fail towards showing the sentence.

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

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CITED_CLAIM_CLASSES",
    "Citation",
    "CitationPlacement",
    "ClaimClass",
    "ModelResponse",
    "ResponseEnvelope",
    "citations_from",
    "parse",
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


def citations_from(payload: Mapping[str, Mapping[str, str]]) -> dict[str, Citation]:
    """Turn a retrieval's citation payload into the mapping :func:`render` takes.

    :meth:`chip_chat.search.retrieve.Retrieval.citations` already returns the
    four fields D9 specifies, keyed by chunk id, and this is the one place they
    become the typed value the renderer resolves against. Keeping the
    conversion here rather than in the tool layer means the tool layer never
    holds a half-built citation, and the type stays the only thing a visitor's
    evidence can be made of.

    Args:
        payload: ``{chunk_id: {id, label, source_url, harvested_at}}``.

    Returns:
        The same mapping with each value a :class:`Citation`. A passage missing
        any of the four fields is dropped rather than defaulted: a citation with
        an empty ``source_url`` would render as evidence pointing nowhere, which
        is worse than no citation and would be counted as neither.
    """
    resolved: dict[str, Citation] = {}
    for identifier, fields in payload.items():
        label = fields.get("label", "")
        source_url = fields.get("source_url", "")
        harvested_at = fields.get("harvested_at", "")
        if not identifier or not label or not source_url or not harvested_at:
            continue
        resolved[identifier] = Citation(
            id=identifier,
            label=label,
            source_url=source_url,
            harvested_at=harvested_at,
        )
    return resolved


_ENVELOPE_MARKERS: Final = frozenset({"citations", "claim_class"})
"""Keys whose presence makes an object the response envelope rather than prose.

Neither of them is a word a model writes into an answer about burritos by
accident, and requiring one of the two is what stops :func:`parse` eating a
trailing JSON object that a visitor actually asked to see.
"""

_TEXT_KEY: Final = "text"


def parse(content: str | None) -> ModelResponse:
    """Read a model's reply as a :class:`ModelResponse`, however it was written.

    D9 says the model names ids and the app draws the citation, and the system
    prompt asks for them *"in the citations field of your response"*. What the
    deployed model actually did with that instruction was append the field to
    its answer as a line of JSON::

        Moderately. It's braised with chipotle chiles and cumin, ...
        {"claim_class":"food","citations":["53b556ab","5e613323"]}

    which nothing parsed, so ``Conversation`` handed the whole string back and
    the visitor read the second line. That is bead ``chip-2ky`` and it is the
    thing this function exists to stop.

    **Three shapes are accepted and everything else is prose.** The whole reply
    as one JSON object (what a ``response_format`` would produce); a trailing
    JSON object after the prose, fenced or not (what the deployment does); and
    anything at all, which comes back as :class:`ModelResponse` with the text
    untouched, no citations and :attr:`ClaimClass.NONE`.

    That last branch is the load-bearing one. A reply is a visitor-facing
    sentence first and a data structure second, so every failure here has to
    fail towards showing the sentence: an unparseable envelope, a half-written
    one, a model that ignored the instruction entirely, or a turn that answered
    in prose because prose was the right answer all render as what the model
    said. The alternative -- raising, or blanking the reply -- would turn a
    formatting disagreement into a broken conversation, and RFC-001 §10's rule
    that a lane may fail and the conversation may not is not weaker for the
    failure being ours.

    Args:
        content: What the model returned, or ``None``.

    Returns:
        The prose, the ids it named, and the claim class it declared. Never
        raises.
    """
    stripped = (content or "").strip()
    if not stripped:
        return ModelResponse(text="")
    whole = _envelope_object(stripped, require_marker=False)
    if whole is not None:
        return _from_object(whole, prose="")
    prose, tail = _trailing_object(stripped)
    if tail is not None:
        parsed = _envelope_object(tail, require_marker=True)
        if parsed is not None:
            return _from_object(parsed, prose=prose)
    return ModelResponse(text=stripped)


def _from_object(body: Mapping[str, Any], *, prose: str) -> ModelResponse:
    """Build a :class:`ModelResponse` from a parsed envelope object.

    ``prose`` is what stood before a trailing envelope and wins over the
    object's own ``text`` only when the object has none, because a model that
    wrote its answer twice -- once as prose and once inside the field -- has
    told us the same thing twice and the visitor should read it once.
    """
    text = body.get(_TEXT_KEY)
    answer = text.strip() if isinstance(text, str) and text.strip() else prose.strip()
    return ModelResponse(
        text=answer,
        citation_ids=_ids(body.get("citations")),
        claim_class=_claim_class(body.get("claim_class")),
    )


def _ids(value: Any) -> tuple[str, ...]:
    """Read the citation ids off whatever the model put in the field.

    A list of strings is the documented shape. A list of objects with an ``id``
    is accepted too, because a model shown ``{"id": ..., "label": ...}`` in a
    retrieval result sometimes echoes the whole thing back -- and the extra
    fields on such an echo are ignored rather than trusted, which is the same
    rule :class:`ModelResponse` enforces by having nowhere to put them.
    """
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            found.append(item.strip())
        elif isinstance(item, Mapping):
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier.strip():
                found.append(identifier.strip())
    return tuple(found)


def _claim_class(value: Any) -> ClaimClass:
    """Read the claim class, defaulting to :attr:`ClaimClass.NONE`.

    An unrecognised value is ``NONE`` rather than an error. It costs a citation
    line on one answer; treating it as a failure would cost the answer.
    """
    if not isinstance(value, str):
        return ClaimClass.NONE
    try:
        return ClaimClass(value.strip().lower())
    except ValueError:
        return ClaimClass.NONE


def _envelope_object(raw: str, *, require_marker: bool) -> Mapping[str, Any] | None:
    """Parse ``raw`` as an envelope object, or return ``None``.

    Args:
        raw: A candidate JSON object, possibly inside a fenced code block.
        require_marker: Whether the object must carry ``citations`` or
            ``claim_class``. True for a *trailing* object, where the alternative
            reading is that the model deliberately showed the visitor some JSON;
            false for a reply that is nothing but the object, where there is no
            prose for it to have been part of.
    """
    body = _fenced(raw).strip()
    if not body.startswith("{") or not body.endswith("}"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if require_marker and not (_ENVELOPE_MARKERS & set(parsed)):
        return None
    return parsed


def _fenced(raw: str) -> str:
    """Return ``raw`` with a surrounding Markdown code fence removed."""
    body = raw.strip()
    if not body.startswith("```"):
        return body
    body = body[3:]
    newline = body.find("\n")
    if newline >= 0 and not body[:newline].strip().startswith("{"):
        body = body[newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body


def _trailing_object(content: str) -> tuple[str, str | None]:
    """Split a reply into the prose before a trailing JSON object, and that object.

    Scanned backwards from the end with a brace counter that skips over string
    literals, rather than matched with a regular expression: a citation label
    with a brace in it, or a nested object in the field, would defeat the
    pattern and this cannot. A reply that does not end in an object comes back
    as itself and ``None``.

    Returns:
        ``(prose, candidate)``. ``candidate`` is ``None`` when there is no
        balanced object at the end of ``content``.
    """
    body = content.rstrip()
    fenced = body.endswith("```")
    if fenced:
        body = body[:-3].rstrip()
    if not body.endswith("}"):
        return content, None
    depth = 0
    in_string = False
    for position in range(len(body) - 1, -1, -1):
        character = body[position]
        if in_string:
            # Walking backwards, a quote closes the literal unless the run of
            # backslashes immediately before it is odd -- which is what makes
            # this different from the forward scan everybody writes first.
            if character == '"' and not _escaped_at(body, position):
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character == "}":
            depth += 1
        elif character == "{":
            depth -= 1
            if depth == 0:
                return _unfence(body[:position]), body[position:]
    return content, None


def _unfence(prose: str) -> str:
    """Drop the opening fence a model left behind when it fenced its envelope.

    `````json`` on its own line before the object is part of the
    envelope's packaging and not part of the answer, and leaving it on the end
    of the prose would swap one visible artefact for a smaller one.
    """
    body = prose.rstrip()
    if body.endswith("```json"):
        return body[: -len("```json")].rstrip()
    if body.endswith("```"):
        return body[:-3].rstrip()
    return prose


def _escaped_at(body: str, position: int) -> bool:
    """Whether the character at ``position`` is preceded by an escaping backslash."""
    backslashes = 0
    index = position - 1
    while index >= 0 and body[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1
