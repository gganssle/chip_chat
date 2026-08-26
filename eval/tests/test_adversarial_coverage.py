"""Whether the shipped suite is the suite issue #30 asked for.

The scorer says whether the target survived the attacks it was given. Nothing in
it can notice that those were four polite questions -- that suite produces zero
breaches and two clean gates, which is exactly what a sound design produces.
These are the checks that see it.
"""

import json
from pathlib import Path

from chip_chat.eval.adversarial.attacks import AdversarialSuite, Family
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


def test_removing_a_disclosure_attack_is_visible_as_an_unmet_clause(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    """The clause has to be able to fail, or it is a comment.

    A3 says *under any phrasing*, and a mechanism that stops the direct
    question and not the oblique one has stopped nothing -- so the floor on
    variety is a check rather than an aspiration.
    """
    thinner = _without(
        tmp_path,
        suite,
        "disclosure-aggregate-across-visitors",
        "disclosure-debug-pretext",
    )

    cover = coverage(thinner)

    assert cover.unmet
    assert not cover.complete
    assert any("phrasings" in clause.name for clause, _ in cover.unmet)


def test_removing_the_only_redeem_attack_is_visible_as_an_unattacked_tool(
    tmp_path: Path, suite: AdversarialSuite
) -> None:
    thinner = _without(tmp_path, suite, "write-redeem-points-irreversibly")

    cover = coverage(thinner)

    assert "redeem_points" in {tool.value for tool in cover.write_tools_without_an_attack}
    assert not cover.complete


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
