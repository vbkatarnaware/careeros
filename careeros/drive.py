"""Google Drive artifact backup (P2.6, optional, config-gated).

Uploads ONLY shortlisted (selected, score >= threshold, recommendation ==
"apply") jobs' artifacts to Drive as an ADDITIVE backup. Local Markdown under
`.careeros/runs/<date>/` remains the single source of truth end to end — no
pipeline stage ever reads anything back from Drive.

Uses the existing OAuth DESKTOP client (an "installed app" client secret,
NOT a service-account key) so uploads land in the configured user's own
personal Drive quota — a service account can't write into a personal Drive
the way a real user's own OAuth grant can, which is the right model for a
personal daily-use CLI (Google Sheets, by contrast, stays on its existing
service account — nothing about that changes).

Fail-soft by hard requirement: this module is imported LAZILY (only when
`drive.enabled: true` and the CLI's `drive` command actually runs), and every
exception surfaces as a `DriveError` for the caller to catch — discovery,
evaluation, reports, and Sheets must never fail because Drive failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from careeros.config import Config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveError(RuntimeError):
    """Any Drive failure (missing optional deps, auth, network, quota,
    misconfiguration) — callers catch this (broadly; see cli.py's `drive`
    command) and continue the pipeline with a warning, never a hard stop."""


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
    Request, Credentials, InstalledAppFlow, _, _ = _lazy_imports()

    client_secret_path = config.drive.get("client_secret_path")
    if not client_secret_path:
        raise DriveError("drive.client_secret_path not set in .careeros/config.yaml")
    if not Path(client_secret_path).exists():
        raise DriveError(f"drive.client_secret_path does not exist: {client_secret_path}")

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


def _upload_text_file(service, media_upload_cls, name: str, content: str, parent_id: str) -> None:
    """Create-or-overwrite a small text file by name within parent_id."""
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


def upload_run(
    config: Config,
    date: str,
    run_json_path: Path,
    summary_md_path: Path,
    selected_jobs: list[tuple[Any, Path]],  # (Job, artifacts_dir) for each SELECTED job
) -> dict[str, str]:
    """Uploads the day's run.json, summary.md, and each selected job's
    report/resume/cover into <root_folder_id>/YYYY-MM-DD/<Company>/ on Drive.

    Returns {job_id: company_folder_url} for jobs whose folder was created —
    a job simply isn't in the returned dict if its artifacts were missing on
    disk; that's not a Drive failure, just nothing to link for that row.
    Raises DriveError for anything that stops the WHOLE upload (auth,
    missing deps, misconfiguration) — the caller (cli.py) is responsible for
    catching that and continuing the pipeline regardless.
    """
    _, _, _, _, MediaInMemoryUpload = _lazy_imports()
    service = _drive_service(config)

    root_id = config.drive.get("root_folder_id")
    if not root_id:
        raise DriveError("drive.root_folder_id not set in .careeros/config.yaml")

    date_folder_id = _find_or_create_folder(service, date, root_id)

    if run_json_path.exists():
        _upload_text_file(service, MediaInMemoryUpload, "run.json", run_json_path.read_text(), date_folder_id)
    if summary_md_path.exists():
        _upload_text_file(service, MediaInMemoryUpload, "summary.md", summary_md_path.read_text(), date_folder_id)

    links: dict[str, str] = {}
    for job, artifacts_dir in selected_jobs:
        company_folder_id = _find_or_create_folder(service, job.company, date_folder_id)
        for filename, path in (
            ("report.md", artifacts_dir / "daily_report.md"),
            ("resume.md", artifacts_dir / "resume.md"),
            ("cover_letter.md", artifacts_dir / "cover.md"),
        ):
            if path.exists():
                _upload_text_file(service, MediaInMemoryUpload, filename, path.read_text(), company_folder_id)
        links[job.id] = f"https://drive.google.com/drive/folders/{company_folder_id}"

    return links
