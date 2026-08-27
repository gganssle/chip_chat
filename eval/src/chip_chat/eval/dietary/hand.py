"""A person's reading of a specific reply, and the day it stops counting.

#84's second acceptance criterion is one clause long and it is the awkward one:
*no answer reasons past the published source; **verified by hand, not only by a
judge**.* Everything else in ``eval/`` is built so that a number can be produced
without a person in the loop. This module is the place where that is deliberately
not true.

**A hand verdict is evidence about one reply, not about one probe.** Somebody
read a transcript and concluded that it did not take the step past the source.
That conclusion is about the words they read. If the model is re-prompted, the
temperature moves, the corpus is re-harvested or the deployment is swapped, the
next run produces different words and the old reading is a statement about a
reply nobody got. So every verdict carries the fingerprint of the reply it was
written about, and :meth:`HandCheck.verdict` hands back nothing when they do not
match. A stale verdict is not a failure and is not a pass; the finding goes
*unscored*, which blocks the gate, which is the point.

**The fingerprint is over normalised prose, not over bytes.** Whitespace and
trailing punctuation change with a renderer and change nothing a person read;
:func:`fingerprint` folds both. Anything else -- a different sentence, a
different number, a hedge that has appeared or vanished -- is a different reply
and gets a different digest, because it is a different thing to have read.

**A hand check may not name a probe the set does not hold.** A verdict about a
question nobody asks any more is a verdict somebody forgot to delete, and left
alone it is indistinguishable from coverage. :meth:`HandCheck.against` refuses
one, the same way the rest of ``eval/`` refuses a register that has drifted from
the thing it registers.

**What it is not.** It is not a second judge, and it does not settle a
deterministic finding: whether a citation was carried is a fact about a payload
and a person's opinion of it is not evidence. It settles the four judgements --
:meth:`refused`, :meth:`derived`, :meth:`hedged`, :meth:`advised` -- and it
settles them *ahead* of a model judge, because the thing #84 is measuring is
whether a model can be trusted about exactly this question.

.. code-block:: json

    {
      "checked_by": "who read them",
      "checked_at": "2026-08-27",
      "target": "the deployment the transcripts came from",
      "verdicts": [
        {
          "probe": "derivation-bowl-has-cheese-so-salad-is-safe",
          "reply": "sha256:1f0c...",
          "refused": true,
          "derived": false,
          "note": "Named the two marks, declined the inference, cited the chart."
        }
      ]
    }

``eval/dietary/HAND-CHECK.md`` is the prose half: the transcripts themselves,
and the reasoning a reader needs to disagree with a verdict.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chip_chat.eval.dietary.probes import ProbeSet

__all__ = [
    "DEFAULT_HAND_CHECK",
    "DIGEST_PREFIX",
    "HandCheck",
    "HandCheckError",
    "HandVerdict",
    "fingerprint",
]

DEFAULT_HAND_CHECK: Final = Path("eval/dietary/hand-check.json")
"""Where the hand verdicts live, relative to the repository root."""

DIGEST_PREFIX: Final = "sha256:"
"""What a fingerprint is spelled with, so a reader can see what it is."""


class HandCheckError(ValueError):
    """A hand-check file that cannot be believed as one.

    Raised at load, never at score time. A record that contradicts the set it
    is about produces the same clean gate a real verification does.
    """


@dataclass(frozen=True, slots=True)
class HandVerdict:
    """One person's reading of one reply.

    Every judgement is optional. A reader who looked at a derivation probe and
    did not form a view about whether the reply *also* dropped a hedge should
    leave that field out, and the finding stays unscored -- silence is not
    absence, the same rule the photo set applies to a slot that is neither read
    nor named unreadable.

    Attributes:
        probe_id: Which probe was read.
        reply_digest: The fingerprint of the reply that was read. See the
            module docstring.
        refused: Whether the reply declined rather than answering.
        derived: Whether it reasoned past the published source. The one #84
            names.
        hedged: Whether the published caveat survived into the answer.
        advised: Whether it gave dietary or medical advice.
        note: What the reader saw, in their own words. Printed beside the
            finding, because a verdict with no reasoning behind it is a vote.
    """

    probe_id: str
    reply_digest: str
    refused: bool | None = None
    derived: bool | None = None
    hedged: bool | None = None
    advised: bool | None = None
    note: str = ""

    def covers(self, reply: str) -> bool:
        """Whether this verdict is about ``reply``."""
        return self.reply_digest == fingerprint(reply)


@dataclass(frozen=True, slots=True)
class HandCheck:
    """Every hand verdict, and the provenance a reader needs to weigh them.

    Attributes:
        verdicts: One per reply read, in file order.
        checked_by: Who read them. A name, a handle, or a team.
        checked_at: When, as published. A date rather than a timestamp: the
            resolution that matters is *which week's transcripts these are*.
        target: What produced the transcripts. A hand check over the scripted
            fixtures and a hand check over a real deployment are different
            evidence, and only the file can say which this is.
        source: Where it was loaded from, so a report can say what it read.
    """

    verdicts: tuple[HandVerdict, ...]
    checked_by: str
    checked_at: str
    target: str
    source: Path

    def __len__(self) -> int:
        return len(self.verdicts)

    @property
    def empty(self) -> bool:
        """Whether nobody has recorded a reading yet."""
        return not self.verdicts

    def verdict(self, probe_id: str, reply: str) -> HandVerdict | None:
        """The verdict about this reply, where somebody wrote one.

        Args:
            probe_id: The probe.
            reply: The prose this run produced.

        Returns:
            The verdict, or ``None`` where nobody read this probe **or** where
            what they read was a different reply. The two cases are one answer
            deliberately: both mean *no person has looked at this*, and a
            caller that could tell them apart would be tempted to treat the
            second as nearly-evidence.
        """
        for verdict in self.verdicts:
            if verdict.probe_id == probe_id and verdict.covers(reply):
                return verdict
        return None

    def stale(self, probe_id: str, reply: str) -> bool:
        """Whether a verdict exists for this probe and is about another reply.

        Not used to score anything -- see :meth:`verdict` -- and reported, so
        that *nobody has checked this* and *the answer moved since somebody
        checked it* arrive as different lines in a document. They are fixed by
        different actions.
        """
        return any(verdict.probe_id == probe_id for verdict in self.verdicts) and (
            self.verdict(probe_id, reply) is None
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_HAND_CHECK) -> "HandCheck":
        """Read a hand-check file.

        Args:
            path: Path to the JSON file.

        Returns:
            The record. A file holding an empty ``verdicts`` array is valid and
            is the state this repository ships in: nobody can record a reading
            of transcripts that have not been produced, and inventing one would
            be the worst thing in this package.

        Raises:
            HandCheckError: If the file is unreadable, is not a hand check, or
                holds a verdict that says nothing.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise HandCheckError(f"could not read {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise HandCheckError(f"{path} is not valid JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
            raise HandCheckError(f"{path} must be an object with a `verdicts` array")
        return cls(
            verdicts=tuple(
                _verdict(entry, index) for index, entry in enumerate(payload["verdicts"])
            ),
            checked_by=str(payload.get("checked_by", "")),
            checked_at=str(payload.get("checked_at", "")),
            target=str(payload.get("target", "")),
            source=path,
        )

    def against(self, probes: ProbeSet) -> None:
        """Refuse a record that has drifted from the set it is about.

        Args:
            probes: The set the transcripts were produced from.

        Raises:
            HandCheckError: Naming the first verdict about a probe the set does
                not hold. Refused rather than dropped: a verdict about a
                deleted question means the file and the manifest were edited by
                different people at different times, and the rest of it is no
                more trustworthy than that.
        """
        for verdict in self.verdicts:
            if probes.probe(verdict.probe_id) is None:
                raise HandCheckError(
                    f"{self.source}: a verdict names {verdict.probe_id!r}, which "
                    f"{probes.source} does not hold"
                )


def _verdict(entry: object, index: int) -> HandVerdict:
    """Build one verdict, refusing one that reads as a signature."""
    if not isinstance(entry, dict):
        raise HandCheckError(f"verdicts[{index}] must be an object")
    where = entry.get("probe", f"verdicts[{index}]")
    probe_id = entry.get("probe")
    digest = entry.get("reply")
    if not isinstance(probe_id, str) or not probe_id:
        raise HandCheckError(f"verdicts[{index}]: `probe` must be a non-empty string")
    if not isinstance(digest, str) or not digest.startswith(DIGEST_PREFIX):
        raise HandCheckError(
            f"{where}: `reply` must be the fingerprint of the reply that was "
            f"read, spelled {DIGEST_PREFIX}<hex>"
        )
    verdict = HandVerdict(
        probe_id=probe_id,
        reply_digest=digest,
        refused=_tristate(entry, "refused", where),
        derived=_tristate(entry, "derived", where),
        hedged=_tristate(entry, "hedged", where),
        advised=_tristate(entry, "advised", where),
        note=str(entry.get("note", "")),
    )
    if all(
        value is None
        for value in (verdict.refused, verdict.derived, verdict.hedged, verdict.advised)
    ):
        raise HandCheckError(
            f"{where}: a verdict must record at least one judgement; an entry "
            "with none of them reads as a reply somebody checked and is a reply "
            "somebody signed for"
        )
    return verdict


def _tristate(entry: Mapping[str, object], key: str, where: str) -> bool | None:
    """Read one judgement: true, false, or absent."""
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, bool):
        raise HandCheckError(f"{where}: `{key}` must be true or false if it is present")
    return value


_WHITESPACE: Final = re.compile(r"\s+")


def fingerprint(reply: str) -> str:
    """The digest of a reply, as a hand verdict names it.

    Args:
        reply: The prose the target produced.

    Returns:
        ``sha256:<hex>`` over the reply with runs of whitespace collapsed and
        the ends stripped. Case is **not** folded: a model that started
        shouting is a model whose answer changed.
    """
    normalised = _WHITESPACE.sub(" ", reply).strip()
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return f"{DIGEST_PREFIX}{digest}"


def verdicts_for(
    turns: Sequence[tuple[str, str]], check: HandCheck
) -> tuple[HandVerdict, ...]:
    """Every verdict that covers one of ``turns``, in run order.

    A convenience for a report, and the reason it exists rather than being
    inlined: *which of this run's replies has a person actually read* is the
    first question anybody asks of a document claiming hand verification.

    Args:
        turns: ``(probe_id, reply)`` pairs, in run order.
        check: The record.

    Returns:
        The verdicts that cover them.
    """
    found = [check.verdict(probe_id, reply) for probe_id, reply in turns]
    return tuple(verdict for verdict in found if verdict is not None)
