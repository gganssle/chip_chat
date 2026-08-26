"""That the command line generates, writes and reports the same thing twice.

The reproducibility criterion is asserted on records and bytes in
``test_determinism.py``. This is the same claim at the level a person actually
checks it: run the command twice against the same landing zone and compare the
``population_version`` it prints.
"""

import json
from pathlib import Path

import pytest
from population_fixtures import CATALOG_FIXTURES, PACKAGED, fixture_policy

from chip_chat.data_gen.__main__ import main
from chip_chat.data_gen.records import DEFAULT_PREFIX, TABLES
from chip_chat.harvest.blobs import LocalBlobStore


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    """A landing zone with the catalogue and the policy harvest already in it.

    Both, because the generator reads two real things: what may be ordered,
    and what Chipotle publishes about what an order earns.
    """
    written = tmp_path / "catalog" / "chipotle"
    written.mkdir(parents=True)
    for source in (CATALOG_FIXTURES / "catalog").iterdir():
        (written / source.name).write_bytes(source.read_bytes())
    fixture_policy().write(LocalBlobStore(tmp_path))
    return tmp_path


@pytest.fixture
def small(tmp_path: Path) -> Path:
    """The shipped config, scaled down so the test does not generate five hundred."""
    text = PACKAGED.read_text(encoding="utf-8")
    written = tmp_path / "small.toml"
    written.write_text(
        text.replace("customers = 500", "customers = 40"), encoding="utf-8"
    )
    return written


def run(landing: Path, small: Path, *extra: str) -> int:
    """Run the command line against a landing zone."""
    return main(["--landing", str(landing), "--config", str(small), *extra])


def test_it_writes_every_table_and_a_manifest(
    landing: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(landing, small) == 0

    written = landing / DEFAULT_PREFIX
    assert {path.name for path in written.iterdir()} == {
        *(f"{name}.jsonl" for name in TABLES),
        "manifest.json",
    }
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["tables"]["demo_visitors"]["rows"] == 40


def test_running_it_twice_prints_the_same_version(
    landing: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The check a person does by hand, done here so nobody has to."""
    assert run(landing, small) == 0
    first = json.loads(capsys.readouterr().out)
    assert run(landing, small) == 0
    second = json.loads(capsys.readouterr().out)

    assert first == second


def test_the_seed_can_be_overridden_without_editing_the_file(
    landing: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(landing, small) == 0
    shipped = json.loads(capsys.readouterr().out)
    assert run(landing, small, "--seed", "99") == 0
    overridden = json.loads(capsys.readouterr().out)

    assert overridden["seed"] == 99
    assert overridden["population_version"] != shipped["population_version"]


def test_a_landing_zone_with_no_catalogue_in_it_fails_loudly(
    tmp_path: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rather than generating a population out of nothing."""
    assert run(tmp_path, small) == 1

    assert "population generation failed" in capsys.readouterr().err


def test_a_landing_zone_with_no_published_terms_in_it_fails_loudly(
    tmp_path: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A catalogue is not enough: the ledger's arithmetic is published too.

    Generating anyway would mean choosing an earn rate here, which is the one
    thing issue #27 asks this package never to do.
    """
    written = tmp_path / "catalog" / "chipotle"
    written.mkdir(parents=True)
    for source in (CATALOG_FIXTURES / "catalog").iterdir():
        (written / source.name).write_bytes(source.read_bytes())

    assert run(tmp_path, small) == 1

    assert "policy harvest" in capsys.readouterr().err


def test_the_policy_prefix_can_be_moved(
    landing: Path, small: Path, tmp_path: Path
) -> None:
    """The published terms land where the lakehouse put them, not where this looks."""
    fixture_policy().write(LocalBlobStore(landing), prefix="elsewhere/policy")

    assert run(landing, small, "--policy-prefix", "elsewhere/policy") == 0


def test_a_config_that_does_not_describe_a_population_fails_loudly(
    landing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text(
        PACKAGED.read_text(encoding="utf-8").replace("share = 0.16", "share = 0.90", 1),
        encoding="utf-8",
    )

    assert main(["--landing", str(landing), "--config", str(broken)]) == 1
    assert "must sum to 1.0" in capsys.readouterr().err


def test_the_prefixes_can_be_moved(landing: Path, small: Path) -> None:
    """The lakehouse decides where a stream lands; this program does not."""
    assert run(landing, small, "--prefix", "accounts/experiment") == 0

    assert (landing / "accounts" / "experiment" / "orders.jsonl").is_file()


def test_it_says_how_many_fixtures_each_archetype_supplied(
    landing: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #26's roster, reported where whoever retuned the file is looking."""
    assert run(landing, small) == 0

    reported = capsys.readouterr().err

    assert "persona fixtures (4 wanted each)" in reported
    for persona_id in ("regular", "lapsed", "explorer"):
        assert persona_id in reported


def test_it_warns_when_an_archetype_cannot_fill_its_roster(
    landing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A thin roster is the cold-start risk arriving quietly, so it is said out loud.

    Forty customers cannot supply four exemplars of all seven archetypes, and
    the right response is fewer fixtures plus a warning — never a customer
    promoted past criteria it failed.
    """
    config = tmp_path / "thin.toml"
    config.write_text(
        PACKAGED.read_text(encoding="utf-8").replace("customers = 500", "customers = 40"),
        encoding="utf-8",
    )

    assert main(["--landing", str(landing), "--config", str(config)]) == 0

    reported = capsys.readouterr().err

    assert "warning:" in reported
    assert "clear its own criteria" in reported


def test_it_says_what_the_texture_checks_measured(
    landing: Path, small: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #28's suite runs on every generation, so every run says so.

    Not a verdict — a degenerate population never gets this far, because
    ``generate_population`` refuses it. This is the line whoever just retuned
    the file reads: how much of the catalogue the population managed to reach.
    """
    assert run(landing, small) == 0

    reported = capsys.readouterr().err

    assert "texture: 19 checks held" in reported
    assert "orderable things ordered" in reported


def test_it_writes_the_texture_report_when_asked(
    landing: Path, small: Path, tmp_path: Path
) -> None:
    """``--report`` is how the same document is produced against a real harvest."""
    report = tmp_path / "texture.md"

    assert run(landing, small, "--report", str(report)) == 0

    written = report.read_text(encoding="utf-8")
    assert "The synthetic population is not thin" in written
    assert "persona_separation" in written
    assert "customers worth a demo query" in written


def test_a_population_that_comes_out_thin_stops_the_run(
    landing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """And stops it before anything is written, which is the point of the gate.

    Thinness is asked for here by demanding more of the population than any
    population could give, rather than by breaking the generator: the claim
    under test is that the CLI refuses a population failing ``[texture]``, not
    that some particular tuning produces one.
    """
    config = tmp_path / "unreachable.toml"
    config.write_text(
        PACKAGED.read_text(encoding="utf-8")
        .replace("customers = 500", "customers = 40")
        .replace("busiest_store_share = 0.35", "busiest_store_share = 0.001"),
        encoding="utf-8",
    )

    assert main(["--landing", str(landing), "--config", str(config)]) == 1

    reported = capsys.readouterr().err

    assert "population generation failed" in reported
    assert "busiest_store_share" in reported
    assert not (landing / DEFAULT_PREFIX).exists()
