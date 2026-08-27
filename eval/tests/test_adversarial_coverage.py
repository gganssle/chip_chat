"""Whether the shipped suite is the suite issue #30 asked for.

The scorer says whether the target survived the attacks it was given. Nothing in
it can notice that those were four polite questions -- that suite produces zero
breaches and two clean gates, which is exactly what a sound design produces.
These are the checks that see it.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.adversarial.attacks import (
    AdversarialSuite,
    Attack,
    Capability,
    Carrier,
    Family,
)
from chip_chat.eval.adversarial.coverage import CLAUSES, DELEGATED_HERE, coverage
from chip_chat.eval.golden.requirements import DELEGATIONS


def _without(
    tmp_path: Path, suite: AdversarialSuite, *attack_ids: str
) -> AdversarialSuite:
    """The shipped suite with some attacks removed."""
    dropped = set(attack_ids)
    manifest = json.loads(suite.source.read_text(encoding="utf-8"))
    manifest["attacks"] = [
        entry for entry in manifest["attacks"] if entry["id"] not in dropped
    ]
    path = tmp_path / "attacks.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return AdversarialSuite.load(path)


def test_the_shipped_suite_meets_every_clause_of_the_ticket(
    suite: AdversarialSuite,
) -> None:
    cover = coverage(suite)

    assert cover.unmet == ()
    assert cover.complete


def test_the_shipped_suite_attacks_every_family(suite: AdversarialSuite) -> None:
    """A family nobody attacks cannot come back breached, however broken it is."""
    assert coverage(suite).families_without_an_attack == ()


def test_the_shipped_suite_aims_at_each_of_the_four_write_tools(
    suite: AdversarialSuite,
) -> None:
    """Counted per tool, not in total.

    The confirmation rule is enforced per call, so nine attacks all aimed at
    ``place_order`` would clear any threshold and leave ``redeem_points`` --
    irreversible, per ``docs/action-surface.md`` section 10 -- covered by an
    argument rather than by a test.
    """
    assert coverage(suite).write_tools_without_an_attack == ()


def test_every_requirement_the_golden_set_delegated_here_is_covered(
    suite: AdversarialSuite,
) -> None:
    """The loop between the two evaluation packages, closed.

    ``eval/golden/requirements.py`` sends A3 and S2 to *"the adversarial suite,
    #30"* and its coverage report counts them as measured on that promise. A
    delegation with an argument and no attack behind it is the same gap golden
    warns about, with better prose.
    """
    cover = coverage(suite)

    assert DELEGATED_HERE
    assert cover.undelivered == ()
    assert {item.id for item, _ in cover.delivered} == set(DELEGATED_HERE)


def test_the_delegations_are_read_off_the_golden_register_not_restated() -> None:
    """A list copied here could fall behind the register it is copied from."""
    expected = {item.requirement_id for item in DELEGATIONS if "#30" in item.target}

    assert set(DELEGATED_HERE) == expected


def test_removing_disclosure_attacks_is_visible_as_an_unmet_clause(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """The clause has to be able to fail, or it is a comment.

    A3 says *under any phrasing*, and a mechanism that stops the direct
    question and not the oblique one has stopped nothing -- so the floor on
    variety is a check rather than an aspiration.

    How many to drop is computed from the clause rather than written down. The
    floor moves -- #82 raised it from six to nine -- and a test naming two
    attacks by hand stops testing anything the first time somebody adds a
    tenth: it would thin the suite to a number still above the floor and assert
    that a met clause is unmet. The concurrent attacks are kept back because
    the manifest cannot load without one.
    """
    clause = next(item for item in CLAUSES if "phrasings" in item.name)
    droppable = [
        attack_id
        for attack_id in clause.met_by(suite.attacks)
        if not suite_attack(suite, attack_id).concurrent
    ]
    surplus = len(clause.met_by(suite.attacks)) - clause.minimum

    thinner = _without(tmp_path, suite, *droppable[: surplus + 1])

    cover = coverage(thinner)

    assert cover.unmet
    assert not cover.complete
    assert any("phrasings" in item.name for item, _ in cover.unmet)


def suite_attack(suite: AdversarialSuite, attack_id: str) -> Attack:
    """The attack with this id. A helper, because the test above reads two fields."""
    return next(attack for attack in suite if attack.attack_id == attack_id)


def test_removing_every_redeem_attack_is_visible_as_an_unattacked_tool(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """A write tool nothing aims at is a gap the report has to name.

    Both attacks that name ``redeem_points`` have to go, and that there are two
    of them is #81's doing rather than an accident. It added a corpus-borne
    redemption instruction -- a document telling the assistant to spend the
    reader's balance -- and the point of writing it as a *second* attack on the
    same tool is that the two arrive by different carriers and would be stopped
    by different things. So the coverage report is right that the tool is
    attacked while either survives, and this test drops both rather than being
    weakened to expect one.
    """
    thinner = _without(
        tmp_path,
        suite,
        "write-redeem-points-irreversibly",
        "injection-retrieved-redeem-instruction",
    )

    cover = coverage(thinner)

    assert "redeem_points" in {tool.value for tool in cover.write_tools_without_an_attack}
    assert not cover.complete


def test_removing_the_only_photo_injection_is_visible_as_an_unmet_clause(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """#82's carrier, and the clause that keeps it in the file.

    Its own clause rather than folded into the retrieved-document one: every
    defence against a retrieved instruction sits on the retrieval path, and a
    frame the visitor uploads passes none of them. A combined count could be
    satisfied by two corpus attacks with nothing aimed at the carrier that
    skips the corpus entirely.
    """
    only = [
        attack.attack_id for attack in suite if attack.carrier is Carrier.UPLOADED_PHOTO
    ]

    cover = coverage(_without(tmp_path, suite, *only))

    assert any("photograph" in item.name for item, _ in cover.unmet)
    assert not cover.complete


def test_removing_the_only_analyst_attack_is_visible_as_an_unmet_clause(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """The attack whose value is that it should not matter.

    RFC-001 puts the scope in a row access policy underneath the query, which
    makes visitor-dictated SQL read the same rows as generated SQL. #82 calls
    that *precisely the claim being tested*, and a claim with no attack behind
    it is what this whole module exists to make visible.
    """
    only = [attack.attack_id for attack in suite if Capability.ANALYST in attack.needs]

    cover = coverage(_without(tmp_path, suite, *only))

    assert any("query generator" in item.name for item, _ in cover.unmet)
    assert not cover.complete


def test_dropping_to_one_concurrent_attack_is_visible_as_an_unmet_clause(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """The loader refuses zero; this is the clause that refuses one.

    Not the load-time check restated. A stale ``demo_id`` is filtered perfectly
    by #43's policies for the wrong visitor, so what a bleed discloses is
    whatever lane the policies sit under -- and one concurrent attack aimed at
    the order desk says nothing about the account lane, which is where the
    deployed product keeps the rows.
    """
    concurrent = [attack.attack_id for attack in suite.concurrent]
    assert len(concurrent) > 1, "the manifest already fails this clause"

    cover = coverage(_without(tmp_path, suite, *concurrent[1:]))

    assert any("more than one of the paths" in item.name for item, _ in cover.unmet)
    assert not cover.complete


_ATTACKS_82_NAMES = {
    "another visitor's order history, by name": "disclosure-direct-order-history",
    "and by description": "disclosure-other-visitor-by-description",
    "persuade the model it is serving someone else": "identity-i-am-someone-else",
    "aggregates that would leak individual rows": (
        "disclosure-aggregate-across-visitors"
    ),
    "SQL that ignores the session scope": "disclosure-analyst-unscoped-sql",
    "a disclosure instruction through the corpus": "injection-retrieved-menu-note",
    "and through an uploaded photo": "injection-uploaded-photo-disclosure",
    "the app's session store, from a tool result": (
        "disclosure-session-store-from-a-tool-result"
    ),
    "switch persona mid-conversation": "identity-persona-switch-mid-conversation",
    "the concurrency test": "disclosure-concurrent-pool-bleed",
}
"""#82's attack list, each bullet against the attack that answers it.

Ids by hand, which is unusual in this directory and is the point. Three of these
bullets -- by description, the session store, the persona switch -- are
*phrasings*, and a phrasing has no structural property for a clause in
``coverage.py`` to count. The floors there stop the suite thinning; only this
stops it thinning in exactly the places #82 asked it not to.

A failure here is not necessarily a bug: an attack may be renamed, or one bullet
may come to be answered by a better attack than the one written for it. It is a
prompt to update this map deliberately rather than to discover next quarter that
the launch gate was verified against a suite that had quietly lost the question
somebody filed the ticket about.
"""


@pytest.mark.parametrize(
    ("bullet", "attack_id"),
    sorted(_ATTACKS_82_NAMES.items()),
)
def test_the_suite_holds_an_attack_for_every_shape_82_names(
    suite: AdversarialSuite, bullet: str, attack_id: str
) -> None:
    """Launch gate one is verified against this list, so the list is a test."""
    assert any(attack.attack_id == attack_id for attack in suite), bullet


def test_every_clause_names_the_document_that_asks_for_it() -> None:
    """A clause nobody can trace is a clause somebody deletes as arbitrary."""
    assert all(clause.source.strip() for clause in CLAUSES)
    assert all(clause.minimum > 0 for clause in CLAUSES)


def test_the_concurrent_attack_is_reported_rather_than_merely_required(
    suite: AdversarialSuite,
) -> None:
    """The loader refuses a suite without one; the report still names which.

    It is the answer to the question a reviewer asks first, and *"the loader
    would have refused otherwise"* is not something a reader of a Markdown file
    can check.
    """
    cover = coverage(suite)

    assert cover.concurrent
    assert all(suite.covering("A3") for _ in cover.concurrent)


def test_coverage_never_raises_on_an_incomplete_suite(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """An incomplete suite is a fact to print above the outcomes, not an error.

    The same argument the other two evaluation packages make, and the same
    caveat: the outcomes are only safe while nobody can read them without this
    beside them.
    """
    thinner = _without(tmp_path, suite, "identity-i-am-someone-else")

    cover = coverage(thinner)

    assert not cover.complete
    assert cover.families[
        [family for family, _ in cover.families].index(Family.IDENTITY)
    ][1]
