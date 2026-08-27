"""A model behind :class:`~chip_chat.eval.grounding.run.Judge`, and what it costs.

:mod:`chip_chat.eval.grounding.run` names the two questions no data structure can
settle -- *is every food or policy claim in this reply supported by the passages
the turn actually retrieved*, and *did this reply decline* -- and deliberately
implements neither. This module implements both, against
:class:`~chip_chat.agent.model.ChatModel`, which is the same seam the agent loop
itself is written to. So a judge is a model deployment named in configuration,
never a client this module builds, and swapping the judge for a cheaper or a
stronger one is the same kind of change as swapping the agent's.

**The judge is handed the passages and nothing else.** Not the corpus, not the
expected answer, not the row's own register. #75 is specific about the first:
*the retrieved documents are on the ``retriever.search`` span, so the judge
scores against what the system really had, not against the corpus in general*.
The other two exclusions are the same argument one step further out. A judge
shown the golden set's expected behaviour is being asked to agree with a label
rather than to read a reply, and it will agree; a judge shown the row's
``declines`` flag before being asked whether the reply declined has been handed
the answer to the question it is being paid to settle.
:func:`~chip_chat.eval.grounding.verdicts._refusal` puts the direction back
afterwards, from the register, where the two facts cannot contaminate each other.

**Three verdicts, and the third one is why this is usable at all.** Each
question is asked for one of three words, and the abstention is a first-class
answer rather than a parsing failure. A judge that never abstains is a judge that
guesses, and a guess scored as a groundedness failure sends somebody to debug a
model that was right. So :meth:`ModelJudge.grounded` returns ``None`` wherever
the model would not commit, wherever the reply came back empty, and wherever the
call itself failed -- and :mod:`chip_chat.eval.grounding.verdicts` turns every
one of those into ``unscored``, which is neither pass nor fail.

**Every call is counted, because #76 makes the count a budget line.** Judging is
inference and inference is spend; an online eval sampling live traffic is a
second, continuous model bill sitting outside the request path that
:mod:`chip_chat.api` meters. :class:`JudgeSpend` is the accounting, it rides on
the judge rather than beside it, and
:mod:`chip_chat.eval.online.budget` is what reads it. A judge whose cost is not
observable is a hole in the daily cap, and the hole is exactly the size nobody
measured.

**Determinism, as far as a model allows.** Temperature is not set here: the
deployments this project runs against reject a non-default temperature outright,
and pretending to have pinned one would be worse than saying so. What *is* pinned
is everything else -- one call per question, no history between rows, a fixed
instruction, and a reply constrained to one word from a closed set. Two runs of
this judge over the same turns will not agree perfectly, and
``eval/grounding/BASELINE.md`` records which judge produced its numbers so that
two documents can at least be read as having been scored by the same thing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from chip_chat.agent.model import ChatModel
from chip_chat.eval.grounding.evidence import Passage
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn

__all__ = [
    "GROUNDED_INSTRUCTION",
    "REFUSAL_INSTRUCTION",
    "JudgeSpend",
    "ModelJudge",
]

_PASSAGE_CHARS = 1_200
"""How much of one passage the judge is shown.

The harvested chunks are a few hundred words; this truncates the outliers rather
than the ordinary case, and it exists so that a turn which retrieved twelve
passages cannot quietly cost twenty times what a turn which retrieved one did.
A judge is billed per token like everything else.
"""

_MAX_PASSAGES = 12
"""How many passages reach the judge, in span order.

The same argument. A turn that retrieved more than twelve passages has a
retrieval problem that :mod:`chip_chat.eval.retrieval` is the place to find, and
handing all of them to a judge would spend tokens confirming it.
"""

GROUNDED_INSTRUCTION = """\
You are grading one reply from a restaurant assistant.

You will be given the visitor's question, the passages the assistant retrieved \
on that turn, and the reply it wrote. Decide whether every factual claim in the \
reply about food, ingredients, allergens, nutrition, prices or store policy is \
supported by those passages.

Rules:
- Judge ONLY against the passages below. If a claim is true in the world but the \
passages do not say it, it is not supported.
- Ignore claims about the visitor's own account, points, order history or \
preferences. Those come from elsewhere and are not what you are grading.
- A reply that declines to answer, or that says the published information does \
not cover the question, makes no unsupported claim and is SUPPORTED.
- Hedged phrasing does not excuse an unsupported specific.

Answer with exactly one word:
SUPPORTED   -- every food or policy claim is supported by the passages.
UNSUPPORTED -- at least one is not.
UNSURE      -- you genuinely cannot tell from what you were given.\
"""
"""What the groundedness judge is told. Never the row's expected behaviour."""

REFUSAL_INSTRUCTION = """\
You are reading one reply from a restaurant assistant.

Decide whether the reply DECLINES -- that is, whether it withholds the answer to \
what was asked. A reply declines when it says it does not know, says the \
published information does not cover the question, redirects the visitor to staff \
or to a website instead of answering, or answers a different question while \
leaving the one asked unanswered.

A reply that answers the question does not decline, even if it is hedged, \
partial, or wrong. Whether declining was the RIGHT thing to do is not your \
question and you are not being told.

Answer with exactly one word:
DECLINES -- the reply withholds the answer.
ANSWERS  -- the reply gives one.
UNSURE   -- you genuinely cannot tell.\
"""
"""What the refusal judge is told. The direction is deliberately withheld."""

_GROUNDED_WORDS: Mapping[str, bool] = {"supported": True, "unsupported": False}
_REFUSAL_WORDS: Mapping[str, bool] = {"declines": True, "answers": False}


@dataclass(slots=True)
class JudgeSpend:
    """What the judging cost, in the units a budget is kept in.

    Mutable on purpose, and the one mutable thing in this package. A judge is
    called once per finding per row and the total is only known at the end, so
    the alternative to accumulating here is returning a cost beside every
    verdict and threading it back through
    :func:`~chip_chat.eval.grounding.scoring.score`, which would put spend
    accounting in the arithmetic of a metric.

    Attributes:
        calls: Round trips made. The count that matters for a rate limit.
        refusals: Round trips that came back ``UNSURE`` or unparseable. Paid
            for and bought nothing, so worth seeing apart -- a judge abstaining
            on half the set is a prompt problem rather than a product one.
        errors: Round trips that raised. Also paid for, sometimes.
        prompt_tokens: Tokens sent.
        completion_tokens: Tokens returned.
    """

    calls: int = 0
    refusals: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Every token this judge has been billed for."""
        return self.prompt_tokens + self.completion_tokens

    def record(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        """Add one round trip's cost."""
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def summary(self) -> str:
        """One line, for the foot of a report."""
        return (
            f"{self.calls} judge call(s), {self.total_tokens} tokens "
            f"({self.prompt_tokens} in / {self.completion_tokens} out); "
            f"{self.refusals} abstention(s), {self.errors} error(s)"
        )


@dataclass(frozen=True, slots=True)
class ModelJudge:
    """A chat deployment, wearing :class:`~chip_chat.eval.grounding.run.Judge`.

    Attributes:
        model: The deployment that judges. Named in configuration and swapped
            there; nothing in this class knows which model it is.
        spend: Where the token cost accumulates. Shared deliberately -- pass one
            :class:`JudgeSpend` to several judges and it totals across them,
            which is what an online runner scoring three findings wants.
    """

    model: ChatModel
    spend: JudgeSpend = field(default_factory=JudgeSpend)

    @property
    def name(self) -> str:
        """What to call this judge in a report."""
        return f"{self.model.deployment} as judge"

    def grounded(self, question: Question, turn: Turn) -> bool | None:
        """Whether the reply's food and policy claims survive its own passages.

        Args:
            question: The row, for the visitor's words. Its register --
                ``answer_owed``, ``refusal_owed``, ``citation_owed`` -- is not
                shown to the model; see the module docstring.
            turn: What came back, including the evidence read off the
                ``retriever.search`` spans.

        Returns:
            ``True`` where every claim is supported by the passages the turn
            retrieved, ``False`` where one is not, and ``None`` where the model
            abstained, the reply was empty, or the call failed.
        """
        if not turn.reply.strip():
            return None
        evidence = turn.evidence
        passages = () if evidence is None else evidence.passages
        prompt = (
            f"Visitor asked: {question.message}\n\n"
            f"{_passages(passages)}\n\n"
            f"The assistant replied:\n{turn.reply.strip()}"
        )
        return self._ask(GROUNDED_INSTRUCTION, prompt, _GROUNDED_WORDS)

    def refused(self, question: Question, turn: Turn) -> bool | None:
        """Whether the reply declined, without being told which way is right.

        Args:
            question: The row, for the visitor's words only.
            turn: What came back. The evidence is *not* shown: whether a reply
                declines is a property of the prose, and a model shown the
                passages would start grading the answer instead of reading it.

        Returns:
            ``True`` where the reply withholds an answer, ``False`` where it
            gives one, and ``None`` where the model abstained, the reply was
            empty, or the call failed.
        """
        if not turn.reply.strip():
            return None
        prompt = (
            f"Visitor asked: {question.message}\n\n"
            f"The assistant replied:\n{turn.reply.strip()}"
        )
        return self._ask(REFUSAL_INSTRUCTION, prompt, _REFUSAL_WORDS)

    def _ask(
        self, instruction: str, prompt: str, words: Mapping[str, bool]
    ) -> bool | None:
        """One round trip, and the three ways it can come back.

        Broad in what it catches for the reason
        :func:`chip_chat.eval.grounding.run._run_one` is: a judge is a network
        and somebody else's service, and a judge that raises through the scorer
        would cost every remaining row. A failed call is an abstention that was
        paid for, which is why :attr:`JudgeSpend.errors` is a separate count
        from :attr:`JudgeSpend.refusals`.
        """
        try:
            reply = self.model.complete(
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": prompt},
                ]
            )
        except Exception:  # a judge is somebody else's service; see the docstring
            self.spend.errors += 1
            return None
        self.spend.record(
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
        verdict = words.get(_word(reply.content))
        if verdict is None:
            self.spend.refusals += 1
        return verdict


def _word(content: str | None) -> str:
    """The first bare word of a reply, lowercased.

    Tolerant of a model that answers ``"SUPPORTED."`` or wraps its word in
    punctuation, and deliberately intolerant of one that writes a paragraph: a
    verdict that has to be extracted from prose is a verdict somebody's regular
    expression invented.
    """
    if not content:
        return ""
    first = content.strip().split()
    if not first:
        return ""
    return first[0].strip(".,:;!*_`\"'").lower()


def _passages(passages: Sequence[Passage]) -> str:
    """The retrieved passages, as the judge sees them.

    Ids are shown. They are not decoration: a judge that can name the passage a
    claim came from is a judge whose ``UNSUPPORTED`` can be argued with, and the
    id is the same one a citation would carry, per D9.
    """
    if not passages:
        return (
            "The assistant retrieved NO passages on this turn. Any claim about "
            "food or policy is therefore unsupported."
        )
    lines = [f"The assistant retrieved {len(passages)} passage(s):"]
    for index, passage in enumerate(passages[:_MAX_PASSAGES], start=1):
        content = passage.content.strip().replace("\n", " ")
        if len(content) > _PASSAGE_CHARS:
            content = content[:_PASSAGE_CHARS] + " ..."
        lines.append(f"\n[{index}] id={passage.id}\n{content}")
    if len(passages) > _MAX_PASSAGES:
        lines.append(f"\n({len(passages) - _MAX_PASSAGES} further passage(s) not shown.)")
    return "\n".join(lines)
