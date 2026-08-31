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


# ---------------------------------------------------------------------------
# --from-built, which is how the image gets its vocabulary without a harvest
# ---------------------------------------------------------------------------


def test_from_built_writes_a_vocabulary_without_touching_the_harvest(
    landing: Path, tmp_path: Path
) -> None:
    """The CI path: a built catalogue in, a vocabulary module out, nothing else.

    The ordinary path rebuilds the catalogue from the harvest cache, which this
    repository does not commit -- so the deploy workflow could not build the
    image at all, and every deployment was made by hand. That is the failure
    this flag exists to remove, so what is asserted is exactly its precondition:
    a directory holding a *built* catalogue and no cache at all is enough.
    """
    assert main(["--landing", str(landing), "--offline", "--stores", str(STORES)]) == 0

    # A second directory holding only what the build wrote -- no `raw/`, no
    # `parsed/`, nothing the harvester left behind.
    published = tmp_path / "published"
    built = published / "catalog" / "chipotle"
    built.mkdir(parents=True)
    for table in (landing / "catalog" / "chipotle").iterdir():
        built.joinpath(table.name).write_bytes(table.read_bytes())

    module = tmp_path / "vision_vocabulary.py"
    status = main(
        ["--landing", str(published), "--from-built", "--vocabulary", str(module)]
    )

    assert status == 0
    assert module.is_file()
    assert not (published / "raw").exists()


def test_from_built_renders_the_same_module_the_harvest_path_does(
    landing: Path, tmp_path: Path
) -> None:
    """Two routes to one artefact, which are worth nothing if they disagree.

    The image's vocabulary is generated from a committed catalogue and the
    matcher is run against a published one. Byte-equality here is what says the
    shorter route is the same route, rather than a second implementation that
    happens to work today.
    """
    from_harvest = tmp_path / "from_harvest.py"
    assert (
        main(
            [
                "--landing",
                str(landing),
                "--offline",
                "--stores",
                str(STORES),
                "--vocabulary",
                str(from_harvest),
            ]
        )
        == 0
    )

    from_built = tmp_path / "from_built.py"
    assert (
        main(["--landing", str(landing), "--from-built", "--vocabulary", str(from_built)])
        == 0
    )

    assert from_built.read_text() == from_harvest.read_text()


def test_from_built_refuses_without_somewhere_to_write(landing: Path) -> None:
    """It produces exactly one thing, so being told nowhere to put it is an error."""
    assert main(["--landing", str(landing), "--from-built"]) == 2


def test_from_built_says_so_when_there_is_no_catalogue(tmp_path: Path) -> None:
    """An empty directory is a mistake worth a message rather than a traceback."""
    status = main(
        [
            "--landing",
            str(tmp_path),
            "--from-built",
            "--vocabulary",
            str(tmp_path / "out.py"),
        ]
    )

    assert status == 1
