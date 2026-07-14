"""Tests for careeros/cli/'s `sheets append` command (P2.10 wiring): joins
drive_links.json + apply_status.json into the new Drive-link Sheet columns,
including each of the specific status labels (see cli.py's `_STATUS_LABELS`)
for an Apply-tier job whose application form wasn't automatically readable.
sheets.py's own append_rows/job_to_row are exercised elsewhere
(test_sheets.py) -- this file is only about the JOIN logic cli.py performs
before calling them."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from careeros import runmeta
from careeros.cli import (
    STATUS_MANUAL_REQUIRED,
    _STATUS_LABELS,
    sheets_append,
    sheets_migrate,
    sheets_sync_status,
)
from careeros.config import Config
from careeros.models import dumps
from careeros.sheets import SHEET_HEADERS
from careeros.tests.conftest import make_job


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={"enabled": True}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _minimal_eval(job_id: str, score: float = 4.3) -> dict:
    return {
        "id": job_id, "score": score, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4, "seniority_fit": 4, "skills_match": 4, "domain": 4, "logistics": 4},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h",
    }


def _seed(cfg, date, jobs, apply_evals=None, consider_evals=None):
    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "selected.json", "w") as f:
        f.write(dumps(apply_evals or []))
    with open(select_dir / "consider.json", "w") as f:
        f.write(dumps(consider_evals or []))
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))


def _run_and_capture_rows(cfg, date):
    captured = {}

    def fake_append_rows(cfg_arg, rows):
        captured["rows"] = rows

    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.append_rows", side_effect=fake_append_rows), \
         patch("careeros.cli.sheets_cmds.append_seen_ids"):
        sheets_append(date=date)
    return captured["rows"]


def test_noop_when_sheets_disabled(tmp_path, monkeypatch, capsys):
    """Sheets is optional (v1.6.0, mirroring Drive) — a local-mode config
    with sheets.enabled: false must never try to reach the Sheets API."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(sheets={"enabled": False})
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.append_rows") as mock_append:
        sheets_append(date="2026-07-10")
    mock_append.assert_not_called()
    assert "disabled" in capsys.readouterr().out


def test_apply_row_shows_manual_required_label_when_form_unreadable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed(cfg, "2026-07-10", [job], apply_evals=[_minimal_eval("job-1")])

    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "manual_required"}))

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    assert (
        rows[0][SHEET_HEADERS.index("Application Answers (Drive)")]
        == _STATUS_LABELS[STATUS_MANUAL_REQUIRED]
    )


@pytest.mark.parametrize(
    "status_code, expected_label",
    [
        ("login_required", "🔒 Login Required"),
        ("closed", "❌ Closed"),
        ("no_essay_questions", "📄 No Essay Questions"),
        ("network_error", "🌐 Network Error"),
    ],
)
def test_apply_row_shows_specific_status_label(tmp_path, monkeypatch, status_code, expected_label):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed(cfg, "2026-07-10", [job], apply_evals=[_minimal_eval("job-1")])

    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": status_code}))

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    assert rows[0][SHEET_HEADERS.index("Application Answers (Drive)")] == expected_label


def test_apply_row_shows_playwright_missing_label_with_install_instructions(tmp_path, monkeypatch):
    """The Playwright-missing label carries the actual install command
    inline, not just a bare status word -- the third of the three named
    browser.py improvements (the extra should be 'easy to install and
    clearly documented'), surfaced right where the candidate will see it."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed(cfg, "2026-07-10", [job], apply_evals=[_minimal_eval("job-1")])

    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "playwright_missing"}))

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    cell = rows[0][SHEET_HEADERS.index("Application Answers (Drive)")]
    assert cell.startswith("⚙️ Playwright Missing")
    assert "playwright install chromium" in cell


def test_apply_row_uses_answers_drive_link_when_generated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed(cfg, "2026-07-10", [job], apply_evals=[_minimal_eval("job-1")])

    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "generated"}))
    with open(run_dir / "drive_links.json", "w") as f:
        f.write(dumps({"job-1": {
            "resume": "https://drive/r.pdf", "cover": "https://drive/c.pdf",
            "eval": "https://drive/e.md", "deep_report": "",
            "answers": "https://drive/a.pdf", "warnings": [],
        }}))

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    row = rows[0]
    assert row[SHEET_HEADERS.index("Application Answers (Drive)")] == "https://drive/a.pdf"
    assert row[SHEET_HEADERS.index("Resume (Drive)")] == "https://drive/r.pdf"
    assert row[SHEET_HEADERS.index("Cover Letter (Drive)")] == "https://drive/c.pdf"
    assert row[SHEET_HEADERS.index("Evaluation (Drive)")] == "https://drive/e.md"
    assert row[SHEET_HEADERS.index("Deep Report (Drive)")] == "-"


def test_apply_row_blank_answers_when_no_apply_status_file(tmp_path, monkeypatch):
    """No apply_status.json at all (apply stage never ran, e.g. an older
    run) -- Application Answers cell is just blank, not an error."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed(cfg, "2026-07-10", [job], apply_evals=[_minimal_eval("job-1")])

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    assert rows[0][SHEET_HEADERS.index("Application Answers (Drive)")] == "-"


def test_consider_row_has_blank_drive_cells_regardless_of_apply_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-2")
    _seed(cfg, "2026-07-10", [job], consider_evals=[_minimal_eval("job-2", score=3.7)])

    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-2": "generated"}))  # would only apply to Apply-tier rows

    rows = _run_and_capture_rows(cfg, "2026-07-10")
    row = rows[0]
    assert row[SHEET_HEADERS.index("Tier")] == "Consider"
    assert row[SHEET_HEADERS.index("Application Answers (Drive)")] == "-"
    assert row[SHEET_HEADERS.index("Resume (Drive)")] == "-"


# ── sheets migrate (P2.10) ────────────────────────────────────────────────

def test_sheets_migrate_reports_removed_and_added(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.migrate",
               return_value={"removed": ["Drive Folder"], "added": ["Deep Report (Drive)"]}):
        sheets_migrate()
    out = capsys.readouterr().out
    assert "Drive Folder" in out
    assert "Deep Report (Drive)" in out


def test_sheets_migrate_reports_already_current(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.migrate", return_value={"removed": [], "added": []}):
        sheets_migrate()
    out = capsys.readouterr().out
    assert "already up to date" in out


# ── sheets sync-status (P2.11) ───────────────────────────────────────────

def test_sync_status_patches_existing_row_with_specific_label(tmp_path, monkeypatch, capsys):
    """A job reclassified from the old generic manual_required into a
    specific status must get its EXISTING Sheet row patched, not a new row
    appended."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "login_required", "job-2": "closed"}))

    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update:
        sheets_sync_status(date="2026-07-10")

    calls = {c.args[1]: c.args[2] for c in mock_update.call_args_list}
    assert calls["job-1"] == {"Application Answers (Drive)": _STATUS_LABELS["login_required"]}
    assert calls["job-2"] == {"Application Answers (Drive)": _STATUS_LABELS["closed"]}
    out = capsys.readouterr().out
    assert "2 row(s) updated" in out


def test_sync_status_skips_generated_jobs(tmp_path, monkeypatch, capsys):
    """Regression: `generated` jobs must NEVER be touched by sync-status.
    `drive_links.json` is only refreshed by the full `careeros drive` batch
    command, not by `careeros publish` (which patches the Sheet directly) --
    so re-deriving a `generated` job's cell from a stale/missing
    drive_links.json here previously overwrote a correct link `publish` had
    just set with a blank. `publish <job-id>` is the only thing allowed to
    touch a `generated` job's Application Answers cell."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "generated", "job-2": "closed"}))
    # Deliberately stale/missing "answers" key -- the real bug trigger.
    with open(run_dir / "drive_links.json", "w") as f:
        f.write(dumps({"job-1": {"resume": "https://drive/r.pdf"}}))

    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update:
        sheets_sync_status(date="2026-07-10")

    called_job_ids = [c.args[1] for c in mock_update.call_args_list]
    assert "job-1" not in called_job_ids
    assert called_job_ids == ["job-2"]
    out = capsys.readouterr().out
    assert "1 'generated' job(s) skipped" in out
    assert "careeros publish" in out


def test_sync_status_reports_jobs_not_found_in_sheet(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    run_dir = runmeta.run_dir(cfg.runs_dir, "2026-07-10")
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "apply_status.json", "w") as f:
        f.write(dumps({"job-1": "closed"}))

    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=False):
        sheets_sync_status(date="2026-07-10")

    out = capsys.readouterr().out
    assert "0 row(s) updated" in out
    assert "job-1" in out


def test_sync_status_noop_when_no_apply_status_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    runmeta.run_dir(cfg.runs_dir, "2026-07-10").mkdir(parents=True, exist_ok=True)

    with patch("careeros.cli.sheets_cmds._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id") as mock_update:
        sheets_sync_status(date="2026-07-10")

    mock_update.assert_not_called()
    out = capsys.readouterr().out
    assert "nothing to sync" in out
