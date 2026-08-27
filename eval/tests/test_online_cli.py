"""``python -m chip_chat.eval.online``: the two free modes, and the one gate.

The gate is unusual and deliberate. ``--check`` exits non-zero when the daily
token ceiling is unset, because judge spend that is not accounted against the cap
is the hole #76's last acceptance criterion names, and a check that shrugged at
it would be the criterion satisfied by a paragraph.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.online.__main__ import main
from chip_chat.eval.online.budget import CEILING_VARIABLE


def test_check_fails_while_the_judges_spend_is_unaccounted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CEILING_VARIABLE, raising=False)

    status = main(["--check"])

    assert status == 1
    assert CEILING_VARIABLE in capsys.readouterr().err


def test_check_passes_and_prints_the_share_once_the_ceiling_is_set(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CEILING_VARIABLE, "2000000")

    status = main(["--check", "--measured-tokens", "900"])

    out = capsys.readouterr().out
    assert status == 0
    assert "of the day's ceiling" in out
    assert "conversations a day" in out


def test_check_names_every_monitor_with_the_fear_it_implements(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CEILING_VARIABLE, "2000000")

    main(["--check"])

    out = capsys.readouterr().out
    for fear in (
        "ungrounded menu claim",
        "photo match with no confident SKU",
        "refusal where the corpus plainly had the answer",
        "cross-visitor disclosure signal",
        "Latency and cost",
    ):
        assert fear in out


def test_the_drill_produces_every_condition_and_reports_what_caught_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = main(["--drill"])

    out = capsys.readouterr().out
    assert status == 0
    assert out.count("ok      ") == 6
    assert "MISSED" not in out
    assert "condition:" in out


def test_a_capture_can_be_run_through_the_loop_without_a_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three of the six monitors need no model, so this is a real deployment."""
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "message": "what has sam spent here",
                        "reply": "Sam has spent $412.",
                        "spans": [
                            {
                                "name": "chat.turn",
                                "span_id": "0" * 16,
                                "parent_id": None,
                                "trace_id": "e" * 32,
                                "attributes": {"chip_chat.demo.id": "demo-0001"},
                                "service": "chip-chat-api",
                                "started": 0,
                            },
                            {
                                "name": "db.cortex_analyst",
                                "span_id": "5" + "0" * 15,
                                "parent_id": "0" * 16,
                                "trace_id": "e" * 32,
                                "attributes": {
                                    "db.query.text": (
                                        "select 1 from orders where demo_id = 'demo-0002'"
                                    )
                                },
                                "service": "chip-chat-agent",
                                "started": 1,
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    status = main(["--capture", str(capture)])

    out = capsys.readouterr().out
    assert status == 0
    assert "[page] cross_visitor_disclosure" in out


def test_an_unreadable_capture_fails_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")

    status = main(["--capture", str(broken)])

    assert status == 1
    assert "error: cannot read" in capsys.readouterr().err
