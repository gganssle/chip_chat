"""Five findings about one turn, and why each is counted apart from the others.

Issue #75 puts two metrics in the same ticket and they are not the same kind of
thing. *Groundedness* is a judgement about meaning with a target of 0.95.
*A menu claim with no citation* is a rule with a target of zero, and D9 is what
made it a rule: a citation is an id the retriever returned, so its absence is a
fact about a payload rather than an opinion about prose. Averaging them together
would produce one number that could not be acted on.

So a turn gets five findings, each with its own owner:

``cited``
    The response made a claim PRD K2 requires a citation on, and carried none.
    Deterministic, and the rule is
    :attr:`~chip_chat.agent.envelope.ResponseEnvelope.uncited_claim`'s -- read
    from there rather than restated, because a copy here could disagree with the
    renderer.

``minted``
    The model named a passage id the retriever did not return.
    :attr:`~chip_chat.agent.envelope.ResponseEnvelope.dropped_citation_ids` says
    in as many words that this is what #75 counts. It is invisible in the
    citations array, because :func:`~chip_chat.agent.envelope.render` has
    already dropped it -- a source that was refused rather than shown, which is
    the design working and still worth knowing about.

``supported``
    The turn made a claim that had to be grounded, and its
    ``retriever.search`` spans returned nothing. Deterministic, needs no judge,
    and it is the floor under groundedness: a claim on a turn that retrieved
    nothing is ungrounded whatever any judge says about the prose. This is the
    ``no_tool`` shape of :mod:`chip_chat.eval.trajectory.shapes` seen from the
    other side -- *the prose reads fine and nothing in it is attached to
    anything* -- and it is the one number in this module that a run produces
    today.

``grounded``
    Every food or policy claim in the reply is supported by the passages the
    turn actually retrieved. Judged, and unscored without a judge. It is also
    unscored where the evidence cannot be read: #75 asks for the judge to score
    against *what the system really had*, and a judge handed the corpus instead
    would score a system that never opened it as grounded.

``refusal``
    Both directions at once. A refusal where the corpus plainly had the answer
    is an **over-refusal**, and measuring only the other direction produces a
    system that hedges everything and scores beautifully -- #75 says so, and it
    is the reason :class:`Refusal` has four outcomes rather than a boolean.

**There are four verdicts, not three.** Every other scorer in ``eval/`` has
pass, fail and unscored, and unscored carries two different failures at once
here: *the deployment cannot report this* and *the set says nothing about this
row*. They are fixed by different people -- one is
``chip_chat.agent.envelope``'s missing caller (bead ``cc-bap``), the other is a
case somebody has to write -- so :attr:`Verdict.NOT_ASKED` is a fourth value and
neither number can hide inside the other.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from chip_chat.eval.grounding.evidence import Evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Judge, Turn

__all__ = ["FINDINGS", "Finding", "Judgement", "Refusal", "Verdict", "assess"]

_NOTHING_CAME_BACK = "nothing came back for this row; the source recorded an error"


class Verdict(StrEnum):
    """What became of one finding.

    Attributes:
        PASS: Asked, observed, and satisfied.
        FAIL: Asked, observed, and not satisfied.
        UNSCORED: Asked, and nothing could be observed. The source does not
            report what the finding needs, or the finding needs a judge and none
            was supplied. Never a pass, never a failure.
        NOT_ASKED: This row is not evidence about this finding. Groundedness
            over a turn that made no food or policy claim is not a hard case;
            it is not a case. See the module docstring for why this is a value
            of its own rather than a third meaning for ``UNSCORED``.
    """

    PASS = "pass"
    FAIL = "fail"
    UNSCORED = "unscored"
    NOT_ASKED = "not_asked"


class Finding(StrEnum):
    """The four findings that carry a :class:`Verdict`. See the module docstring.

    :class:`Refusal` is the fifth and is not here: it has four outcomes rather
    than a verdict, because *refused where it should have answered* and
    *answered where it should have refused* are two failures and one of them is
    invisible to any scorer that only counts wrong answers.
    """

    CITED = "cited"
    MINTED = "minted"
    SUPPORTED = "supported"
    GROUNDED = "grounded"


FINDINGS: tuple[Finding, ...] = tuple(Finding)
"""Every finding, in the order a report asks them: the rules before the judgement."""


class Refusal(StrEnum):
    """Which way round the turn got the refusal, if either.

    Attributes:
        CORRECT: It answered a question the published data answers, or declined
            one it does not.
        OVER_REFUSAL: It declined where the corpus plainly had the answer. The
            failure that a scorer measuring only groundedness rewards.
        UNDER_REFUSAL: It answered where the corpus does not support one. The
            failure PRD section 10 makes launch-blocking for allergen questions.
        UNSCORED: No judge, or nothing came back. Whether a reply *declines* is
            a property of prose and no data structure settles it; a keyword rule
            looking for "I don't know" would produce a number measuring the
            keyword rule.
        NOT_ASKED: The row says neither that the corpus answers this nor that it
            does not, so neither direction is a mistake it could have made.
    """

    CORRECT = "correct"
    OVER_REFUSAL = "over_refusal"
    UNDER_REFUSAL = "under_refusal"
    UNSCORED = "unscored"
    NOT_ASKED = "not_asked"


@dataclass(frozen=True, slots=True)
class Judgement:
    """One row, one turn, five findings.

    Attributes:
        question: What was asked, and what the set says is owed.
        turn: What came back, and what its trace says it had.
        verdicts: One per :class:`Finding`, in :data:`FINDINGS` order.
        refusal: Which way round the refusal went.
        details: One line per finding with something to say, keyed the same way
            as :attr:`verdicts`. A plain pass has no line. The refusal's line is
            :attr:`refusal_detail` instead, because its key is not a
            :class:`Finding`.
        refusal_detail: One line about the refusal, or empty where there is
            nothing to say.
        minted_ids: The passage ids the model named and the retriever never
            returned, in the order the response named them.
    """

    question: Question
    turn: Turn
    verdicts: Mapping[Finding, Verdict]
    refusal: Refusal
    details: Mapping[Finding, str] = field(default_factory=dict)
    refusal_detail: str = ""
    minted_ids: tuple[str, ...] = ()

    @property
    def dietary(self) -> bool:
        """Whether this row is in #75's stricter category."""
        return self.question.dietary

    @property
    def evidence(self) -> Evidence | None:
        """The turn's retrieval, where the source read one."""
        return self.turn.evidence

    @property
    def failed(self) -> tuple[Finding, ...]:
        """Every finding that failed, in :data:`FINDINGS` order."""
        return tuple(
            finding for finding in FINDINGS if self.verdicts[finding] is Verdict.FAIL
        )

    @property
    def breached(self) -> bool:
        """Whether anything about this turn was wrong.

        A refusal in either direction counts. Over-refusal is a failure of the
        product even though nothing it said was untrue, which is the whole
        argument for measuring it.
        """
        return bool(self.failed) or self.refusal in (
            Refusal.OVER_REFUSAL,
            Refusal.UNDER_REFUSAL,
        )

    def lines(self) -> tuple[str, ...]:
        """Everything that went wrong on this turn, one line each."""
        found = [
            self.details[finding] for finding in self.failed if finding in self.details
        ]
        if self.refusal in (Refusal.OVER_REFUSAL, Refusal.UNDER_REFUSAL):
            found.append(self.refusal_detail)
        return tuple(line for line in found if line)


def assess(question: Question, turn: Turn, *, judge: Judge | None = None) -> Judgement:
    """Give one turn its five findings.

    Args:
        question: What the dataset row is owed.
        turn: What came back.
        judge: Settles ``grounded`` and the refusal. ``None`` leaves both
            unscored, which is the state #75 ships in and which the report says
            on every run.

    Returns:
        The judgement.
    """
    verdicts: dict[Finding, Verdict] = {}
    details: dict[Finding, str] = {}
    cited, cited_line = _cited(question, turn)
    minted, minted_line = _minted(turn)
    supported, supported_line = _supported(question, turn)
    grounded, grounded_line = _grounded(question, turn, judge)
    refusal, refusal_line = _refusal(question, turn, judge)
    for finding, verdict, line in (
        (Finding.CITED, cited, cited_line),
        (Finding.MINTED, minted, minted_line),
        (Finding.SUPPORTED, supported, supported_line),
        (Finding.GROUNDED, grounded, grounded_line),
    ):
        verdicts[finding] = verdict
        if line:
            details[finding] = line
    return Judgement(
        question=question,
        turn=turn,
        verdicts=verdicts,
        refusal=refusal,
        details=details,
        refusal_detail=refusal_line,
        minted_ids=turn.dropped_citations if turn.reports_citations else (),
    )


def _cited(question: Question, turn: Turn) -> tuple[Verdict, str]:
    """PRD K2, as the rule D9 turned it into.

    Two ways a citation can be owed, and both are read. The *response* can owe
    one, by declaring a food, policy or allergen claim; and the *row* can owe
    one, because the set says this question's answer has to show what it read.
    The second matters on a turn that made its claim in prose without declaring
    a class -- the set has already said a citation is owed there, and a rule
    that consulted only the declaration would let that turn through.
    """
    if turn.error is not None:
        return Verdict.UNSCORED, _NOTHING_CAME_BACK
    if not turn.reports_citations:
        return (
            Verdict.UNSCORED,
            "the source does not report citations; chip_chat.agent.envelope is "
            "imported by no caller (cc-bap)",
        )
    owed = turn.claims_needing_citation or question.citation_owed
    if not owed:
        return Verdict.NOT_ASKED, ""
    if turn.citations:
        return Verdict.PASS, ""
    declared = "" if turn.declared_class is None else f" ({turn.declared_class.value})"
    return (
        Verdict.FAIL,
        f"a claim requiring a citation{declared} carried none; PRD K2's target "
        "for this is zero",
    )


def _minted(turn: Turn) -> tuple[Verdict, str]:
    """Ids the model named that the retriever never returned."""
    if turn.error is not None:
        return Verdict.UNSCORED, _NOTHING_CAME_BACK
    if not turn.reports_citations:
        return Verdict.UNSCORED, "the source does not report citations (cc-bap)"
    if not turn.dropped_citations:
        return Verdict.PASS, ""
    named = ", ".join(turn.dropped_citations)
    return (
        Verdict.FAIL,
        f"named {named}, which the retriever did not return on this turn; the "
        "renderer dropped them rather than showing a source that does not exist",
    )


def _supported(question: Question, turn: Turn) -> tuple[Verdict, str]:
    """The floor under groundedness, and the only one a run produces for free.

    Asked wherever a citation or a grounded answer was owed -- the same rows
    :func:`_grounded` is asked on, because a citation cannot exist without a
    passage to resolve against either. A refusal is not an exception: the one
    row that owes both is the one that has to *show what it read*.

    Whether the row is asked at all is settled before whether anything came
    back, so that a source outage does not quietly enlarge the denominator: a
    row that owed no grounded claim is *not asked* whether or not the turn ran.
    """
    if not (question.scores_grounding or turn.claims_needing_citation):
        return Verdict.NOT_ASKED, ""
    if turn.error is not None:
        return Verdict.UNSCORED, _NOTHING_CAME_BACK
    evidence = turn.evidence
    if evidence is None:
        return (
            Verdict.UNSCORED,
            "the source does not read span trees, so what the turn retrieved is "
            "not observable",
        )
    unreadable = evidence.unreadable_because
    if unreadable is not None:
        return Verdict.UNSCORED, unreadable
    if evidence.retrieved:
        return Verdict.PASS, ""
    if evidence.failed_searches:
        # RFC-001 section 10's outage path. The lane declining is not the
        # corpus being empty and is not the model answering from nothing;
        # putting it in the same column would send somebody to read a prompt.
        return (
            Verdict.UNSCORED,
            f"{evidence.failed_searches} search(es) declined; the knowledge lane "
            "was unavailable, which is an outage rather than an ungrounded claim",
        )
    searched = (
        "made no retrieval at all"
        if not evidence.searches
        else (f"retrieved nothing across {evidence.searches} search(es)")
    )
    return (
        Verdict.FAIL,
        f"a claim that had to be grounded, and the turn {searched}",
    )


def _grounded(question: Question, turn: Turn, judge: Judge | None) -> tuple[Verdict, str]:
    """The PRD's groundedness target, judged against what the turn really had.

    The denominator is *turns that made a food or policy claim*, and the only
    thing that names those is the register plus whatever class the response
    declared. So askability is settled from those two before the turn's outcome
    is consulted, for the reason :func:`_supported` gives.
    """
    if not (question.scores_grounding or turn.claims_needing_citation):
        return Verdict.NOT_ASKED, ""
    if turn.error is not None:
        return Verdict.UNSCORED, _NOTHING_CAME_BACK
    if judge is None:
        return Verdict.UNSCORED, "no judge was supplied"
    evidence = turn.evidence
    if evidence is None or not evidence.readable:
        # #75: the judge scores against what the system really had. Without the
        # retrieval there is nothing to score against but the corpus in
        # general, and that is a different and much easier question.
        return (
            Verdict.UNSCORED,
            "the turn's retrieval could not be read, so there is nothing to "
            "score the claims against",
        )
    verdict = judge.grounded(question, turn)
    if verdict is None:
        return Verdict.UNSCORED, "the judge would not say"
    if verdict:
        return Verdict.PASS, ""
    return Verdict.FAIL, "a food or policy claim the retrieved passages do not support"


def _refusal(question: Question, turn: Turn, judge: Judge | None) -> tuple[Refusal, str]:
    """Both directions, from one judgement about the prose and one fact about the row."""
    if not question.scores_refusal:
        return Refusal.NOT_ASKED, ""
    if turn.error is not None:
        return Refusal.UNSCORED, _NOTHING_CAME_BACK
    if judge is None:
        return Refusal.UNSCORED, "no judge was supplied"
    declined = judge.refused(question, turn)
    if declined is None:
        return Refusal.UNSCORED, "the judge would not say whether the reply declined"
    if declined and question.answer_owed:
        return (
            Refusal.OVER_REFUSAL,
            "declined a question the published data answers; a system that "
            "hedges everything scores beautifully on groundedness",
        )
    if not declined and question.refusal_owed:
        return (
            Refusal.UNDER_REFUSAL,
            "answered a question the published data does not support",
        )
    return Refusal.CORRECT, ""
