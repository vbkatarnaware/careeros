"""Tests for careeros/drive.py — the P2.6 optional Drive backup module.
Everything here is mocked (no real Google API calls, no real OAuth flow);
what's under test is CareerOS's own logic: config validation, folder
find-or-create branching, and the upload_run orchestration/return shape. The
module's fail-soft contract (any failure -> DriveError, caught by cli.py) is
exercised at the config-validation layer, which needs no network access."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from careeros.drive import DriveError, _find_or_create_folder, _get_credentials, upload_run
from careeros.tests.conftest import make_job


def _cfg(**drive_overrides):
    drive = {"enabled": True, "client_secret_path": None, "token_path": None, "root_folder_id": None}
    drive.update(drive_overrides)
    cfg = MagicMock()
    cfg.drive = drive
    return cfg


# ── config validation (reachable without any real Drive/OAuth call) ─────

def test_get_credentials_raises_when_client_secret_path_missing():
    with pytest.raises(DriveError, match="client_secret_path not set"):
        _get_credentials(_cfg(client_secret_path=None))


def test_get_credentials_raises_when_client_secret_file_does_not_exist(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(DriveError, match="does not exist"):
        _get_credentials(_cfg(client_secret_path=str(missing)))


def test_upload_run_raises_when_root_folder_id_missing(tmp_path):
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id=None)
    with patch("careeros.drive._drive_service", return_value=MagicMock()):
        with pytest.raises(DriveError, match="root_folder_id not set"):
            upload_run(cfg, "2026-07-08", tmp_path / "run.json", tmp_path / "summary.md", [])


# ── _find_or_create_folder: query construction + branching ──────────────

def test_find_or_create_folder_returns_existing_id_when_found():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123", "name": "Acme"}]
    }
    folder_id = _find_or_create_folder(service, "Acme", "parent-1")
    assert folder_id == "existing-123"
    service.files.return_value.create.assert_not_called()


def test_find_or_create_folder_creates_when_not_found():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.return_value = {"id": "new-456"}
    folder_id = _find_or_create_folder(service, "Acme", "parent-1")
    assert folder_id == "new-456"
    create_kwargs = service.files.return_value.create.call_args.kwargs
    assert create_kwargs["body"]["name"] == "Acme"
    assert create_kwargs["body"]["parents"] == ["parent-1"]
    assert create_kwargs["body"]["mimeType"] == "application/vnd.google-apps.folder"


def test_find_or_create_folder_escapes_single_quotes_in_name():
    """A company name with an apostrophe (e.g. "Bjak's") must not break the
    Drive API query string."""
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.return_value = {"id": "x"}
    _find_or_create_folder(service, "O'Reilly", "parent-1")
    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "O\\'Reilly" in query


# ── upload_run: orchestration + return shape ─────────────────────────────

def test_upload_run_returns_folder_link_per_selected_job(tmp_path):
    run_json = tmp_path / "run.json"
    run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"
    summary_md.write_text("# summary")
    artifacts_dir = tmp_path / "artifacts" / "job-1"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "resume.md").write_text("resume content")
    (artifacts_dir / "cover.md").write_text("cover content")
    (artifacts_dir / "daily_report.md").write_text("report content")

    job = make_job(id="job-1", company="Bjak")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=MagicMock()), \
         patch("careeros.drive._find_or_create_folder", side_effect=["date-folder", "company-folder"]), \
         patch("careeros.drive._upload_text_file"):
        links = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert links == {"job-1": "https://drive.google.com/drive/folders/company-folder"}


def test_upload_run_skips_missing_artifact_files_without_failing(tmp_path):
    """A selected job whose resume/cover somehow isn't on disk yet must not
    crash the whole upload — the job still gets a folder link."""
    run_json = tmp_path / "run.json"
    run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"
    summary_md.write_text("# summary")
    artifacts_dir = tmp_path / "artifacts" / "job-1"  # deliberately not created

    job = make_job(id="job-1", company="Bjak")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=MagicMock()), \
         patch("careeros.drive._find_or_create_folder", side_effect=["date-folder", "company-folder"]), \
         patch("careeros.drive._upload_text_file") as mock_upload:
        links = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert links == {"job-1": "https://drive.google.com/drive/folders/company-folder"}
    # run.json + summary.md DO exist and upload; none of the per-job
    # resume/cover/report files do, so only those 2 calls happen.
    assert mock_upload.call_count == 2


def test_upload_run_empty_selected_jobs_still_uploads_run_json_and_summary(tmp_path):
    run_json = tmp_path / "run.json"
    run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"
    summary_md.write_text("# summary")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=MagicMock()), \
         patch("careeros.drive._find_or_create_folder", return_value="date-folder"), \
         patch("careeros.drive._upload_text_file") as mock_upload:
        links = upload_run(cfg, "2026-07-08", run_json, summary_md, [])

    assert links == {}
    assert mock_upload.call_count == 2  # run.json + summary.md
