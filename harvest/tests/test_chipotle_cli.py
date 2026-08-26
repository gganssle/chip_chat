"""The command line, exercised on the offline path.

Only the offline path: the other one fetches from a real site, and a test
suite that reaches a third party's servers is a test suite that fails when
their DNS does, and that harvests them a few thousand times a year for no
reason. The landing zone is seeded through the framework with a fake
transport, exactly as the fetching path would have left it.
"""

import json
from pathlib import Path

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import harvest_menu
from chip_chat.harvest.sources.chipotle.__main__ import main
from chip_chat.harvest.testing import FakeClock


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    """A landing zone with the fixture site already harvested into it."""
    clock = FakeClock()
    harvester = Harvester(
        LocalBlobStore(tmp_path),
        site.site(),
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )
    harvest_menu(harvester, [site.REFERENCE])
    return tmp_path


def test_the_offline_run_writes_the_tables_and_prints_the_manifest(
    landing: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["--landing", str(landing), "--offline"])

    assert status == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["reference_restaurant_id"] == 679
    assert manifest["tables"]["menu_items"]["rows"] > 0
    parsed = landing / "parsed" / "chipotle" / "menu"
    assert (parsed / "menu_items.jsonl").is_file()
    assert (parsed / "manifest.json").is_file()


def test_two_offline_runs_print_the_same_manifest(
    landing: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reproducibility claim, from the outside."""
    assert main(["--landing", str(landing), "--offline"]) == 0
    first = capsys.readouterr().out
    assert main(["--landing", str(landing), "--offline"]) == 0
    second = capsys.readouterr().out

    assert first == second


def test_an_unharvested_landing_zone_fails_with_a_useful_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["--landing", str(tmp_path), "--offline"])

    assert status == 1
    assert "run the harvest first" in capsys.readouterr().err
