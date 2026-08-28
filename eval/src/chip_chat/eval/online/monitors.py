"""The five things #76 is actually afraid of, each as a condition that can fire.

The ticket lists them by name and the list is not generic. It is what this
system, with this design, gets wrong in a way that matters:

1. **An ungrounded menu claim.** The headline failure, and the one PRD K2 sets
   at zero.
2. **A photo match with no confident SKU** -- the matcher escalating, or worse,
   *not* escalating when it should. PRD V5: ask, do not guess.
3. **A refusal where the corpus plainly had the answer.** #76 says *easy to
   forget, and the failure mode a cautious system drifts toward*, and it is the
   only monitor here whose condition is the product being too careful.
4. **A cross-visitor disclosure signal of any kind, which should be impossible
   and therefore alarming.** Launch gate one.
5. **Latency and cost per conversation breaching their targets.**

**Three of the five need no judge, and that is a design choice rather than a
convenience.** Monitors 2, 4 and 5 are properties of the spans: the matcher
records what it resolved and whether it escalated, an identifier belonging to
somebody else is a fact about an attribute, and tokens and duration are numbers.
They therefore run on **every** turn, not on the sampled fraction, which is what
makes launch gate one meaningful in production -- a disclosure monitor that ran
on a fifth of traffic would miss four disclosures in five. Monitor 1 has a
deterministic half that runs on everything (*a claim with nothing retrieved*) and
a judged half that runs on the sample. Monitor 3 is judged, because *did this
reply decline* is a property of prose.

**Severity is a routing decision, not an adjective.** :class:`Severity` decides
what reaches a human and how fast, and the mapping is stated here rather than in
whoever wires the alert: a disclosure signal pages, a gate breach opens an issue,
everything else accumulates on a dashboard. A monitor whose severity nobody chose
is a monitor that fires into a log file.

**Every monitor is demonstrated by producing its condition.** #76's second
acceptance criterion is *each monitor tested by producing the condition
deliberately*, and :mod:`chip_chat.eval.online.testing` is where each condition
is built by hand -- a turn that claims with nothing retrieved, a photo match that
resolved nothing and did not escalate, a reply that declines with six passages in
hand, a span carrying another visitor's identifier, a turn over both ceilings.
``make online-drill`` runs them and prints which monitor caught which, which is
the difference between a monitor that exists and a monitor that works.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.eval.online.signals import LiveTurn

__all__ = [
    "COST_TOKEN_CEILING",
    "LATENCY_CEILING_MS",
    "MONITORS",
    "Alert",
    "Monitor",
    "Severity",
    "evaluate",
    "monitor",
]

LATENCY_CEILING_MS: Final = 6_000.0
"""PRD §05's latency target for a turn, in milliseconds.

Six seconds, which is the number the PRD sets for a conversational turn end to
end. A monitor on a target somebody invented here would fire on a product that
is meeting its published bar, so the number comes from the document and the
document is where to argue with it.
"""

COST_TOKEN_CEILING: Final = 30_000
"""Tokens in one turn, above which the cost-per-conversation target is at risk.

PRD §05 sets cost per *conversation* under five cents. A conversation is four or
five turns, and at the chat deployment's rate five cents is on the order of a
hundred and fifty thousand tokens, so thirty thousand in a single turn is the
point at which one turn is spending a whole conversation's budget. It is a
leading indicator rather than the metric: the metric is a division somebody does
on a cost dashboard, and by the time it moves the money is spent.
"""


class Severity(StrEnum):
    """What a finding is worth waking somebody for.

    Attributes:
        PAGE: Reaches a human immediately. Reserved for the disclosure signal,
            which should be impossible.
        TICKET: Opens an issue the same day. A launch gate breached.
        DASHBOARD: Accumulates and is read. A quality or cost signal that means
            something as a rate rather than as an instance.
    """

    PAGE = "page"
    TICKET = "ticket"
    DASHBOARD = "dashboard"


@dataclass(frozen=True, slots=True)
class Alert:
    """One monitor firing on one turn.

    Attributes:
        monitor: Which monitor.
        severity: How it is routed.
        trace_id: The trace, so a human can open it.
        detail: What was seen, in one line. Never the visitor's prose: an alert
            is read in a channel, and a channel is not where a stranger's
            question belongs.
        judged: Whether a model was involved in the finding.
    """

    monitor: str
    severity: Severity
    trace_id: str
    detail: str
    judged: bool = False


@dataclass(frozen=True, slots=True)
class Monitor:
    """One condition, and what firing on it costs.

    Attributes:
        name: What it is called, in an alert and on a dashboard.
        severity: How a finding routes.
        fear: The sentence from #76 this monitor is the implementation of.
        judged: Whether it needs a judge, and therefore whether it runs on the
            sampled fraction or on everything.
        escalates: Whether this monitor firing is a reason to spend a judge on
            the turn. True for every monitor except the budget breach, and the
            exception was found in production rather than reasoned out in
            advance — see :data:`BUDGET_BREACH`.
    """

    name: str
    severity: Severity
    fear: str
    judged: bool = False
    escalates: bool = True


UNGROUNDED_CLAIM = Monitor(
    name="ungrounded_menu_claim",
    severity=Severity.TICKET,
    fear="An ungrounded menu claim.",
)
UNGROUNDED_CLAIM_JUDGED = Monitor(
    name="ungrounded_menu_claim_judged",
    severity=Severity.TICKET,
    fear="An ungrounded menu claim, where the passages do not support it.",
    judged=True,
)
PHOTO_WITHOUT_SKU = Monitor(
    name="photo_match_without_confident_sku",
    severity=Severity.DASHBOARD,
    fear="A photo match with no confident SKU — the matcher escalating or, "
    "worse, not escalating when it should.",
)
REFUSAL_WITH_EVIDENCE = Monitor(
    name="refusal_where_the_corpus_answered",
    severity=Severity.DASHBOARD,
    fear="A refusal where the corpus plainly had the answer.",
    judged=True,
)
CROSS_VISITOR = Monitor(
    name="cross_visitor_disclosure",
    severity=Severity.PAGE,
    fear="A cross-visitor disclosure signal of any kind, which should be "
    "impossible and therefore alarming.",
)
BUDGET_BREACH = Monitor(
    name="latency_or_cost_breach",
    severity=Severity.DASHBOARD,
    fear="Latency and cost per conversation breaching their targets.",
    # The one monitor that does not escalate a turn to a judge, and the reason
    # is a measurement rather than an opinion.
    #
    # `escalates` exists because of what the first run against real production
    # traces showed. Three turns went through the deployed app; all three
    # breached the six-second latency target -- by 11, 23 and 52 seconds -- and
    # since a turn any deterministic monitor fired on is always judged, all
    # three were judged. The sampling rate was 20% and the realised rate was
    # 100%, which is not a rate that drifted: it is a rate that had been
    # switched off by a monitor that fires on every turn. The budget line then
    # reports the judges at 5% of the daily ceiling while the loop is actually
    # spending five times that.
    #
    # The narrowing is principled rather than a fudge, which is why it is a
    # field on the monitor and not a threshold somebody raised until the noise
    # stopped. The judge answers exactly two questions -- is this claim
    # supported by what the turn retrieved, and did the reply decline -- and
    # neither of them is a thing you learn about a slow turn. A latency breach
    # is a real finding, it still fires, it still routes to the dashboard where
    # it means something as a rate; it is simply not a reason to buy an opinion
    # that cannot be about it.
    escalates=False,
)

MONITORS: Final[tuple[Monitor, ...]] = (
    CROSS_VISITOR,
    UNGROUNDED_CLAIM,
    UNGROUNDED_CLAIM_JUDGED,
    REFUSAL_WITH_EVIDENCE,
    PHOTO_WITHOUT_SKU,
    BUDGET_BREACH,
)
"""Every monitor, most alarming first. The order an operator reads them in."""


def monitor(name: str) -> Monitor:
    """Look one up by name.

    Args:
        name: The monitor's name.

    Returns:
        The monitor.

    Raises:
        KeyError: If there is no such monitor.
    """
    for item in MONITORS:
        if item.name == name:
            return item
    raise KeyError(name)


def evaluate(
    turn: LiveTurn,
    *,
    grounded: bool | None = None,
    declined: bool | None = None,
) -> tuple[Alert, ...]:
    """Run every monitor whose input this turn has.

    Args:
        turn: The live turn.
        grounded: What a judge said about its claims, or ``None`` where the turn
            was not sampled. ``None`` is not ``True``: an unjudged turn is a
            turn nobody asked about, and the judged monitor stays silent rather
            than clearing it.
        declined: What a judge said about whether the reply withheld an answer,
            on the same terms.

    Returns:
        Every alert, in :data:`MONITORS` order. Empty is the ordinary case and
        is not evidence of anything: three of the monitors need a photo, a
        retrieval or a judge to have an opinion at all.
    """
    if not turn.readable:
        # An unreadable trace is a monitoring failure rather than a product
        # one, and firing five monitors on half a turn would bury the real
        # ones. #103 is the fix; the run's own counters are where this shows.
        return ()
    alerts: list[Alert] = []
    alerts.extend(_cross_visitor(turn))
    alerts.extend(_ungrounded(turn))
    alerts.extend(_judged_grounding(turn, grounded))
    alerts.extend(_refusal(turn, declined))
    alerts.extend(_photo(turn))
    alerts.extend(_budget(turn))
    return tuple(alerts)


def _cross_visitor(turn: LiveTurn) -> Sequence[Alert]:
    """Launch gate one, watched continuously.

    Fires on the *signal*, not on a confirmed disclosure. An identifier
    belonging to somebody else appearing anywhere in a turn is not proof a
    visitor saw it, and waiting for proof would mean the monitor never fires
    until it is too late to matter. #76's own words: *of any kind, which should
    be impossible and therefore alarming*.
    """
    if not turn.foreign_identifiers:
        return ()
    return (
        Alert(
            monitor=CROSS_VISITOR.name,
            severity=CROSS_VISITOR.severity,
            trace_id=turn.trace_id,
            detail=(
                f"{len(turn.foreign_identifiers)} identifier(s) not belonging to "
                f"this visitor appeared in the turn: "
                f"{', '.join(turn.foreign_identifiers)}"
            ),
        ),
    )


def _ungrounded(turn: LiveTurn) -> Sequence[Alert]:
    """The deterministic half of monitor one: a claim with nothing behind it.

    Two conditions, and both are facts about a payload rather than judgements.
    A turn that searched and got nothing back, and then made a food or policy
    claim, cannot be grounded in anything whatever it said. And a turn carrying
    a citation id the retriever never returned has minted a source, which is a
    violation rather than a nuisance even though the renderer drops it.
    """
    alerts: list[Alert] = []
    claims = turn.claim_class in {"food", "policy", "allergen"}
    if claims and turn.searched and not turn.retrieved:
        alerts.append(
            Alert(
                monitor=UNGROUNDED_CLAIM.name,
                severity=UNGROUNDED_CLAIM.severity,
                trace_id=turn.trace_id,
                detail=(
                    f"a {turn.claim_class} claim on a turn whose "
                    f"{turn.searched} search(es) returned nothing"
                ),
            )
        )
    if claims and not turn.citations:
        alerts.append(
            Alert(
                monitor=UNGROUNDED_CLAIM.name,
                severity=UNGROUNDED_CLAIM.severity,
                trace_id=turn.trace_id,
                detail=f"a {turn.claim_class} claim carrying no citation (PRD K2)",
            )
        )
    if turn.dropped_citations:
        alerts.append(
            Alert(
                monitor=UNGROUNDED_CLAIM.name,
                severity=UNGROUNDED_CLAIM.severity,
                trace_id=turn.trace_id,
                detail=(
                    "the response named passage(s) the retriever never returned: "
                    + ", ".join(turn.dropped_citations)
                ),
            )
        )
    return alerts


def _judged_grounding(turn: LiveTurn, grounded: bool | None) -> Sequence[Alert]:
    """The judged half of monitor one."""
    if grounded is not False:
        return ()
    return (
        Alert(
            monitor=UNGROUNDED_CLAIM_JUDGED.name,
            severity=UNGROUNDED_CLAIM_JUDGED.severity,
            trace_id=turn.trace_id,
            detail=(
                f"the judge found a claim the turn's {turn.retrieved} retrieved "
                "passage(s) do not support"
            ),
            judged=True,
        ),
    )


def _refusal(turn: LiveTurn, declined: bool | None) -> Sequence[Alert]:
    """Monitor three, and the only one whose condition is being too careful.

    A refusal is only interesting where the turn's *own* retrieval answered the
    question. A decline on a turn that retrieved nothing is the product working:
    that is precisely what *"where the published data stops, stop"* asks for.
    So the condition is a declining reply on a turn holding passages, which is
    over-refusal as #75 defines it, computed from the same two facts.
    """
    if declined is not True or turn.retrieved == 0:
        return ()
    return (
        Alert(
            monitor=REFUSAL_WITH_EVIDENCE.name,
            severity=REFUSAL_WITH_EVIDENCE.severity,
            trace_id=turn.trace_id,
            detail=(
                f"the reply declined while holding {turn.retrieved} retrieved "
                "passage(s); a system that hedges everything scores beautifully"
            ),
            judged=True,
        ),
    )


def _photo(turn: LiveTurn) -> Sequence[Alert]:
    """Monitor two, in both directions the ticket names.

    *The matcher escalating* is worth seeing as a rate -- a lane that asks on
    every second photograph is not usable. *Not escalating when it should* is
    the worse one and the harder one: a resolution with no SKU and no escalation
    is a turn that neither ordered anything nor asked, which is a dead end the
    visitor experiences as the product ignoring them.
    """
    if not turn.matched_photo:
        return ()
    if turn.resolved_skus:
        return ()
    if turn.escalated:
        return (
            Alert(
                monitor=PHOTO_WITHOUT_SKU.name,
                severity=PHOTO_WITHOUT_SKU.severity,
                trace_id=turn.trace_id,
                detail="the matcher resolved no SKU and escalated (PRD V5, working)",
            ),
        )
    return (
        Alert(
            monitor=PHOTO_WITHOUT_SKU.name,
            severity=PHOTO_WITHOUT_SKU.severity,
            trace_id=turn.trace_id,
            detail=(
                "the matcher resolved no SKU and did NOT escalate; the turn "
                "neither ordered anything nor asked"
            ),
        ),
    )


def _budget(turn: LiveTurn) -> Sequence[Alert]:
    """Monitor five: the two numbers PRD §05 puts a ceiling on.

    A zero duration is *not measured* rather than *fast*. Backends differ in
    whether they carry span end times through their query API, and a monitor
    that read a missing number as compliance would report a latency breach as
    the product meeting its target -- which is the single most flattering bug a
    monitor can have.
    """
    alerts: list[Alert] = []
    if turn.duration_ms > LATENCY_CEILING_MS:
        alerts.append(
            Alert(
                monitor=BUDGET_BREACH.name,
                severity=BUDGET_BREACH.severity,
                trace_id=turn.trace_id,
                detail=(
                    f"the turn took {turn.duration_ms:.0f} ms against a "
                    f"{LATENCY_CEILING_MS:.0f} ms target"
                ),
            )
        )
    if turn.total_tokens > COST_TOKEN_CEILING:
        alerts.append(
            Alert(
                monitor=BUDGET_BREACH.name,
                severity=BUDGET_BREACH.severity,
                trace_id=turn.trace_id,
                detail=(
                    f"the turn spent {turn.total_tokens} tokens against a "
                    f"{COST_TOKEN_CEILING} per-turn ceiling"
                ),
            )
        )
    return alerts
