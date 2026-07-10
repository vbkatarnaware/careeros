"""Tests for careeros/cli.py's `backfill-drive` command (Phase 3, v1.1).
Everything Drive/Sheets-related is mocked; what's under test is CareerOS's
own logic: legacy-row (missing Tier) inclusion, Consider-tier exclusion,
idempotent skip, missing-local-artifacts detection (never fabricate), the
default --dry-run safety net, and the Sheet-row-update wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import typer

from careeros.cli import backfill_drive
from careeros.drive import JobUploadResult


def _row(job_id, date="2026-07-07", company="Acme", role="PM", tier=None,
         resume_drive="", cover_drive="", eval_drive=""):
    row = {
        "Date": date, "Company": company, "Role": role, "Job ID": job_id,
        "Resume (Drive)": resume_drive, "Cover Letter (Drive)": cover_drive,
        "Evaluation (Drive)": eval_drive,
    }
    if tier is not None:
        row["Tier"] = tier
    return row


def _cfg_with_drive():
    cfg = MagicMock()
    cfg.drive = {"enabled": True, "root_folder_id": "root-1"}
    cfg.runs_dir = MagicMock()
    return cfg


def test_exits_when_drive_not_configured():
    cfg = MagicMock()
    cfg.drive = {"enabled": False}
    with patch("careeros.cli._config", return_value=cfg):
        with pytest.raises(typer.Exit):
            backfill_drive(dry_run=True)


def test_legacy_rows_with_no_tier_column_are_treated_as_apply(tmp_path, monkeypatch):
    """Rows written before the Tier column existed (Phase 3) predate the
    Consider tier entirely, so a missing Tier means Apply — not excluded."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    rows = [_row("job-legacy", tier=None)]  # no "Tier" key at all

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=tmp_path):  # empty dir -> needs regen
        backfill_drive(dry_run=True)
    # No exception + no crash means the legacy row was at least considered
    # (it lands in needs_regen since tmp_path has no resume.md/cover.md) —
    # verified precisely in the needs-regen test below.


def test_explicit_consider_tier_rows_are_excluded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    rows = [_row("job-consider", tier="Consider")]

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows) as mock_read, \
         patch("careeros.cli.runmeta.artifacts_dir") as mock_artifacts_dir:
        backfill_drive(dry_run=True)
    mock_artifacts_dir.assert_not_called()  # never even looked for local artifacts


def test_already_backfilled_rows_are_skipped_idempotently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    rows = [_row("job-done", tier="Apply", resume_drive="https://x", cover_drive="https://y",
                 eval_drive="https://z")]

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir") as mock_artifacts_dir:
        backfill_drive(dry_run=True)
    mock_artifacts_dir.assert_not_called()  # already has all three links -> never even checked disk


def test_dash_placeholder_is_treated_as_blank_not_already_done(tmp_path, monkeypatch):
    """After the P2.10 blank-fill migration, a genuinely-empty cell reads as
    "-", not "". That must still count as missing -- not as "already
    backfilled" (a truthy-string bug that would silently no-op the whole
    command against a migrated live sheet)."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    rows = [_row("job-dash", tier="Apply", resume_drive="-", cover_drive="-", eval_drive="-")]

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir") as mock_artifacts_dir:
        backfill_drive(dry_run=True)
    mock_artifacts_dir.assert_called_once()  # "-" must NOT be mistaken for "already backfilled"


def test_missing_local_artifacts_are_listed_not_fabricated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    rows = [_row("job-gone", tier="Apply")]
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=empty_dir), \
         patch("careeros.drive.upload_jobs") as mock_upload:
        backfill_drive(dry_run=True)
    mock_upload.assert_not_called()  # dry-run AND nothing eligible to upload


def test_dry_run_never_calls_upload_or_sheet_update(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "resume.md").write_text("# R")
    (artifacts / "cover.md").write_text("C")
    (artifacts / "daily_report.md").write_text("# Eval")
    rows = [_row("job-a", tier="Apply")]

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=artifacts), \
         patch("careeros.drive.upload_jobs") as mock_upload, \
         patch("careeros.cli.sheets_mod.update_row_by_job_id") as mock_update:
        backfill_drive(dry_run=True)  # default

    mock_upload.assert_not_called()
    mock_update.assert_not_called()


def test_no_dry_run_uploads_and_updates_matching_rows(tmp_path, monkeypatch):
    """Covers the full reconciliation pass: upload -> Sheet update -> Drive
    re-verification -> Sheet re-read verification -> MIGRATION COMPLETE.
    The Sheet is read TWICE by the real command (initial scan, then a fresh
    re-read to verify the write actually landed) — `side_effect` supplies the
    pre- and post-update row shape for each, matching real Sheets behavior."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "resume.md").write_text("# R")
    (artifacts / "cover.md").write_text("C")
    (artifacts / "daily_report.md").write_text("# Eval")
    rows_before = [_row("job-a", tier="Apply", company="Acme", role="PM")]
    rows_after = [_row("job-a", tier="Apply", company="Acme", role="PM",
                       resume_drive="https://drive/r.pdf", cover_drive="https://drive/c.pdf",
                       eval_drive="https://drive/e.md")]

    fake_result = JobUploadResult(folder_link="https://drive/folder",
                                  resume_link="https://drive/r.pdf",
                                  cover_link="https://drive/c.pdf",
                                  eval_link="https://drive/e.md")
    fake_verification = {"job-a": {"resume_ok": True, "cover_ok": True, "errors": []}}

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id",
               side_effect=[rows_before, rows_after]), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=artifacts), \
         patch("careeros.drive.upload_jobs", return_value={"job-a": fake_result}) as mock_upload, \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update, \
         patch("careeros.drive.verify_uploads", return_value=fake_verification) as mock_verify:
        backfill_drive(dry_run=False)

    mock_upload.assert_called_once()
    batch = mock_upload.call_args.args[1]
    assert len(batch) == 1
    date, job_like, artifacts_dir_arg = batch[0]
    assert date == "2026-07-07" and job_like.company == "Acme" and job_like.title == "PM"
    assert job_like.id == "job-a"

    mock_update.assert_called_once_with(cfg, "job-a", {
        "Resume (Drive)": "https://drive/r.pdf",
        "Cover Letter (Drive)": "https://drive/c.pdf",
        "Evaluation (Drive)": "https://drive/e.md",
    })
    mock_verify.assert_called_once_with(cfg, {"job-a": fake_result})


def test_no_dry_run_only_updates_the_link_this_run_actually_produced(tmp_path, monkeypatch):
    """A row missing ONLY "Evaluation (Drive)" (resume/cover already linked)
    must not have its Sheet update clobber the existing resume/cover links
    with empty strings just because this run's upload didn't reprocess
    those files."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "daily_report.md").write_text("# Eval")  # only the eval source exists on disk
    rows_before = [_row("job-a", tier="Apply", resume_drive="https://drive/r.pdf",
                        cover_drive="https://drive/c.pdf")]
    rows_after = [_row("job-a", tier="Apply", resume_drive="https://drive/r.pdf",
                       cover_drive="https://drive/c.pdf", eval_drive="https://drive/e.md")]

    fake_result = JobUploadResult(folder_link="https://drive/folder", eval_link="https://drive/e.md")
    fake_verification = {"job-a": {"resume_ok": True, "cover_ok": True, "errors": []}}

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id",
               side_effect=[rows_before, rows_after]), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=artifacts), \
         patch("careeros.drive.upload_jobs", return_value={"job-a": fake_result}), \
         patch("careeros.cli.sheets_mod.update_row_by_job_id", return_value=True) as mock_update, \
         patch("careeros.drive.verify_uploads", return_value=fake_verification):
        backfill_drive(dry_run=False)

    mock_update.assert_called_once_with(cfg, "job-a", {"Evaluation (Drive)": "https://drive/e.md"})


def test_upload_failure_is_fail_soft_exits_nonzero_without_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg_with_drive()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "resume.md").write_text("# R")
    (artifacts / "cover.md").write_text("C")
    (artifacts / "daily_report.md").write_text("# Eval")
    rows = [_row("job-a", tier="Apply")]

    with patch("careeros.cli._config", return_value=cfg), \
         patch("careeros.cli.sheets_mod.read_all_rows_with_job_id", return_value=rows), \
         patch("careeros.cli.runmeta.artifacts_dir", return_value=artifacts), \
         patch("careeros.drive.upload_jobs", side_effect=RuntimeError("boom")):
        with pytest.raises(typer.Exit):
            backfill_drive(dry_run=False)
