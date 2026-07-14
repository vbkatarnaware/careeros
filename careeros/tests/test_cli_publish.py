"""Tests for careeros/cli/'s `publish` command (P2.10): upload one job's
current artifacts (whichever exist) to Drive and patch just that Sheet row.
Drive and Sheets are both mocked -- what's under test is CareerOS's own
wiring: which JobUploadResult fields map to which Sheet column, and the
fail-soft/fail-loud boundaries (Drive disabled, job not found, nothing
uploaded, Drive error, row not found)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer

from careeros import runmeta
from careeros.cli import publish
from careeros.config import Config
from careeros.drive import DriveError, JobUploadResult
from careeros.models import dumps
from careeros.tests.conftest import make_job


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={"enabled": True}, apify={}, api={}, fx_rates={},
        drive={"enabled": True, "root_folder_id": "root-1"},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed_job(cfg, date, job):
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([job.to_dict()]))


def test_noop_when_drive_disabled(tmp_path, monkeypatch, capsys):
    """Drive is optional (mirrors sheets append/drive upload's own pattern) —
    local mode must never hard-fail; artifacts are already on disk."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(drive={"enabled": False})
    with patch("careeros.cli.perjob._config", return_value=cfg):
        publish("job-1", date="2026-07-10")  # must not raise
    assert "nothing to upload" in capsys.readouterr().out


def test_exits_when_no_normalize_output_for_date(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.perjob._config", return_value=cfg):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_exits_when_job_id_not_in_normalize_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    _seed_job(cfg, "2026-07-10", make_job(id="some-other-job"))
    with patch("careeros.cli.perjob._config", return_value=cfg):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_uploads_to_drive_but_skips_sheet_patch_when_sheets_disabled(tmp_path, monkeypatch, capsys):
    """Drive-only mode (drive.enabled: true, sheets.enabled: false) — a
    combination only possible since Sheets became independently optional —
    must upload to Drive but never call the Sheets API."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(sheets={"enabled": False})
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(folder_link="https://drive/folder", eval_link="https://drive/eval.md")
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id") as mock_update:
        publish("job-1", date="2026-07-10")

    mock_update.assert_not_called()
    assert "Sheets is disabled" in capsys.readouterr().out


def test_publish_uploads_and_patches_only_available_links(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(
        folder_link="https://drive/folder",
        deep_report_link="https://drive/deep.md",
        eval_link="https://drive/eval.md",
    )
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}) as mock_upload, \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update:
        publish("job-1", date="2026-07-10")

    mock_upload.assert_called_once()
    batch = mock_upload.call_args.args[1]
    assert len(batch) == 1
    date, job_arg, artifacts_dir_arg = batch[0]
    assert date == "2026-07-10" and job_arg.id == "job-1"

    mock_update.assert_called_once_with(cfg, "job-1", {
        "Evaluation (Drive)": "https://drive/eval.md",
        "Deep Report (Drive)": "https://drive/deep.md",
    })


def test_publish_includes_answers_resume_cover_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(
        folder_link="https://drive/folder",
        resume_link="https://drive/r.pdf", cover_link="https://drive/c.pdf",
        answers_link="https://drive/a.pdf",
    )
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update:
        publish("job-1", date="2026-07-10")

    mock_update.assert_called_once_with(cfg, "job-1", {
        "Application Answers (Drive)": "https://drive/a.pdf",
        "Resume (Drive)": "https://drive/r.pdf",
        "Cover Letter (Drive)": "https://drive/c.pdf",
    })


def test_publish_exits_when_nothing_uploaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={}):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_publish_exits_when_job_upload_result_has_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(folder_link="", error="upload blew up")
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_publish_exits_on_drive_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", side_effect=DriveError("boom")):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_publish_exits_when_row_not_found_in_sheet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(folder_link="https://drive/folder", eval_link="https://drive/eval.md")
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=False):
        with pytest.raises(typer.Exit):
            publish("job-1", date="2026-07-10")


def test_publish_succeeds_with_nothing_new_to_link(tmp_path, monkeypatch, capsys):
    """A job that uploaded (e.g. only run.json-level metadata) but produced
    no per-artifact link at all is not an error -- just nothing to patch."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_job(cfg, "2026-07-10", job)

    fake_result = JobUploadResult(folder_link="https://drive/folder")
    with patch("careeros.cli.perjob._config", return_value=cfg), \
         patch("careeros.drive.upload_jobs", return_value={"job-1": fake_result}), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id") as mock_update:
        publish("job-1", date="2026-07-10")

    mock_update.assert_not_called()
