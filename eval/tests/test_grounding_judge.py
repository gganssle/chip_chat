"""The judge behind #75's two judged findings, driven without a model.

Every test here scripts :class:`~chip_chat.agent.model.ChatModel` rather than
calling one, for the reason ``chip_chat.eval.photos.testing`` gives at greater
length: a test that called a real judge would measure the judge. What is under
test is the contract around it -- one word in, three outcomes out, an abstention
that is never a failure, and a token count that is never lost.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chip_chat.agent.model import ModelReply
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import Evidence, Passage
from chip_chat.eval.grounding.judge import JudgeSpend, ModelJudge
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.grounding.verdicts import Finding, Refusal, Verdict, assess


@dataclass
class ScriptedModel:
    """A chat model that answers from a list, and remembers what it was asked."""

    answers: list[str]
    deployment: str = "scripted-judge"
    seen: list[Sequence[Mapping[str, Any]]] = field(default_factory=list)
    raises: bool = False

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelReply:
        if self.raises:
            raise RuntimeError("the judge deployment refused the call")
        self.seen.append(messages)
        content = self.answers.pop(0) if self.answers else ""
        return ModelReply(content=content, prompt_tokens=100, completion_tokens=3)


QUESTION = Question(
    entry_id="golden/k1-bowl-ingredients",
    lane=Lane.KNOWLEDGE,
    answer_owed=True,
    citation_owed=True,
    message="what's actually in a burrito bowl",
    why="the plainest knowledge question there is",
)


def _turn(reply: str, *, passages: Sequence[Passage] = ()) -> Turn:
    return Turn(
        entry_id=QUESTION.entry_id,
        reply=reply,
        evidence=Evidence(
            entry_id=QUESTION.entry_id,
            passages=tuple(passages),
            searches=1,
            trace_ids=frozenset({"a" * 32}),
            roots=1,
        ),
        reports=frozenset({Signal.TOOLS}),
    )


PASSAGE = Passage(id="menu-bowl-0001", content="A burrito bowl starts with rice.")


def test_supported_is_true_and_unsupported_is_false() -> None:
    judge = ModelJudge(ScriptedModel(["SUPPORTED", "UNSUPPORTED"]))
    turn = _turn("It starts with rice.", passages=[PASSAGE])

    assert judge.grounded(QUESTION, turn) is True
    assert judge.grounded(QUESTION, turn) is False


def test_an_abstention_is_none_rather_than_a_failure() -> None:
    judge = ModelJudge(ScriptedModel(["UNSURE"]))

    assert judge.grounded(QUESTION, _turn("...", passages=[PASSAGE])) is None
    assert judge.spend.refusals == 1


def test_prose_instead_of_a_word_is_an_abstention() -> None:
    """A verdict extracted from a paragraph is a verdict a regex invented."""
    judge = ModelJudge(ScriptedModel(["Well, it depends on what you mean by..."]))

    assert judge.grounded(QUESTION, _turn("x", passages=[PASSAGE])) is None
    assert judge.spend.refusals == 1


def test_punctuation_around_the_word_is_tolerated() -> None:
    judge = ModelJudge(ScriptedModel(["**SUPPORTED.**"]))

    assert judge.grounded(QUESTION, _turn("x", passages=[PASSAGE])) is True


def test_a_failed_call_abstains_and_is_counted_apart_from_one_that_answered() -> None:
    judge = ModelJudge(ScriptedModel([], raises=True))

    assert judge.grounded(QUESTION, _turn("x", passages=[PASSAGE])) is None
    assert judge.spend.errors == 1
    assert judge.spend.refusals == 0
    assert judge.spend.calls == 0


def test_an_empty_reply_is_not_worth_a_round_trip() -> None:
    model = ScriptedModel(["SUPPORTED"])
    judge = ModelJudge(model)

    assert judge.grounded(QUESTION, _turn("   ", passages=[PASSAGE])) is None
    assert judge.refused(QUESTION, _turn("", passages=[PASSAGE])) is None
    assert model.seen == []


def test_the_groundedness_judge_is_shown_the_passages_and_not_the_register() -> None:
    """#75's sentence, as an assertion: the judge scores what the turn had."""
    model = ScriptedModel(["SUPPORTED"])
    ModelJudge(model).grounded(QUESTION, _turn("rice", passages=[PASSAGE]))

    prompt = str(model.seen[0][1]["content"])
    assert PASSAGE.id in prompt
    assert "A burrito bowl starts with rice." in prompt
    assert "answer_owed" not in prompt
    assert QUESTION.why not in prompt


def test_no_passages_is_said_in_words_rather_than_left_blank() -> None:
    model = ScriptedModel(["UNSUPPORTED"])
    ModelJudge(model).grounded(QUESTION, _turn("rice", passages=[]))

    assert "retrieved NO passages" in str(model.seen[0][1]["content"])


def test_the_refusal_judge_is_told_neither_the_direction_nor_the_evidence() -> None:
    """Handing it ``answer_owed`` would be handing it the answer."""
    model = ScriptedModel(["DECLINES"])
    ModelJudge(model).refused(QUESTION, _turn("I cannot say.", passages=[PASSAGE]))

    system = str(model.seen[0][0]["content"])
    prompt = str(model.seen[0][1]["content"])
    assert "not your question and you are not being told" in system
    assert PASSAGE.id not in prompt


def test_declines_and_answers_are_the_two_committed_verdicts() -> None:
    judge = ModelJudge(ScriptedModel(["DECLINES", "ANSWERS", "UNSURE"]))
    turn = _turn("something", passages=[PASSAGE])

    assert judge.refused(QUESTION, turn) is True
    assert judge.refused(QUESTION, turn) is False
    assert judge.refused(QUESTION, turn) is None


def test_spend_totals_across_judges_that_share_it() -> None:
    """#76 makes this a budget line, and a budget line has to total."""
    spend = JudgeSpend()
    first = ModelJudge(ScriptedModel(["SUPPORTED"]), spend=spend)
    second = ModelJudge(ScriptedModel(["ANSWERS"]), spend=spend)
    turn = _turn("x", passages=[PASSAGE])

    first.grounded(QUESTION, turn)
    second.refused(QUESTION, turn)

    assert spend.calls == 2
    assert spend.prompt_tokens == 200
    assert spend.completion_tokens == 6
    assert spend.total_tokens == 206
    assert "2 judge call(s), 206 tokens" in spend.summary()


def test_an_over_refusal_is_what_a_declining_judge_produces_on_an_answerable_row() -> (
    None
):
    """The whole point of #75's second direction, end to end through the verdicts."""
    judge = ModelJudge(ScriptedModel(["SUPPORTED", "DECLINES"]))

    judgement = assess(
        QUESTION, _turn("I'd check the website.", passages=[PASSAGE]), judge=judge
    )

    assert judgement.refusal is Refusal.OVER_REFUSAL


def test_a_judge_that_will_not_say_leaves_the_finding_unscored() -> None:
    judge = ModelJudge(ScriptedModel(["UNSURE", "UNSURE"]))

    judgement = assess(QUESTION, _turn("rice", passages=[PASSAGE]), judge=judge)

    assert judgement.verdicts[Finding.GROUNDED] is Verdict.UNSCORED
    assert judgement.refusal is Refusal.UNSCORED


def test_a_judge_that_commits_turns_the_finding_into_a_number() -> None:
    judge = ModelJudge(ScriptedModel(["UNSUPPORTED", "ANSWERS"]))

    judgement = assess(QUESTION, _turn("beef.", passages=[PASSAGE]), judge=judge)

    assert judgement.verdicts[Finding.GROUNDED] is Verdict.FAIL
    assert judgement.refusal is Refusal.CORRECT
