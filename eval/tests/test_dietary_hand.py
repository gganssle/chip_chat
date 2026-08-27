"""That a person's reading is evidence about a reply, and stops being so when it moves.

#84 asks for the derivation to be verified *by hand, not only by a judge*, which
puts a human verdict inside a scoring path. The failure mode that introduces is
specific and quiet: a verdict recorded in March about an answer the model no
longer gives, still counting in August, still holding the launch gate open.

So every test here is about the expiry rather than about the verdict.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.dietary.hand import (
    HandCheck,
    HandCheckError,
    HandVerdict,
    fingerprint,
)
from chip_chat.eval.dietary.probes import ProbeSet

_REPLY = "The published chart marks Cheese with dairy. I can't say whether it is safe."


def _record(tmp_path: Path, *verdicts: dict) -> HandCheck:
    path = tmp_path / "hand-check.json"
    path.write_text(
        json.dumps(
            {
                "checked_by": "a test",
                "checked_at": "2026-08-27",
                "target": "a fixture",
                "verdicts": list(verdicts),
            }
        ),
        encoding="utf-8",
    )
    return HandCheck.load(path)


def test_the_shipped_record_loads_and_is_empty(hand: HandCheck) -> None:
    """Empty is the honest state, and the report says so above its own numbers.

    A verdict is one person's reading of one reply. No deployment in this
    repository has produced replies to read, so there is nothing to record --
    and inventing one would be the worst thing in the package.
    """
    assert hand.empty


def test_the_shipped_record_is_about_the_shipped_set(
    hand: HandCheck, probes: ProbeSet
) -> None:
    """A verdict about a deleted question is indistinguishable from coverage."""
    hand.against(probes)


def test_a_verdict_covers_the_reply_it_was_written_about(tmp_path: Path) -> None:
    """The ordinary case: somebody read this answer, and this is what they said."""
    check = _record(
        tmp_path,
        {"probe": "a-probe", "reply": fingerprint(_REPLY), "derived": False},
    )
    verdict = check.verdict("a-probe", _REPLY)
    assert verdict is not None
    assert verdict.derived is False


def test_a_verdict_does_not_cover_a_reply_that_has_changed(tmp_path: Path) -> None:
    """The whole mechanism. A reading of a reply nobody got is not evidence."""
    check = _record(
        tmp_path,
        {"probe": "a-probe", "reply": fingerprint(_REPLY), "derived": False},
    )
    assert check.verdict("a-probe", _REPLY + " Also, it is dairy-free.") is None


def test_a_changed_reply_is_reported_as_stale_rather_than_as_unread(
    tmp_path: Path,
) -> None:
    """Two different things to do about it, so two different lines in the report."""
    check = _record(
        tmp_path,
        {"probe": "a-probe", "reply": fingerprint(_REPLY), "derived": False},
    )
    assert check.stale("a-probe", "something else") is True
    assert check.stale("never-read", "something else") is False


def test_whitespace_is_not_a_change_and_a_word_is(tmp_path: Path) -> None:
    """A renderer that reflows a paragraph has not changed what anybody read."""
    assert fingerprint("  one   two \n three ") == fingerprint("one two three")
    assert fingerprint("one two three") != fingerprint("one two four")


def test_a_verdict_recording_nothing_is_refused(tmp_path: Path) -> None:
    """An entry with no judgement in it is a signature, not a reading."""
    with pytest.raises(HandCheckError, match="somebody signed for"):
        _record(tmp_path, {"probe": "a-probe", "reply": fingerprint(_REPLY)})


def test_a_verdict_without_a_fingerprint_is_refused(tmp_path: Path) -> None:
    """Without one there is nothing to expire, which is the same as never expiring."""
    with pytest.raises(HandCheckError, match="fingerprint"):
        _record(tmp_path, {"probe": "a-probe", "reply": "I read it", "derived": False})


def test_a_verdict_about_a_probe_the_set_lost_is_refused(
    tmp_path: Path, probes: ProbeSet
) -> None:
    """Refused rather than dropped: the two files were edited by different hands."""
    check = _record(
        tmp_path,
        {
            "probe": "a-question-nobody-asks",
            "reply": fingerprint(_REPLY),
            "derived": False,
        },
    )
    with pytest.raises(HandCheckError, match="does not hold"):
        check.against(probes)


def test_a_judgement_left_out_stays_unrecorded(tmp_path: Path) -> None:
    """Silence is not absence -- the rule the photo set applies to an unread slot."""
    check = _record(
        tmp_path,
        {"probe": "a-probe", "reply": fingerprint(_REPLY), "derived": False},
    )
    verdict = check.verdict("a-probe", _REPLY)
    assert isinstance(verdict, HandVerdict)
    assert verdict.hedged is None
    assert verdict.advised is None
