"""What the package promises about itself, and what it refuses to duplicate.

Two things are checked here and neither is about a score. The first is that the
public surface matches what is exported, because a name in ``__all__`` that does
not exist is a broken import in somebody else's module. The second is the reuse
argument: this eval reads a different set of spans out of the *same* tree #74
reads, and a second span adapter would be a second thing to keep in step with
the SDK.
"""

import chip_chat.eval.grounding as grounding
from chip_chat.eval.golden.scoring import GROUNDEDNESS_TARGET as GOLDEN_TARGET
from chip_chat.eval.grounding.evidence import TraceSpan as EvidenceSpan
from chip_chat.eval.grounding.scoring import GROUNDEDNESS_TARGET
from chip_chat.eval.trajectory.trees import TraceSpan


def test_everything_exported_exists() -> None:
    """``__all__`` is a promise, and this is the test that keeps it one."""
    for name in grounding.__all__:
        assert hasattr(grounding, name), name


def test_the_span_type_is_the_one_74_already_defined() -> None:
    """One adapter between a recording and a reader, not two.

    A copy here would drift the day the SDK changes what a ``ReadableSpan``
    looks like, and the two evals would then disagree about what a turn's trace
    said while both reading it.
    """
    assert EvidenceSpan is TraceSpan


def test_the_groundedness_target_is_the_golden_set_s() -> None:
    """PRD section 05 sets one number, so two modules must not name two.

    The golden set already declares it -- unscored there, because no judge is
    wired -- and this eval imports it rather than restating it, so the two
    reports cannot quote different bars for the same metric.
    """
    assert GROUNDEDNESS_TARGET is GOLDEN_TARGET
    assert GROUNDEDNESS_TARGET == 0.95
