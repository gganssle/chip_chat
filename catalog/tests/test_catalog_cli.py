"""That ``python -m chip_chat.catalog`` builds a catalogue out of a landing zone.

The offline path is the one that matters and the one tested here: a landing
zone is warmed by a harvest against the fixture site, and then the command
builds the catalogue from the cache with a transport that would raise if it
were asked for anything. That is the same thing ``--offline`` claims on the
command line, checked rather than described.
"""

import json
from pathlib import Path

import pytest
from catalog_fixtures import chipotle, harvester

from chip_chat.catalog.__main__ import build_parser, main
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.harvest.sources.chipotle import (
    harvest_menu,
    harvest_nutrition,
    harvest_policy,
)

STORES = 30


@pytest.fixture(name="landing")
def landing_zone(tmp_path: Path) -> Path:
    """A landing zone with the fixture site's raw documents already in it."""
    blobs = LocalBlobStore(tmp_path)
    served = chipotle.site()
    harvest_menu(harvester(served, blobs=blobs), [chipotle.REFERENCE])
    harvest_nutrition(harvester(served, blobs=blobs), [chipotle.REFERENCE])
    harvest_policy(harvester(served, blobs=blobs), store_count=STORES)
    return tmp_path


def test_the_parser_requires_a_landing_zone() -> None:
    """Nothing here has a default place to write; naming one is the caller's job."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    parsed = build_parser().parse_args(["--landing", "landing"])
    assert parsed.landing == Path("landing")
    assert parsed.offline is False
    assert parsed.vocabulary is None


def test_it_builds_and_writes_the_catalogue_offline(
    landing: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nine files and a manifest, out of a cache and no network at all."""
    status = main(["--landing", str(landing), "--offline", "--stores", str(STORES)])
    assert status == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["reference_restaurant_id"] == 679
    assert manifest["tables"]["menu_items"]["rows"] == 10
    assert len(manifest["content_version"]) == 64

    written = sorted(path.name for path in (landing / "catalog" / "chipotle").iterdir())
    assert "manifest.json" in written
    assert "menu_items.jsonl" in written


def test_it_writes_the_vision_vocabulary_where_it_is_asked_to(
    landing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build-time generation RFC-001 section 07 asks for, as a command."""
    target = tmp_path / "generated" / "vision_vocabulary.py"
    status = main(
        [
            "--landing",
            str(landing),
            "--offline",
            "--stores",
            str(STORES),
            "--vocabulary",
            str(target),
        ]
    )
    assert status == 0
    capsys.readouterr()

    module = target.read_text(encoding="utf-8")
    assert "DO NOT EDIT" in module
    assert "class Vessel(StrEnum):" in module
    assert 'BURRITO = "burrito"' in module


def test_a_cold_landing_zone_fails_rather_than_fetching(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--offline`` means offline; an empty cache is an error, not a harvest."""
    status = main(["--landing", str(tmp_path), "--offline"])
    assert status == 1
    assert "catalogue build failed" in capsys.readouterr().err
