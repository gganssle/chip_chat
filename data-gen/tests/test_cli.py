"""That the command line generates, writes and reports the same thing twice.

The reproducibility criterion is asserted on records and bytes in
``test_determinism.py``. This is the same claim at the level a person actually
checks it: run the command twice against the same landing zone and compare the
``population_version`` it prints.
"""

import json
from pathlib import Path

import pytest
from population_fixtures import CATALOG_FIXTURES, PACKAGED

from chip_chat.data_gen.__main__ import main
from chip_chat.data_gen.records import DEFAULT_PREFIX, TABLES


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    """A landing zone with the fixture catalogue already in it."""
    written = tmp_path / "catalog" / "chipotle"
    written.mkdir(parents=True)
    for source in (CATALOG_FIXTURES / "catalog").iterdir():
        (written / source.name).write_bytes(source.read_bytes())
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
