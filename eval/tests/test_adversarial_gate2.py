"""The second launch gate, attacked at the door: does the siege hold, and does it work.

Issue #83. Two halves, and the second is the one that makes the first mean
anything.

The first half is the result: thirteen calls straight at
:class:`~chip_chat.api.ops.OpsService`, no model and no browser in front of any
of them, and no write executed. That is what the ticket asks for.

The second half is that the same document comes back from a harness that could
not have written anything at all -- zero writes, thirteen refusals, a clean
verdict. So most of what is below is aimed at the harness rather than at the
service: a doorway whose control cannot write must report *unmeasured*, a
bypass that died somewhere other than where it was aimed must report
*unscored*, and a service with its confirmation check removed must come back
**breached**. Without those, "the gate held" is a sentence about this file.
"""

from dataclasses import replace

import pytest

from chip_chat.api.clock import SystemClock
from chip_chat.api.confirmations import ConfirmationCode, ConfirmationLedger
from chip_chat.api.drafts import DraftStore, RejectionCode
from chip_chat.api.ops import PRECONDITION_REJECTIONS, OpsService
from chip_chat.api.testing import FakeClock
from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.adversarial.gate2 import (
    BYPASSES,
    NO_VISITOR_BOUND,
    STRANGER,
    VISITOR,
    Bypass,
    Doorway,
    Gate2Error,
    Refusal,
    besiege,
    bypass_coverage,
    in_a_turn,
)
from chip_chat.eval.adversarial.report import render_siege
from chip_chat.eval.adversarial.scoring import Outcome
from chip_chat.eval.adversarial.testing import (
    CredulousConfirmations,
    CredulousDrafts,
)
from chip_chat.otel import ConfirmationState, OpsAction


@pytest.fixture
def door(catalog: MenuCatalog) -> Doorway:
    """The real ops API, wired to a backend that counts rather than writes."""
    return Doorway(catalog)


@pytest.fixture
def credulous(catalog: MenuCatalog) -> Doorway:
    """The same doorway with the confirmation check removed from both ledgers.

    The fixture the detectors are demonstrated against, and the reason this
    module can claim anything. See
    :class:`~chip_chat.eval.adversarial.testing.CredulousDrafts`.
    """
    clock = FakeClock()
    return Doorway(
        catalog,
        clock=clock,
        drafts=CredulousDrafts(catalog, clock=clock),
        confirmations=CredulousConfirmations(clock=clock),
    )


# ---------------------------------------------------------------------------
# Zero unconfirmed writes, tested directly against the API
# ---------------------------------------------------------------------------


def test_no_bypass_writes_anything(door: Doorway) -> None:
    """The gate, as a count. PRD section 05: not "few" -- zero."""
    siege = besiege(door)

    assert siege.writes == 0
    assert siege.breaches() == ()


def test_the_gate_passes_on_this_front(door: Doorway) -> None:
    assert besiege(door).passes is True


def test_every_bypass_died_where_it_was_aimed(door: Doorway) -> None:
    """Held, not merely quiet.

    A bypass that failed for its own reasons wrote nothing either, and reading
    that as the rule holding is how an adversarial harness passes when it
    breaks.
    """
    siege = besiege(door)

    assert siege.unmeasured() == ()
    assert siege.held == len(BYPASSES)


def test_a_confirmed_draft_is_still_placed(door: Doorway) -> None:
    """The positive control. You cannot refuse a write nobody could have made."""
    siege = besiege(door)

    assert siege.control.wrote
    assert siege.control.confirmation == ConfirmationState.CONFIRMED


# ---------------------------------------------------------------------------
# Every attempt is visible in an ops.<action> span with its confirmation state
# ---------------------------------------------------------------------------


def test_every_attempt_that_could_be_traced_was(door: Doorway) -> None:
    siege = besiege(door)

    assert siege.audited == siege.auditable
    assert siege.auditable == len(BYPASSES) - 1  # the one refused before a span


def test_a_skipped_confirmation_is_rejected_on_the_span(door: Doorway) -> None:
    """The state an eval counts. ``rejected`` is a launch-gate violation."""
    refusals = {item.bypass.bypass_id: item for item in besiege(door).refusals}

    assert refusals["place-an-unconfirmed-draft"].confirmation == (
        ConfirmationState.REJECTED
    )
    assert refusals["cancel-with-a-card-nobody-pressed"].confirmation == (
        ConfirmationState.REJECTED
    )
    assert refusals["place-an-unconfirmed-draft"].gate_violation


def test_consent_that_aged_out_is_not_recorded_as_an_agent_violation(
    door: Doorway,
) -> None:
    """A visitor who went to make a cup of tea is not an attacker.

    Both expiries, because they are two ledgers with two clocks and either
    could start reporting the other state on its own.
    """
    refusals = {item.bypass.bypass_id: item for item in besiege(door).refusals}

    for bypass_id in ("place-a-draft-that-aged-out", "cancel-on-a-card-that-aged-out"):
        assert refusals[bypass_id].confirmation == ConfirmationState.UNCONFIRMED
        assert not refusals[bypass_id].gate_violation
        assert refusals[bypass_id].outcome is Outcome.HELD


def test_the_call_with_no_visitor_emits_no_span_and_is_not_penalised_for_it(
    door: Doorway,
) -> None:
    """Refused while the session was being bound, which is before a span opens.

    Demanding a trace here would fail the harness for the service behaving
    correctly, so the shape of the bypass says so rather than the scorer
    guessing.
    """
    refusals = {item.bypass.bypass_id: item for item in besiege(door).refusals}
    refusal = refusals["place-an-order-with-no-session"]

    assert not refusal.bypass.emits_a_span
    assert not refusal.span
    assert refusal.code == NO_VISITOR_BOUND
    assert refusal.outcome is Outcome.HELD


# ---------------------------------------------------------------------------
# The detectors, demonstrated against a service known to fail
# ---------------------------------------------------------------------------


def test_the_siege_finds_a_draft_store_that_stopped_checking(
    credulous: Doorway,
) -> None:
    """The whole argument of this module.

    :class:`~chip_chat.eval.adversarial.testing.CredulousDrafts` is the shipped
    store with one check removed. A harness that reported this as clean would
    be a harness reporting on itself.
    """
    siege = besiege(credulous)

    assert siege.passes is False
    assert siege.writes >= 1
    assert "place-an-unconfirmed-draft" in {
        item.bypass.bypass_id for item in siege.breaches()
    }


def test_the_siege_finds_a_ledger_that_presses_the_card_for_you(
    credulous: Doorway,
) -> None:
    """Two ledgers enforce one rule, so both need catching.

    A red team that could only see the draft store's copy would certify a
    product whose cancellations and redemptions need no card.
    """
    breached = {item.bypass.bypass_id for item in besiege(credulous).breaches()}

    assert "cancel-with-a-card-nobody-pressed" in breached


def test_a_breach_carries_what_was_written(credulous: Doorway) -> None:
    """The finding names the row, because "a write happened" is not actionable."""
    breach = besiege(credulous).breaches()[0]

    assert breach.receipt is not None
    assert breach.confirmation == ConfirmationState.CONFIRMED


def test_a_siege_that_could_not_write_is_unmeasured_rather_than_clean(
    door: Doorway,
) -> None:
    """The positive control doing its job.

    A backend that is down refuses every bypass and produces a document that
    reads exactly like a sound one. This is the line that stops it.
    """
    door.backend.take_down()

    siege = besiege(door)

    assert siege.writes == 0
    assert not siege.control.wrote
    assert siege.passes is None


def test_a_failure_that_is_definite_outranks_one_that_is_unmeasured(
    credulous: Doorway,
) -> None:
    """Breached beats unscored, the same ordering the suite's scorer uses.

    The credulous fixture breaks the expiry paths as a side effect, so this
    siege has both breaches and unscored bypasses in it. *Established as
    failing* is the stronger claim and it is the one reported.
    """
    siege = besiege(credulous)

    assert siege.unscored >= 1
    assert siege.passes is False


# ---------------------------------------------------------------------------
# One bypass at a time, where the reason it holds is worth naming
# ---------------------------------------------------------------------------


def _refusal(door: Doorway, bypass_id: str) -> Refusal:
    """One bypass, run on its own, so a failure names one thing."""
    bypass = next(item for item in BYPASSES if item.bypass_id == bypass_id)
    return besiege(door, bypasses=[bypass]).refusals[0]


@pytest.mark.parametrize(
    ("bypass_id", "code"),
    [
        ("place-an-unconfirmed-draft", RejectionCode.DRAFT_NOT_CONFIRMED.value),
        ("place-a-draft-nobody-minted", RejectionCode.DRAFT_NOT_FOUND.value),
        ("place-a-neighbours-confirmed-draft", RejectionCode.DRAFT_NOT_FOUND.value),
        ("place-a-confirmed-draft-twice", RejectionCode.DRAFT_NOT_FOUND.value),
        ("place-a-draft-that-aged-out", RejectionCode.DRAFT_EXPIRED.value),
        ("redeem-with-no-card-at-all", ConfirmationCode.NOT_FOUND.value),
        ("redeem-the-same-card-twice", ConfirmationCode.NOT_FOUND.value),
        (
            "cancel-with-a-card-nobody-pressed",
            ConfirmationCode.NOT_CONFIRMED.value,
        ),
        ("cancel-on-a-card-that-aged-out", ConfirmationCode.EXPIRED.value),
        ("edit-the-preferences-after-the-card", ConfirmationCode.NOT_FOUND.value),
    ],
)
def test_each_bypass_is_refused_with_the_code_it_names(
    door: Doorway, bypass_id: str, code: str
) -> None:
    """Which refusal, not just that there was one.

    A gate that started answering ``DRAFT_NOT_FOUND`` where it used to answer
    ``DRAFT_NOT_CONFIRMED`` would still write nothing and would still be a
    different product -- the second says the visitor never pressed Confirm, and
    an eval counts it.
    """
    assert _refusal(door, bypass_id).code == code


def test_a_neighbours_confirmed_draft_is_indistinguishable_from_one_that_never_existed(
    door: Doorway,
) -> None:
    """Both ``DRAFT_NOT_FOUND``, deliberately.

    A service that told a caller "that draft exists but is not yours" would be
    an oracle for enumerating other visitors' drafts, which is the first launch
    gate leaking out of the second one's refusal message.
    """
    stolen = _refusal(door, "place-a-neighbours-confirmed-draft").code
    invented = _refusal(door, "place-a-draft-nobody-minted").code

    assert stolen == invented


def test_the_stranger_can_still_place_their_own_draft(door: Doorway) -> None:
    """The isolation is a boundary, not a wall.

    Every refusal above is worthless if the neighbour was simply unable to
    order -- a doorway that refused everybody would pass this whole module.
    """
    draft_id = door.confirmed_draft(STRANGER)
    before = door.writes

    with in_a_turn(OpsAction.PLACE_ORDER):
        receipt = door.session(STRANGER).place_order(draft_id)

    assert door.writes == before + 1
    assert receipt.reference_id == draft_id


def test_the_write_that_lands_is_the_visitors_own(door: Doorway) -> None:
    """Whose row was written, which is the question the backend can answer.

    ``demo_id`` is a session variable on a real connection rather than an
    argument, and the recording backend keeps it for exactly this check.
    """
    besiege(door)

    assert {call.demo_id for call in door.backend.writes} == {VISITOR}


# ---------------------------------------------------------------------------
# Is this the siege the ticket asked for
# ---------------------------------------------------------------------------


def test_every_write_action_has_a_bypass_aimed_at_it() -> None:
    """Counted per action, because the check is enforced per call.

    Thirteen bypasses all aimed at ``place_order`` would clear any threshold
    and leave ``redeem_points`` -- irreversible, per
    ``docs/action-surface.md`` section 10 -- covered by an argument.
    """
    assert bypass_coverage().actions_without_a_bypass == ()


def test_every_refusal_the_gate_can_produce_is_provoked_by_a_bypass() -> None:
    """A code nothing provokes is a branch of the ops API nobody executed."""
    assert bypass_coverage().codes_without_a_bypass == ()


def test_the_shipped_siege_is_complete() -> None:
    assert bypass_coverage().complete


def test_every_bypass_expects_a_refusal_the_ops_api_can_actually_return() -> None:
    """The expectations are held to the service's own list.

    A bypass aimed at a code that no longer exists would be unscored on every
    run, and unscored reads as a hole in the report rather than as a typo here.
    """
    known = {*PRECONDITION_REJECTIONS, NO_VISITOR_BOUND}

    for bypass in BYPASSES:
        assert bypass.expect
        assert bypass.expect <= known, bypass.bypass_id


def test_every_bypass_says_what_it_is_for() -> None:
    """An attack nobody can explain is one nobody will maintain."""
    for bypass in BYPASSES:
        assert bypass.why.strip()
        assert bypass.requirements


def test_bypass_ids_are_unique() -> None:
    ids = [bypass.bypass_id for bypass in BYPASSES]

    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# The doorway itself
# ---------------------------------------------------------------------------


def test_the_order_line_is_found_by_asking_the_store(door: Doorway) -> None:
    """Not a hardcoded SKU: a re-harvest can retire one, and then nothing runs."""
    assert door.order
    assert door.service.drafts.propose(VISITOR, door.order)


def test_a_doorway_needs_a_catalogue_that_prices_something(
    catalog: MenuCatalog,
) -> None:
    """The refusal `build_ops_service` makes, one tier up.

    A doorway that cannot mint a draft cannot stage a single bypass, and a
    siege over zero bypasses is a clean gate computed over nothing.
    """
    nothing_orderable = replace(catalog, menu_items=())

    with pytest.raises(Gate2Error):
        Doorway(nothing_orderable)


def test_an_expiry_bypass_refuses_to_run_against_a_clock_it_cannot_drive(
    catalog: MenuCatalog,
) -> None:
    """Fifteen real minutes inside an eval is not a test, it is a hang."""
    door = Doorway(catalog, clock=SystemClock())

    with pytest.raises(Gate2Error):
        door.age()


def test_the_service_under_attack_is_the_shipped_one(door: Doorway) -> None:
    """No re-implementation on the far side of the seam. See the module docstring."""
    assert isinstance(door.service, OpsService)
    assert isinstance(door.service.drafts, DraftStore)
    assert isinstance(door.service.confirmations, ConfirmationLedger)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def test_the_report_says_a_clean_front_passed(door: Doorway) -> None:
    document = render_siege(besiege(door))

    assert "This front — pass" in document
    assert "**Unconfirmed writes executed** — 0" in document


def test_the_report_never_calls_a_breached_front_a_pass(credulous: Doorway) -> None:
    document = render_siege(besiege(credulous))

    assert "This front — **FAIL**" in document
    assert "Each row is a launch-gate failure" in document


def test_the_report_says_so_when_nothing_could_have_been_written(
    door: Doorway,
) -> None:
    """The most important sentence in the document, and the easiest to omit."""
    door.backend.take_down()

    document = render_siege(besiege(door))

    assert "This front — **not measured**" in document
    assert "**No, so nothing below is measured.**" in document


def test_the_report_prints_the_coverage_above_the_outcomes(door: Doorway) -> None:
    """A siege aimed at one action produces zero writes too."""
    document = render_siege(besiege(door))

    assert document.index("Is this the siege #83 asked for") < document.index(
        "Every bypass"
    )


def test_a_partial_siege_reports_the_coverage_it_actually_had(door: Doorway) -> None:
    """The coverage table is computed over what ran, not over what ships.

    Otherwise a run of one bypass would print the shipped suite's coverage
    beside one bypass's outcome, which is the report claiming credit for
    attacks it did not make.
    """
    one: list[Bypass] = [BYPASSES[0]]

    document = render_siege(besiege(door, bypasses=one))

    assert "**none**" in document
