"""Tests for `careeros sheets sync-outcomes` (careeros/cli/sheets_cmds.py) —
reading the Sheet's Status column into .careeros/learning/outcomes.jsonl,
the un-gameable outcome signal (see careeros/pipeline/ledger.py)."""

from __future__ import annotations

import json
from unittest.mock import patch

from careeros.cli.sheets_cmds import sheets_sync_outcomes
from careeros.config import Config


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={"enabled": True}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _row(job_id, status, tier="Apply", score="4.5", **overrides):
    r = {"Job ID": job_id, "Status": status, "Tier": tier, "Score": score,
         "Date": "2026-07-31", "Company": "Acme", "Role": "PM"}
    r.update(overrides)
    return r


def test_noop_when_sheets_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(sheets={"enabled": False})
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id") as mock_read:
        sheets_sync_outcomes()
    mock_read.assert_not_called()
    assert "disabled" in capsys.readouterr().out


def test_writes_outcomes_jsonl_from_sheet_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    rows = [
        _row("job-1", "Interview"),
        _row("job-2", "Not Applied", tier="Consider", score="3.6"),
        _row("job-3", "-", score="-"),  # blank cells
    ]
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows):
        sheets_sync_outcomes()

    outcomes_path = cfg.careeros_dir / "learning" / "outcomes.jsonl"
    lines = [json.loads(l) for l in outcomes_path.read_text().splitlines()]
    by_id = {o["job_id"]: o for o in lines}

    assert by_id["job-1"]["status"] == "Interview"
    assert by_id["job-1"]["score"] == 4.5
    assert by_id["job-3"]["status"] == "Not Applied"  # "-" treated as default
    assert by_id["job-3"]["score"] is None  # "-" treated as no score


def test_skips_rows_with_no_job_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    rows = [_row("job-1", "Applied"), _row("-", "Applied"), _row("", "Applied")]
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows):
        sheets_sync_outcomes()

    outcomes_path = cfg.careeros_dir / "learning" / "outcomes.jsonl"
    lines = [json.loads(l) for l in outcomes_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["job_id"] == "job-1"


def test_is_idempotent_overwriting_from_current_sheet_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=[_row("job-1", "Applied")]):
        sheets_sync_outcomes()
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=[_row("job-1", "Rejected")]):
        sheets_sync_outcomes()

    outcomes_path = cfg.careeros_dir / "learning" / "outcomes.jsonl"
    lines = [json.loads(l) for l in outcomes_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["status"] == "Rejected"
