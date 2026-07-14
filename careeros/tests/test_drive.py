"""Tests for careeros/drive.py — the Drive backup module (P2.6, extended in
Phase 3/v1.1 for flat layout + PDF + idempotent per-file upload). Everything
here is mocked (no real Google API calls, no real OAuth flow, no real PDF
rendering — `render_markdown_to_pdf` is patched so these tests don't depend
on the optional [pdf] extra being installed); what's under test is
CareerOS's own logic: config validation, folder find-or-create, the
idempotent create-or-update-by-name + collision-disambiguation dance, and
the upload_run orchestration/return shape."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from careeros.drive import (
    DriveError,
    JobUploadResult,
    _find_file_by_name,
    _find_or_create_folder,
    _get_credentials,
    _upload_bytes,
    upload_jobs,
    upload_run,
    verify_uploads,
)
from careeros.tests.conftest import make_job


def _cfg(**drive_overrides):
    drive = {"enabled": True, "client_secret_path": None, "token_path": None,
             "root_folder_id": None, "date_subfolder": False}
    drive.update(drive_overrides)
    cfg = MagicMock()
    cfg.drive = drive
    return cfg


class _FakeDriveService:
    """A minimal in-memory fake of the pieces of the Drive v3 `files()`
    resource this module actually calls (list/create/update), so tests can
    exercise the real create-vs-update-vs-disambiguate logic in drive.py
    without either a live API or a wall of chained MagicMock side_effects."""

    def __init__(self):
        self._next_id = 1
        self.store: dict[str, dict] = {}  # file_id -> {name, parents, appProperties, media}

    def files(self):
        return self

    def list(self, q, fields="files(id, name)"):
        # Parse the query shapes this module builds: name (exact or
        # "contains") + parent (+ optional mimeType).
        is_contains = "name contains '" in q
        needle_key = "name contains '" if is_contains else "name = '"
        needle = q.split(needle_key)[1].split("'")[0].replace("\\'", "'")
        parent = q.split("'")[-2] if q.count("'") >= 4 else None
        is_folder_query = "mimeType = 'application/vnd.google-apps.folder'" in q
        matches = []
        for fid, rec in self.store.items():
            if is_contains:
                if needle not in rec["name"]:
                    continue
            elif rec["name"] != needle:
                continue
            if parent not in rec["parents"]:
                continue
            if is_folder_query and rec.get("mimeType") != "application/vnd.google-apps.folder":
                continue
            matches.append({"id": fid, "name": rec["name"], "appProperties": rec.get("appProperties", {})})
        return _Exec({"files": matches})

    def create(self, body, media_body=None, fields=""):
        fid = f"f{self._next_id}"
        self._next_id += 1
        self.store[fid] = {
            "name": body["name"], "parents": body["parents"],
            "appProperties": body.get("appProperties", {}),
            "mimeType": body.get("mimeType"), "media": media_body,
        }
        return _Exec({"id": fid, "webViewLink": f"https://drive.google.com/file/d/{fid}/view"})

    def update(self, fileId, media_body=None, fields=""):
        self.store[fileId]["media"] = media_body
        return _Exec({"id": fileId, "webViewLink": f"https://drive.google.com/file/d/{fileId}/view"})

    def get(self, fileId, fields=""):
        if fileId not in self.store:
            raise KeyError(f"no such file: {fileId}")
        return _Exec({"id": fileId, "trashed": self.store[fileId].get("trashed", False)})

    def delete(self, fileId):
        del self.store[fileId]
        return _Exec(None)


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


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


# ── _find_or_create_folder: query construction + branching (unchanged) ──

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


# ── _upload_bytes: idempotent create/update + collision disambiguation ──

def test_upload_bytes_creates_new_file_when_none_exists():
    service = _FakeDriveService()
    file_id, link = _upload_bytes(service, MagicMock, "Acme - PM - Resume.pdf", b"%PDF-1", "application/pdf",
                                  "parent-1", "job-1")
    assert link.startswith("https://drive.google.com/file/d/")
    assert file_id in service.store
    assert len(service.store) == 1
    rec = next(iter(service.store.values()))
    assert rec["appProperties"] == {"careeros_job_id": "job-1"}


def test_upload_bytes_updates_same_job_in_place_not_duplicated():
    service = _FakeDriveService()
    _upload_bytes(service, MagicMock, "Acme - PM - Resume.pdf", b"v1", "application/pdf", "parent-1", "job-1")
    _upload_bytes(service, MagicMock, "Acme - PM - Resume.pdf", b"v2", "application/pdf", "parent-1", "job-1")
    assert len(service.store) == 1  # updated, not duplicated


def test_upload_bytes_disambiguates_genuine_name_collision_different_job():
    """Two DIFFERENT jobs producing the identical 'Company - Role' filename
    must not silently clobber each other."""
    service = _FakeDriveService()
    _upload_bytes(service, MagicMock, "Acme - PM - Resume.pdf", b"job1", "application/pdf", "parent-1", "job-1")
    link2 = _upload_bytes(service, MagicMock, "Acme - PM - Resume.pdf", b"job2", "application/pdf", "parent-1", "job-2")
    assert len(service.store) == 2
    names = {rec["name"] for rec in service.store.values()}
    assert names == {"Acme - PM - Resume.pdf", "Acme - PM - Resume (2).pdf"}


def test_find_file_by_name_returns_none_when_absent():
    service = _FakeDriveService()
    assert _find_file_by_name(service, "Nothing.pdf", "parent-1") is None


# ── upload_run: orchestration + flat layout + return shape ───────────────

def _make_artifacts(tmp_path, job_id="job-1", resume=True, cover=True, report=True,
                    deep_report=False, answers=False):
    artifacts_dir = tmp_path / "artifacts" / job_id
    artifacts_dir.mkdir(parents=True)
    if resume:
        (artifacts_dir / "resume.md").write_text("# Resume\n\nSome content.")
    if cover:
        (artifacts_dir / "cover.md").write_text("Cover letter body.")
    if report:
        (artifacts_dir / "daily_report.md").write_text("# Report\n\nEval summary.")
    if deep_report:
        (artifacts_dir / "deep_report.md").write_text("# Deep dive")
    if answers:
        (artifacts_dir / "answers.md").write_text("# Application Answers\n\n## Q\nA")
    return artifacts_dir


def test_upload_run_flat_layout_no_date_subfolder_by_default(tmp_path):
    """Locked Phase 3 layout: files land directly under root_folder_id; no
    per-company, no per-date subfolder unless explicitly configured."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)
    job = make_job(id="job-1", company="Bjak", title="Product Manager")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert "job-1" in results
    r = results["job-1"]
    assert isinstance(r, JobUploadResult)
    assert r.folder_link == "https://drive.google.com/drive/folders/root-1"
    assert r.resume_link and r.cover_link
    assert r.eval_link and r.eval_file_id  # P2.10: previously uploaded but discarded
    assert not r.answers_link  # no answers.md for this job
    assert r.warnings == []
    names = {rec["name"] for rec in service.store.values()}
    assert "Bjak - Product Manager - Resume.pdf" in names
    assert "Bjak - Product Manager - Cover Letter.pdf" in names
    assert "Bjak - Product Manager - Evaluation.md" in names
    assert not any(n.endswith("Deep Report.md") for n in names)  # not generated for this job
    assert not any("Application Answers" in n for n in names)
    # all files parented directly at root, NOT in any company/date subfolder
    assert all("root-1" in rec["parents"] for rec in service.store.values())


def test_upload_run_date_subfolder_when_configured(tmp_path):
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1", date_subfolder=True)

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert results["job-1"].folder_link.startswith("https://drive.google.com/drive/folders/")
    assert "root-1" not in results["job-1"].folder_link  # it's the date-subfolder id, not root


def test_upload_run_falls_back_to_markdown_when_pdf_extra_unavailable(tmp_path):
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=None):  # simulates [pdf] not installed
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    r = results["job-1"]
    assert len(r.warnings) == 2  # resume + cover both fell back
    names = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Resume.md" in names
    assert "Bjak - PM - Cover Letter.md" in names
    assert "Bjak - PM - Resume.pdf" not in names


def test_upload_run_uploads_deep_report_only_when_present(tmp_path):
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path, deep_report=True)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    names = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Deep Report.md" in names


def test_upload_run_uploads_deep_report_link_and_file_id(tmp_path):
    """P2.10: the Deep Report webViewLink/file id, previously uploaded and
    discarded, must now be captured on the result -- that's what lets
    cli.py wire it into the Sheet's Deep Report (Drive) column."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path, deep_report=True)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    r = results["job-1"]
    assert r.deep_report_link and r.deep_report_file_id
    assert r.deep_report_file_id in service.store


def test_upload_run_answers_is_always_markdown_never_pdf(tmp_path):
    """v1.3.2: PDF is attempted for Resume/Cover ONLY. Application Answers
    always uploads as Markdown -- even when render_markdown_to_pdf WOULD
    succeed, proving this isn't a fallback, it's simply never attempted --
    and never fabricated for a job that has no answers.md locally."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path, answers=True)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake") as mock_render:
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    r = results["job-1"]
    assert r.answers_link and r.answers_file_id
    names = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Application Answers.md" in names
    assert "Bjak - PM - Application Answers.pdf" not in names
    assert not any("Answers" in w for w in r.warnings)  # no fallback warning -- never attempted, not a failure
    # render_markdown_to_pdf is only ever called for Resume + Cover Letter (2), never Answers.
    assert mock_render.call_count == 2


def test_upload_run_reupload_same_job_updates_in_place_not_duplicated(tmp_path):
    """Idempotency: re-running upload_run for the SAME job (e.g. a re-run of
    `daily`, or `backfill-drive` on an already-backfilled row) must not
    create duplicate files."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])
        count_after_first = len(service.store)
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])
        count_after_second = len(service.store)

    assert count_after_first == count_after_second


def test_upload_run_pdf_now_available_deletes_stale_markdown_for_same_job(tmp_path):
    """v1.3.2: if a job's Resume/Cover was previously uploaded as .md (e.g.
    before fpdf2 was installed) and PDF rendering later becomes available,
    re-uploading must replace it -- not leave the old .md orphaned alongside
    the new .pdf in the flat Drive folder (Drive matches files by exact
    filename including extension, so a .pdf upload doesn't find/update an
    existing .md on its own -- this is the explicit cleanup for that)."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=None):  # fpdf2 not installed yet
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    names_before = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Resume.md" in names_before
    assert "Bjak - PM - Cover Letter.md" in names_before

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):  # fpdf2 now installed
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    names_after = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Resume.pdf" in names_after
    assert "Bjak - PM - Cover Letter.pdf" in names_after
    assert "Bjak - PM - Resume.md" not in names_after      # stale .md cleaned up
    assert "Bjak - PM - Cover Letter.md" not in names_after
    assert results["job-1"].warnings == []


def test_upload_run_pdf_now_available_cleans_up_suffixed_stale_markdown(tmp_path):
    """Real incident found during v1.3.2's own release verification: TWO
    DIFFERENT jobs sharing the exact same Company/Role string collide on
    name -- the second job's .md upload gets disambiguated to "... (2).md"
    (see _upload_bytes). The stale-.md cleanup must find THAT suffixed
    variant too (by job_id, not just the unsuffixed base name), and must
    NEVER touch the first job's own files even though they share a prefix."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    dir_a = _make_artifacts(tmp_path / "a")
    dir_b = _make_artifacts(tmp_path / "b")
    job_a = make_job(id="job-a", company="Tata Consultancy Services", title="Product Manager")
    job_b = make_job(id="job-b", company="Tata Consultancy Services", title="Product Manager")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    # Both jobs upload as .md first (fpdf2 not installed yet) -- job_b's
    # name collides with job_a's and gets suffixed "(2)".
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=None):
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job_a, dir_a), (job_b, dir_b)])

    names_before = {rec["name"] for rec in service.store.values()}
    assert "Tata Consultancy Services - Product Manager - Resume.md" in names_before
    assert "Tata Consultancy Services - Product Manager - Resume (2).md" in names_before

    # Now fpdf2 is available -- re-publish both jobs.
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job_a, dir_a), (job_b, dir_b)])

    names_after = {rec["name"] for rec in service.store.values()}
    assert "Tata Consultancy Services - Product Manager - Resume.pdf" in names_after
    assert "Tata Consultancy Services - Product Manager - Resume (2).pdf" in names_after
    # Both stale .md variants -- base AND suffixed -- must be gone.
    assert "Tata Consultancy Services - Product Manager - Resume.md" not in names_after
    assert "Tata Consultancy Services - Product Manager - Resume (2).md" not in names_after
    assert "Tata Consultancy Services - Product Manager - Cover Letter.md" not in names_after
    assert "Tata Consultancy Services - Product Manager - Cover Letter (2).md" not in names_after
    # Each job's .pdf is still tagged to the correct, distinct job_id.
    by_name = {rec["name"]: rec for rec in service.store.values()}
    assert by_name["Tata Consultancy Services - Product Manager - Resume.pdf"]["appProperties"]["careeros_job_id"] == "job-a"
    assert by_name["Tata Consultancy Services - Product Manager - Resume (2).pdf"]["appProperties"]["careeros_job_id"] == "job-b"


def test_upload_run_prefers_local_prerendered_pdf_over_rendering_markdown(tmp_path):
    """v1.4.0: `careeros artifacts --finalize` now renders resume.pdf/
    cover.pdf locally (careeros/typst_render.py) before Drive upload ever
    runs. When that local .pdf already exists, upload_run must ship those
    bytes as-is and must NOT call the legacy render_markdown_to_pdf at all —
    patching it to raise proves the upload path never touches it."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path, resume=False, cover=False)
    (artifacts_dir / "resume.json").write_text('{"tagline": "x"}')
    (artifacts_dir / "resume.pdf").write_bytes(b"%PDF-prerendered-resume")
    (artifacts_dir / "cover.md").write_text("Cover letter body.")
    (artifacts_dir / "cover.pdf").write_bytes(b"%PDF-prerendered-cover")
    job = make_job(id="job-1", company="Bjak", title="Product Manager")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    def _boom(*a, **kw):
        raise AssertionError("render_markdown_to_pdf must not be called when a local .pdf already exists")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", side_effect=_boom):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    r = results["job-1"]
    assert r.warnings == []
    names = {rec["name"] for rec in service.store.values()}
    assert "Bjak - Product Manager - Resume.pdf" in names
    assert "Bjak - Product Manager - Cover Letter.pdf" in names


def test_upload_run_local_pdf_cleans_up_stale_markdown_from_before_v2_migration(tmp_path):
    """A job re-run through the v1.4.0 pipeline after previously being
    uploaded as .md under the old resume.md content model must still clean
    up the stale .md, exactly like the fpdf2-just-became-available case."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = _make_artifacts(tmp_path)  # writes legacy resume.md/cover.md
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=None):  # pre-migration, no PDF yet
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert "Bjak - PM - Resume.md" in {rec["name"] for rec in service.store.values()}

    # Now the job has been re-run through the v1.4.0 pipeline: resume.pdf
    # exists locally (finalize rendered it), resume.md is gone (replaced by
    # resume.json), cover.pdf exists too.
    (artifacts_dir / "resume.md").unlink()
    (artifacts_dir / "resume.pdf").write_bytes(b"%PDF-new")
    (artifacts_dir / "cover.pdf").write_bytes(b"%PDF-new-cover")

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", side_effect=AssertionError("must not re-render")):
        upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    names_after = {rec["name"] for rec in service.store.values()}
    assert "Bjak - PM - Resume.pdf" in names_after
    assert "Bjak - PM - Resume.md" not in names_after


def test_upload_run_skips_missing_artifact_files_without_failing(tmp_path):
    """A selected job whose resume/cover somehow isn't on disk yet must not
    crash the whole upload — it's simply absent from the results dict."""
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    artifacts_dir = tmp_path / "artifacts" / "job-1"  # deliberately not created
    job = make_job(id="job-1", company="Bjak", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [(job, artifacts_dir)])

    assert results == {}
    # run.json + summary.md still upload even with zero job artifacts
    names = {rec["name"] for rec in service.store.values()}
    assert names == {"run.json", "summary.md"}


def test_upload_run_empty_selected_jobs_still_uploads_run_json_and_summary(tmp_path):
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md, [])

    assert results == {}
    names = {rec["name"] for rec in service.store.values()}
    assert names == {"run.json", "summary.md"}


# ── upload_jobs: multi-date backfill batch (Phase 3, v1.1) ───────────────

def test_upload_jobs_batch_spans_multiple_dates_flat_by_default(tmp_path):
    """Backfill jobs from different historical dates all land in the SAME
    flat root when date_subfolder is off (the default)."""
    a = _make_artifacts(tmp_path, job_id="job-a")
    b = _make_artifacts(tmp_path, job_id="job-b")
    job_a = make_job(id="job-a", company="Razorpay", title="PM II")
    job_b = make_job(id="job-b", company="Coinbase", title="Senior PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_jobs(cfg, [
            ("2026-07-07", job_a, a),
            ("2026-07-08", job_b, b),
        ])

    assert set(results) == {"job-a", "job-b"}
    assert all("root-1" in rec["parents"] for rec in service.store.values())
    assert results["job-a"].folder_link == results["job-b"].folder_link  # same flat root


def test_upload_jobs_uses_per_job_date_subfolder_when_configured(tmp_path):
    a = _make_artifacts(tmp_path, job_id="job-a")
    b = _make_artifacts(tmp_path, job_id="job-b")
    job_a = make_job(id="job-a", company="Razorpay", title="PM")
    job_b = make_job(id="job-b", company="Coinbase", title="Senior PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1", date_subfolder=True)

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_jobs(cfg, [
            ("2026-07-07", job_a, a),
            ("2026-07-08", job_b, b),
        ])

    # different dates -> different (date) subfolders -> different folder links
    assert results["job-a"].folder_link != results["job-b"].folder_link


def test_upload_jobs_skips_jobs_with_no_local_artifacts(tmp_path):
    missing_dir = tmp_path / "artifacts" / "job-missing"  # never created
    job = make_job(id="job-missing", company="Ghost Co", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service):
        results = upload_jobs(cfg, [("2026-07-07", job, missing_dir)])

    assert results == {}


def test_upload_jobs_is_idempotent_across_repeated_backfill_runs(tmp_path):
    a = _make_artifacts(tmp_path, job_id="job-a")
    job_a = make_job(id="job-a", company="Razorpay", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        upload_jobs(cfg, [("2026-07-07", job_a, a)])
        count_1 = len(service.store)
        upload_jobs(cfg, [("2026-07-07", job_a, a)])  # re-run backfill on the same job
        count_2 = len(service.store)

    assert count_1 == count_2


# ── per-job failure isolation + verify_uploads (reconciliation) ─────────

def test_upload_jobs_one_job_failure_does_not_abort_the_batch(tmp_path):
    """A single job's upload blowing up (e.g. a transient Drive API error)
    must not prevent the REST of the batch from uploading."""
    a = _make_artifacts(tmp_path, job_id="job-good")
    b = _make_artifacts(tmp_path, job_id="job-bad")
    job_good = make_job(id="job-good", company="Good Co", title="PM")
    job_bad = make_job(id="job-bad", company="Bad Co", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    real_upload_bytes = service.create
    call_count = {"n": 0}

    def flaky_create(body, media_body=None, fields=""):
        call_count["n"] += 1
        if "Bad Co" in body["name"]:
            raise RuntimeError("simulated Drive API failure")
        return real_upload_bytes(body, media_body, fields)

    service.create = flaky_create

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_jobs(cfg, [("2026-07-07", job_good, a), ("2026-07-07", job_bad, b)])

    assert "job-good" in results and not results["job-good"].error
    assert results["job-good"].resume_link  # actually uploaded
    assert "job-bad" in results and results["job-bad"].error  # recorded, not silently dropped
    assert not results["job-bad"].resume_link


def test_upload_run_one_job_failure_does_not_abort_the_batch(tmp_path):
    run_json = tmp_path / "run.json"; run_json.write_text("{}")
    summary_md = tmp_path / "summary.md"; summary_md.write_text("# summary")
    a = _make_artifacts(tmp_path, job_id="job-good")
    b = _make_artifacts(tmp_path, job_id="job-bad")
    job_good = make_job(id="job-good", company="Good Co", title="PM")
    job_bad = make_job(id="job-bad", company="Bad Co", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    real_create = service.create

    def flaky_create(body, media_body=None, fields=""):
        if "Bad Co" in body["name"]:
            raise RuntimeError("simulated failure")
        return real_create(body, media_body, fields)

    service.create = flaky_create

    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_run(cfg, "2026-07-08", run_json, summary_md,
                            [(job_good, a), (job_bad, b)])

    assert not results["job-good"].error and results["job-good"].resume_link
    assert results["job-bad"].error


def test_verify_uploads_confirms_files_exist_and_not_trashed(tmp_path):
    a = _make_artifacts(tmp_path, job_id="job-a")
    job_a = make_job(id="job-a", company="Acme", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_jobs(cfg, [("2026-07-07", job_a, a)])
        verification = verify_uploads(cfg, results)

    assert verification["job-a"]["resume_ok"] is True
    assert verification["job-a"]["cover_ok"] is True
    assert verification["job-a"]["errors"] == []


def test_verify_uploads_detects_a_missing_or_trashed_file(tmp_path):
    a = _make_artifacts(tmp_path, job_id="job-a")
    job_a = make_job(id="job-a", company="Acme", title="PM")
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")

    service = _FakeDriveService()
    with patch("careeros.drive._lazy_imports", return_value=(MagicMock(),) * 5), \
         patch("careeros.drive._drive_service", return_value=service), \
         patch("careeros.drive.render_markdown_to_pdf", return_value=b"%PDF-fake"):
        results = upload_jobs(cfg, [("2026-07-07", job_a, a)])
        # simulate the resume file having been deleted/trashed after upload
        del service.store[results["job-a"].resume_file_id]

        verification = verify_uploads(cfg, results)

    assert verification["job-a"]["resume_ok"] is False
    assert verification["job-a"]["errors"]  # a reason was recorded


def test_verify_uploads_skips_jobs_that_already_failed_to_upload(tmp_path):
    cfg = _cfg(client_secret_path=str(tmp_path / "x.json"), root_folder_id="root-1")
    service = _FakeDriveService()
    failed_results = {"job-x": JobUploadResult(folder_link="https://x", error="upload blew up")}
    with patch("careeros.drive._drive_service", return_value=service):
        verification = verify_uploads(cfg, failed_results)
    assert "job-x" not in verification  # nothing to verify for a job that never uploaded
