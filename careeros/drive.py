"""Google Drive artifact backup (P2.6, extended in Phase 3/v1.1).

Uploads ONLY Apply-tier (score >= threshold, recommendation == "apply", hard
constraints passing) jobs' artifacts to Drive as an ADDITIVE backup. Local
Markdown under `.careeros/runs/<date>/` remains the single source of truth
end to end — no pipeline stage ever reads anything back from Drive. Consider
-tier jobs never reach this module (they have no artifacts to upload).

Flat layout (Phase 3, locked): ONE root folder (`drive.root_folder_id`), no
per-company or per-job subfolders. An optional per-run date subfolder is
available via `drive.date_subfolder: true` (default false — flat). Files are
named `Company - Role - <Artifact>.<ext>`, so every job's files sit directly
in the same folder, sorted naturally by company. **Only Resume and Cover
Letter** are ever uploaded as PDF (rendered by careeros/pdf.py) — the `[pdf]`
extra (`fpdf2`) is folded into the `[drive]` extra (v1.3.2), so a fresh OSS
clone that installs `[drive]` gets PDF rendering for these two by default; if
it's still somehow missing (or a render fails for some edge-case markdown),
this falls back to uploading the `.md` source instead and returns a warning
string, but never raises for that reason alone. If a job's Resume/Cover was
previously uploaded as `.md` (before PDF rendering was available) and is now
re-uploaded as `.pdf`, the stale `.md` is deleted — Drive matches files by
exact filename including extension, so the new `.pdf` upload wouldn't
otherwise replace it, just sit alongside it as an orphan. Application
Answers, Evaluation, and Deep Report are **always** Markdown — PDF is never
attempted for them. Deep Report is uploaded only if
`artifacts_dir/deep_report.md` exists locally (it's `prep`-only — `daily`
never forces its generation); Application Answers only if
`artifacts_dir/answers.md` exists (written by the `apply` stage — see
`careeros/apply/` — for Apply-tier jobs whose question-form was readable, or
by an on-demand `careeros apply <job-id>`).

Uses the existing OAuth DESKTOP client (an "installed app" client secret,
NOT a service-account key) so uploads land in the configured user's own
personal Drive quota — a service account can't write into a personal Drive
the way a real user's own OAuth grant can, which is the right model for a
personal daily-use CLI (Google Sheets, by contrast, stays on its existing
service account — nothing about that changes).

Idempotency: every uploaded file is tagged with a Drive `appProperties` key
(`careeros_job_id`) so re-uploading the SAME job's artifacts (a re-run of
`daily`, or `backfill-drive`) finds and UPDATES the existing file in place
rather than creating a duplicate. A genuine name collision (a different job
that happens to produce the exact same "Company - Role" combination) is
disambiguated with a numeric suffix, e.g. "... - Resume (2).pdf" — detected
by comparing the existing file's `careeros_job_id` against the job being
uploaded, not by assuming names are always unique.

Fail-soft by hard requirement: this module is imported LAZILY (only when
`drive.enabled: true` and the CLI's `drive`/`backfill-drive` commands
actually run), and every exception surfaces as a `DriveError` for the caller
to catch — discovery, evaluation, reports, and Sheets must never fail
because Drive failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from careeros.config import Config
from careeros.pdf import render_markdown_to_pdf

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_JOB_ID_PROPERTY = "careeros_job_id"


class DriveError(RuntimeError):
    """Any Drive failure (missing optional deps, auth, network, quota,
    misconfiguration) — callers catch this (broadly; see cli.py's `drive`
    command) and continue the pipeline with a warning, never a hard stop."""


@dataclass
class JobUploadResult:
    """What actually happened uploading one job's artifacts. `warnings`
    covers per-file fallbacks (e.g. PDF render unavailable/failed -> MD
    uploaded instead) that are NOT DriveErrors — the job's folder link is
    still returned, just with a note attached.

    `*_file_id` fields are the Drive file ids (not just the clickable link)
    — kept so a caller can independently re-verify the file still exists via
    the Drive API (`files().get(fileId=...)`) rather than trusting the link
    alone. `eval_link`/`deep_report_link`/`answers_link` (P2.10) were
    previously uploaded but discarded by this module — the files existed in
    Drive with no way to find them from the Sheet; capturing them here is
    what lets `cli.py` wire them into the Sheet's Evaluation (Drive) / Deep
    Report (Drive) / Application Answers (Drive) columns. `error` is set
    (and links left blank) when this JOB'S upload failed — callers
    (upload_run/upload_jobs) isolate per-job failures so one bad job never
    aborts the rest of the batch."""
    folder_link: str
    resume_link: str = ""
    cover_link: str = ""
    resume_file_id: str = ""
    cover_file_id: str = ""
    eval_link: str = ""
    eval_file_id: str = ""
    deep_report_link: str = ""
    deep_report_file_id: str = ""
    answers_link: str = ""
    answers_file_id: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def _lazy_imports():
    """Import the optional `[drive]` extra's deps only when Drive is actually
    used, so a user who never enables it doesn't need them installed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaInMemoryUpload
    except ImportError as e:
        raise DriveError(
            "Drive integration needs the optional [drive] extra — "
            "run: pip install -e '.[drive]'"
        ) from e
    return Request, Credentials, InstalledAppFlow, build, MediaInMemoryUpload


def _get_credentials(config: Config):
    client_secret_path = config.drive.get("client_secret_path")
    if not client_secret_path:
        raise DriveError("drive.client_secret_path not set in .careeros/config.yaml")
    if not Path(client_secret_path).exists():
        raise DriveError(f"drive.client_secret_path does not exist: {client_secret_path}")

    Request, Credentials, InstalledAppFlow, _, _ = _lazy_imports()

    token_path = Path(config.drive.get("token_path") or ".careeros/drive_token.json")

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # One-time browser consent. After this, the refresh token in
            # token_path makes every later run silent — no browser needed.
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return creds


def _drive_service(config: Config):
    _, _, _, build, _ = _lazy_imports()
    return build("drive", "v3", credentials=_get_credentials(config))


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    existing = results.get("files", [])
    if existing:
        return existing[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _sanitize_filename_component(text: str) -> str:
    """Drive allows almost any character in a filename, but slashes are
    genuinely path-breaking in some clients/exports — replace them, and trim
    whitespace. Deliberately minimal; this is not a general slugifier."""
    return text.replace("/", "-").replace("\\", "-").strip()


def _find_file_by_name(service, name: str, parent_id: str) -> Optional[dict]:
    """Returns {"id", "appProperties"} for an exact-name match in parent_id,
    or None. appProperties is included so the caller can tell whether an
    existing same-named file belongs to the SAME job (update in place) or a
    DIFFERENT job (a genuine collision needing disambiguation)."""
    safe_name = name.replace("'", "\\'")
    query = f"name = '{safe_name}' and '{parent_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, appProperties)").execute()
    files = results.get("files", [])
    return files[0] if files else None


def _upload_bytes(
    service, media_upload_cls, name: str, data: bytes, mimetype: str, parent_id: str, job_id: str,
) -> tuple[str, str]:
    """Create-or-update a file by name within parent_id, tagged with the
    owning job's id (for idempotent re-upload + collision detection — see
    module docstring). If a same-named file exists for a DIFFERENT job, the
    name is disambiguated with a numeric suffix before creating a new file.
    Returns (file_id, webViewLink) — the id lets a caller independently
    re-verify the file via the Drive API later, not just trust the link."""
    media = media_upload_cls(data, mimetype=mimetype)
    app_properties = {_JOB_ID_PROPERTY: job_id}

    candidate_name = name
    suffix = 1
    while True:
        existing = _find_file_by_name(service, candidate_name, parent_id)
        if not existing:
            metadata = {"name": candidate_name, "parents": [parent_id], "appProperties": app_properties}
            created = service.files().create(
                body=metadata, media_body=media, fields="id, webViewLink"
            ).execute()
            return created["id"], created["webViewLink"]
        if existing.get("appProperties", {}).get(_JOB_ID_PROPERTY) == job_id:
            updated = service.files().update(
                fileId=existing["id"], media_body=media, fields="id, webViewLink"
            ).execute()
            return updated["id"], updated["webViewLink"]
        # Same name, different job — a genuine collision. Disambiguate.
        suffix += 1
        stem, _, ext = name.rpartition(".")
        candidate_name = f"{stem} ({suffix}).{ext}" if stem else f"{name} ({suffix})"


def _upload_text(service, media_upload_cls, name: str, content: str, parent_id: str, job_id: str) -> tuple[str, str]:
    return _upload_bytes(service, media_upload_cls, name, content.encode("utf-8"),
                          "text/markdown", parent_id, job_id)


def _delete_stale_markdown_variant(
    service, prefix: str, label: str, job_id: str, parent_id: str, keep_file_id: str,
) -> None:
    """After uploading a job's Resume/Cover as `.pdf`, delete a leftover
    `.md` file for the SAME job/label if one exists (e.g. from before the
    `[pdf]` extra was installed, or from before this fix). `_find_file_by_name`
    matches by exact filename including extension, so a new `.pdf` upload
    does NOT find/replace an old `.md` — without this cleanup, the stale
    `.md` sits orphaned in the flat Drive folder next to the new `.pdf`."""
    stale_name = f"{prefix} - {label}.md"
    existing = _find_file_by_name(service, stale_name, parent_id)
    if (
        existing
        and existing["id"] != keep_file_id
        and existing.get("appProperties", {}).get(_JOB_ID_PROPERTY) == job_id
    ):
        service.files().delete(fileId=existing["id"]).execute()


def _upload_text_file(service, media_upload_cls, name: str, content: str, parent_id: str) -> None:
    """Create-or-overwrite a small run-level text file (run.json, summary.md)
    by name — no job-id tagging, since these aren't per-job artifacts."""
    media = media_upload_cls(content.encode("utf-8"), mimetype="text/markdown")
    safe_name = name.replace("'", "\\'")
    query = f"name = '{safe_name}' and '{parent_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    existing = results.get("files", [])
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    else:
        metadata = {"name": name, "parents": [parent_id]}
        service.files().create(body=metadata, media_body=media).execute()


def _upload_job_artifacts(
    service, media_upload_cls, job: Any, artifacts_dir: Path, parent_id: str,
) -> JobUploadResult:
    """Uploads one job's Resume/Cover (PDF — the only two artifacts PDF is
    ever attempted for) + Application Answers/Evaluation/Deep Report
    (Markdown, always — never PDF-attempted). Never fabricates: a missing
    source file is simply skipped, not invented."""
    company = _sanitize_filename_component(job.company)
    role = _sanitize_filename_component(job.title)
    prefix = f"{company} - {role}"
    warnings: list[str] = []
    resume_link = resume_file_id = ""
    cover_link = cover_file_id = ""
    answers_link = answers_file_id = ""

    for label, md_filename in (("Resume", "resume.md"), ("Cover Letter", "cover.md")):
        src = artifacts_dir / md_filename
        if not src.exists():
            continue
        md_text = src.read_text()
        pdf_bytes = render_markdown_to_pdf(md_text)
        if pdf_bytes is not None:
            file_id, link = _upload_bytes(service, media_upload_cls, f"{prefix} - {label}.pdf",
                                          pdf_bytes, "application/pdf", parent_id, job.id)
            _delete_stale_markdown_variant(service, prefix, label, job.id, parent_id, file_id)
        else:
            warnings.append(f"PDF rendering unavailable or failed — uploaded {label} as Markdown, not PDF")
            file_id, link = _upload_text(service, media_upload_cls, f"{prefix} - {label}.md",
                                         md_text, parent_id, job.id)
        if label == "Resume":
            resume_link, resume_file_id = link, file_id
        else:
            cover_link, cover_file_id = link, file_id

    answers_src = artifacts_dir / "answers.md"
    if answers_src.exists():
        answers_file_id, answers_link = _upload_text(
            service, media_upload_cls, f"{prefix} - Application Answers.md",
            answers_src.read_text(), parent_id, job.id,
        )

    eval_link = eval_file_id = ""
    eval_src = artifacts_dir / "daily_report.md"
    if eval_src.exists():
        eval_file_id, eval_link = _upload_text(service, media_upload_cls, f"{prefix} - Evaluation.md",
                                               eval_src.read_text(), parent_id, job.id)

    deep_report_link = deep_report_file_id = ""
    deep_report_src = artifacts_dir / "deep_report.md"
    if deep_report_src.exists():
        deep_report_file_id, deep_report_link = _upload_text(
            service, media_upload_cls, f"{prefix} - Deep Report.md",
            deep_report_src.read_text(), parent_id, job.id,
        )

    return JobUploadResult(
        folder_link="", resume_link=resume_link, cover_link=cover_link,
        resume_file_id=resume_file_id, cover_file_id=cover_file_id,
        eval_link=eval_link, eval_file_id=eval_file_id,
        deep_report_link=deep_report_link, deep_report_file_id=deep_report_file_id,
        answers_link=answers_link, answers_file_id=answers_file_id,
        warnings=warnings,
    )


def upload_run(
    config: Config,
    date: str,
    run_json_path: Path,
    summary_md_path: Path,
    selected_jobs: list[tuple[Any, Path]],  # (Job, artifacts_dir) for each APPLY-tier job
) -> dict[str, JobUploadResult]:
    """Uploads the day's run.json, summary.md, and each Apply-tier job's
    resume/cover (PDF)/evaluation/deep-report into the flat Drive layout:
    `<root_folder_id>/` by default, or `<root_folder_id>/YYYY-MM-DD/` if
    `drive.date_subfolder: true`.

    Returns {job_id: JobUploadResult} — a job is simply absent from the
    returned dict if NONE of its artifact files exist on disk; that's not a
    Drive failure, just nothing to link for that row. Raises DriveError for
    anything that stops the WHOLE upload (auth, missing deps,
    misconfiguration) — the caller (cli.py) is responsible for catching that
    and continuing the pipeline regardless.
    """
    root_id = config.drive.get("root_folder_id")
    if not root_id:
        raise DriveError("drive.root_folder_id not set in .careeros/config.yaml")

    _, _, _, _, MediaInMemoryUpload = _lazy_imports()
    service = _drive_service(config)

    parent_id = root_id
    if config.drive.get("date_subfolder"):
        parent_id = _find_or_create_folder(service, date, root_id)

    if run_json_path.exists():
        _upload_text_file(service, MediaInMemoryUpload, "run.json", run_json_path.read_text(), parent_id)
    if summary_md_path.exists():
        _upload_text_file(service, MediaInMemoryUpload, "summary.md", summary_md_path.read_text(), parent_id)

    folder_link = f"https://drive.google.com/drive/folders/{parent_id}"
    results: dict[str, JobUploadResult] = {}
    for job, artifacts_dir in selected_jobs:
        if not artifacts_dir.exists():
            continue
        try:
            result = _upload_job_artifacts(service, MediaInMemoryUpload, job, artifacts_dir, parent_id)
        except Exception as e:  # ONE job's failure must never abort the rest of the day's upload
            results[job.id] = JobUploadResult(folder_link=folder_link, error=str(e))
            continue
        if not (result.resume_link or result.cover_link):
            continue  # nothing existed on disk for this job — not a failure, just nothing to link
        result.folder_link = folder_link
        results[job.id] = result

    return results


def upload_jobs(
    config: Config,
    jobs: list[tuple[str, Any, Path]],  # (date, Job-like, artifacts_dir) — may span many dates
) -> dict[str, JobUploadResult]:
    """Like `upload_run`, but for a batch of jobs spanning MULTIPLE run-dates
    with no per-run run.json/summary.md — used by `careeros backfill-drive`
    to add Drive artifacts for jobs that predate Drive automation. One Drive
    connection is reused for the whole batch (not reconnected per job).

    Each job's parent folder is resolved from ITS OWN `date` — only matters
    if `drive.date_subfolder: true`; otherwise every job lands in the same
    flat root regardless of date, same as `upload_run`. `job` only needs
    `.id`, `.company`, `.title` attributes — a full `Job` isn't required
    (backfill may not have normalize.json for an old run; a lightweight
    stand-in with just those three fields is enough)."""
    root_id = config.drive.get("root_folder_id")
    if not root_id:
        raise DriveError("drive.root_folder_id not set in .careeros/config.yaml")

    _, _, _, _, MediaInMemoryUpload = _lazy_imports()
    service = _drive_service(config)

    use_date_subfolder = config.drive.get("date_subfolder")
    parent_cache: dict[str, str] = {}

    def _parent_for(date: str) -> str:
        if not use_date_subfolder:
            return root_id
        if date not in parent_cache:
            parent_cache[date] = _find_or_create_folder(service, date, root_id)
        return parent_cache[date]

    results: dict[str, JobUploadResult] = {}
    for date, job, artifacts_dir in jobs:
        if not artifacts_dir.exists():
            continue
        parent_id = _parent_for(date)
        folder_link = f"https://drive.google.com/drive/folders/{parent_id}"
        try:
            result = _upload_job_artifacts(service, MediaInMemoryUpload, job, artifacts_dir, parent_id)
        except Exception as e:  # ONE job's failure must never abort the rest of the backfill batch
            results[job.id] = JobUploadResult(folder_link=folder_link, error=str(e))
            continue
        if not (result.resume_link or result.cover_link):
            continue
        result.folder_link = folder_link
        results[job.id] = result

    return results


def verify_uploads(config: Config, results: dict[str, JobUploadResult]) -> dict[str, dict[str, Any]]:
    """Re-fetch each successfully-uploaded job's Resume/Cover files from the
    Drive API (NOT just trusting the link/id returned at upload time) and
    confirm they genuinely exist and aren't trashed. Returns
    {job_id: {"resume_ok": bool, "cover_ok": bool, "errors": [...]}}.

    Jobs with `result.error` set (upload already failed) are skipped — there
    is nothing to verify for them. This is the authoritative Drive-side half
    of a post-migration reconciliation check; see also the Sheet-side check
    a caller does separately by re-reading the row."""
    service = _drive_service(config)
    verification: dict[str, dict[str, Any]] = {}
    for job_id, r in results.items():
        if r.error:
            continue
        entry: dict[str, Any] = {"resume_ok": False, "cover_ok": False, "errors": []}
        for label, file_id in (("resume", r.resume_file_id), ("cover", r.cover_file_id)):
            if not file_id:
                entry[f"{label}_ok"] = True  # nothing was supposed to be uploaded for this slot
                continue
            try:
                meta = service.files().get(fileId=file_id, fields="id, trashed").execute()
                entry[f"{label}_ok"] = not meta.get("trashed", False)
                if meta.get("trashed"):
                    entry["errors"].append(f"{label} file {file_id} is trashed")
            except Exception as e:
                entry["errors"].append(f"{label} file {file_id} not verifiable: {e}")
        verification[job_id] = entry
    return verification
