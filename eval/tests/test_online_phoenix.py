"""The trace source: a backend's rows, as the turns the monitors already take.

Every one of these is about a *shape difference*, because that is the entire job
of an adapter and every one of them is a silent wrong answer if it is got wrong.
An attribute left nested is a citation nobody can find; a timestamp left as a
string is a latency the monitor reads as zero; an end time not written under the
key the reader looks for is *every* turn reading as fast enough, which is the
latency monitor reporting compliance because it has no data.

The HTTP itself is stubbed. What is being tested is the translation, and a test
that needed a Phoenix container to check that ``"2026-08-28T00:31:02+00:00"``
becomes an integer would be testing Docker.
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from chip_chat.eval.online import phoenix
from chip_chat.eval.online.signals import END_TIME

TRACE = "cf24f787fe124a1c37edbf2093d02c0c"


def _row(
    name: str,
    span_id: str,
    parent_id: str | None,
    attributes: Mapping[str, Any] | None = None,
    *,
    start: str = "2026-08-28T00:31:02.000000+00:00",
    end: str = "2026-08-28T00:31:04.500000+00:00",
) -> dict[str, Any]:
    """One row in the shape the backend's REST API documents."""
    return {
        "id": f"U3Bhbjo{span_id}",
        "name": name,
        "context": {"trace_id": TRACE, "span_id": span_id},
        "span_kind": "CHAIN",
        "parent_id": parent_id,
        "start_time": start,
        "end_time": end,
        "status_code": "UNSET",
        "attributes": dict(attributes or {}),
        "events": [],
    }


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub the one HTTP call, returning pages in order."""

    def install(*pages: dict[str, Any]) -> list[str]:
        seen: list[str] = []
        remaining = list(pages)

        def fake_get(
            base_url: str, path: str, query: Mapping[str, str], *, timeout: float
        ) -> Mapping[str, Any]:
            seen.append(f"{base_url}{path}?{json.dumps(dict(query), sort_keys=True)}")
            return remaining.pop(0) if remaining else {"data": []}

        monkeypatch.setattr(phoenix, "_get", fake_get)
        return seen

    return install


def test_a_trace_becomes_one_turn_carrying_what_the_visitor_said(answers: Any) -> None:
    answers(
        {
            "data": [
                _row(
                    "chat.turn",
                    "aaaa",
                    None,
                    {
                        "input": {"value": "Is the barbacoa gluten free?"},
                        "output": {"value": "No — it lists wheat."},
                        "session": {"id": "sess-1"},
                        "chip_chat": {"tokens": {"total": 10278}},
                    },
                ),
            ],
            "next_cursor": None,
        }
    )

    turns = phoenix.read_live_turns("http://backend")

    assert len(turns) == 1
    assert turns[0].trace_id == TRACE
    assert turns[0].message == "Is the barbacoa gluten free?"
    assert turns[0].reply == "No — it lists wheat."
    assert turns[0].session_id == "sess-1"
    assert turns[0].total_tokens == 10278


def test_nested_attributes_are_flattened_to_the_keys_the_readers_look_up() -> None:
    flat = phoenix._flatten({"llm": {"token_count": {"prompt": 100}}})

    assert flat["llm.token_count.prompt"] == 100


def test_flattening_keeps_an_attribute_whose_value_is_genuinely_an_object() -> None:
    """A flattener that consumed the mapping would delete data to tidy a shape."""
    flat = phoenix._flatten({"tool": {"parameters": {"item": "bowl"}}})

    assert flat["tool.parameters.item"] == "bowl"
    assert flat["tool.parameters"] == {"item": "bowl"}


def test_flat_dotted_attributes_survive_unchanged() -> None:
    """The deployed backend answers flat; the nested form is the one to defend
    against, not the one to require."""
    flat = phoenix._flatten({"input.value": "hello", "chip_chat.tokens.total": 5})

    assert flat == {"input.value": "hello", "chip_chat.tokens.total": 5}


def test_the_end_time_lands_under_the_key_the_latency_monitor_reads(
    answers: Any,
) -> None:
    """The failure this prevents is every turn reporting a duration of zero,
    which the latency monitor would read as every turn being fast enough."""
    answers({"data": [_row("chat.turn", "aaaa", None)], "next_cursor": None})

    turns = phoenix.read_live_turns("http://backend")

    assert turns[0].duration_ms == pytest.approx(2500.0)


def test_times_are_nanoseconds_because_the_duration_is_divided_by_a_million() -> None:
    nanos = phoenix._nanos("2026-08-28T00:31:02.000000+00:00")

    assert nanos is not None
    assert nanos % 1_000_000_000 == 0
    assert nanos > 1_700_000_000_000_000_000


def test_a_trace_with_no_root_in_the_window_is_dropped_rather_than_unreadable(
    answers: Any,
) -> None:
    """A turn whose root started before the window opened is a windowing
    artefact. Counting it as unreadable would put a permanent floor under the
    unreadable rate, which is the number that is supposed to mean #103."""
    answers(
        {
            "data": [_row("retriever.search", "bbbb", "aaaa")],
            "next_cursor": None,
        }
    )

    assert phoenix.read_live_turns("http://backend") == ()


def test_pagination_follows_the_cursor_until_the_backend_stops_offering_one(
    answers: Any,
) -> None:
    seen = answers(
        {"data": [_row("chat.turn", "aaaa", None)], "next_cursor": "page-2"},
        {"data": [_row("guard.budget_check", "bbbb", "aaaa")], "next_cursor": None},
    )

    turns = phoenix.read_live_turns("http://backend")

    assert len(seen) == 2
    assert "page-2" in seen[1]
    assert len(turns) == 1


def test_the_window_is_sent_to_the_backend_rather_than_filtered_here(
    answers: Any,
) -> None:
    """Reading a day of spans in order to keep twenty minutes of them would
    make the job's cost a function of the backend's whole history."""
    seen = answers({"data": [], "next_cursor": None})

    phoenix.read_live_turns("http://backend", lookback_minutes=20)

    assert "start_time" in seen[0]
    assert "end_time" in seen[0]


def test_a_malformed_row_raises_rather_than_shortening_the_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuing past a row whose shape moved would report a partial trace as a
    complete one, and a partial trace scores as a real finding."""
    monkeypatch.setattr(
        phoenix,
        "_get",
        lambda *a, **k: {"data": [{"name": "chat.turn"}], "next_cursor": None},
    )

    with pytest.raises(phoenix.PhoenixError):
        phoenix.read_live_turns("http://backend")


def test_a_backend_that_answers_without_data_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phoenix, "_get", lambda *a, **k: {"detail": "not found"})

    with pytest.raises(phoenix.PhoenixError):
        phoenix.read_live_turns("http://backend")


def test_the_adapter_writes_the_end_time_key_signals_agreed_on() -> None:
    """A literal here would be free to drift from the reader's constant."""
    span = phoenix._span(_row("chat.turn", "aaaa", None))

    assert END_TIME in span.attributes
