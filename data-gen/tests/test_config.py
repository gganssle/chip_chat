"""That the parameters are read, and that a wrong one stops the run.

Issue #25 asks for parameters in a config file "so the population can be
retuned without archaeology", and the retuning loop only closes if the numbers
in the file are the numbers that ran. So every case below is a refusal rather
than a correction: a share that does not sum to one, a cadence of an hour, a
timezone that does not exist. Silently clamping any of them would mean the
population you tuned and the population you got are two different things and
nothing told you.

The first test is the important one. It loads the file the package actually
ships, so the shipped parameters cannot drift out of what the reader accepts.
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest
from population_fixtures import PACKAGED

from chip_chat.data_gen import load_config
from chip_chat.data_gen.config import MINIMUM_CADENCE_DAYS
from chip_chat.data_gen.errors import ConfigError


def edited(tmp_path: Path, change: dict[str, Any]) -> Path:
    """Write the shipped config with one table replaced, and return its path."""
    raw = tomllib.loads(PACKAGED.read_text(encoding="utf-8"))
    for path, value in change.items():
        target: Any = raw
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if key.isdigit() else target[key]
        last = keys[-1]
        if last.isdigit():
            target[int(last)] = value
        else:
            target[last] = value
    written = tmp_path / "population.toml"
    written.write_text(_dump(raw), encoding="utf-8")
    return written


def _dump(raw: Any, prefix: str = "") -> str:
    """Serialise a parsed config back to TOML. Enough of it for these tests."""
    import json

    lines: list[str] = []
    scalars = {k: v for k, v in raw.items() if not isinstance(v, dict | list)}
    scalars |= {
        k: v
        for k, v in raw.items()
        if isinstance(v, list) and not any(isinstance(item, dict) for item in v)
    }
    for key, value in scalars.items():
        lines.append(f"{key} = {json.dumps(value)}")
    for key, value in raw.items():
        if isinstance(value, dict):
            lines.append(f"\n[{prefix}{key}]")
            lines.append(_dump(value, f"{prefix}{key}."))
        elif isinstance(value, list) and any(isinstance(item, dict) for item in value):
            for item in value:
                lines.append(f"\n[[{prefix}{key}]]")
                lines.append(_dump(item, f"{prefix}{key}."))
    return "\n".join(lines)


def test_the_shipped_parameters_load() -> None:
    """The file in the package is the file the reader accepts."""
    config = load_config()

    assert config.customers == 500
    assert config.stores == 30
    assert config.months == 18
    assert abs(sum(spec.share for spec in config.personas) - 1.0) < 1e-9
    assert {spec.persona_id for spec in config.personas} >= {
        "regular",
        "lapsed",
        "explorer",
        "office_manager",
    }


def test_the_shipped_parameters_round_trip_through_the_test_writer(
    tmp_path: Path,
) -> None:
    """So a failure below is the edit, and never the writer that made it."""
    assert load_config(edited(tmp_path, {})) == load_config()


def test_a_named_file_replaces_the_packaged_one(tmp_path: Path) -> None:
    """``--config`` is how the population is retuned without editing the wheel."""
    config = load_config(edited(tmp_path, {"population.customers": 12}))

    assert config.customers == 12


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nothing.toml")


def test_unreadable_toml_is_named(tmp_path: Path) -> None:
    broken = tmp_path / "population.toml"
    broken.write_text("[population\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="not readable TOML"):
        load_config(broken)


def test_persona_shares_must_sum_to_one(tmp_path: Path) -> None:
    """Otherwise the population is a different size than the file describes."""
    with pytest.raises(ConfigError, match=r"share must sum to 1\.0"):
        load_config(edited(tmp_path, {"personas.0.share": 0.9}))


def test_a_persona_may_not_be_faster_than_a_day(tmp_path: Path) -> None:
    """A guard, not a preference: a tenth of a day is six thousand orders."""
    with pytest.raises(ConfigError, match="cadence_days must be at least"):
        load_config(
            edited(tmp_path, {"personas.0.cadence_days": MINIMUM_CADENCE_DAYS / 10})
        )


def test_a_lapse_may_not_happen_before_the_customer_starts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be after"):
        load_config(edited(tmp_path, {"personas.1.active_until_share": 0.0}))


def test_an_hour_distribution_must_be_a_distribution(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"weights must sum to 1\.0"):
        load_config(
            edited(tmp_path, {"timing.weekday_lunch.weights": [0.1] * 6}),
        )


def test_an_hour_must_be_an_hour(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not an hour"):
        load_config(
            edited(tmp_path, {"timing.weekday_lunch.hours": [10, 11, 12, 13, 14, 26]})
        )


def test_a_timezone_must_exist(tmp_path: Path) -> None:
    """Caught here rather than at the four hundredth customer."""
    with pytest.raises(ConfigError, match="not a timezone"):
        load_config(edited(tmp_path, {"timing.store_timezones.CA": "Mars/Olympus"}))


def test_a_naive_instant_is_refused(tmp_path: Path) -> None:
    """A window without a zone is a different window on a different machine."""
    with pytest.raises(ConfigError, match="must carry a timezone"):
        load_config(edited(tmp_path, {"population.ends_at": "2026-08-01T00:00:00"}))


def test_settled_statuses_must_be_statuses(tmp_path: Path) -> None:
    """Otherwise points are earned by a status no order can reach."""
    with pytest.raises(ConfigError, match="settled_statuses names"):
        load_config(edited(tmp_path, {"orders.settled_statuses": ["SETTLED"]}))


def test_there_must_be_room_for_every_archetype(tmp_path: Path) -> None:
    """Fewer customers than archetypes means behaviour nobody exhibits."""
    with pytest.raises(ConfigError, match="every archetype must reach"):
        load_config(edited(tmp_path, {"population.customers": 3}))


def test_a_weekday_must_be_spelled_the_way_the_catalogue_spells_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="must hold names from"):
        load_config(edited(tmp_path, {"personas.0.preferred_weekdays": ["Tues"]}))


def test_the_archetypes_are_reachable_by_identifier() -> None:
    config = load_config()

    assert config.persona("explorer").label == "The Explorer"
    with pytest.raises(KeyError):
        config.persona("nobody")
