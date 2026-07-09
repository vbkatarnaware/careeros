"""Google Sheets integration. Deterministic — no AI involved.

Google Sheets is the primary OUTPUT for v1 (not a database). This module
does exactly two things: append rows for a day's selected jobs, and read
back existing Job IDs for dedupe. Both are plain gspread calls; there is no
sync/merge logic because CareerOS only ever appends, never edits or deletes
a row it already wrote (an existing row is left for the human to edit
freely — status, notes, whatever they want to track there).
"""

from __future__ import annotations

from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from careeros.config import Config
from careeros.models import Eval, Job

SHEET_HEADERS = [
    "Date", "Company", "Company LinkedIn", "Role", "Score", "Confidence", "Recommendation",
    "Tier", "Apply URL", "Resume Path", "Cover Letter Path", "Report Path",
    "Source", "Hiring Contact", "Contact LinkedIn", "Contact Email",
    "Drive Folder", "Notes", "Job ID",
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _open_worksheet(config: Config) -> gspread.Worksheet:
    creds_path = config.sheets.get("credentials_path")
    spreadsheet_id = config.sheets.get("spreadsheet_id")
    if not creds_path or not spreadsheet_id:
        raise RuntimeError(
            "Sheets not configured — set sheets.credentials_path and "
            "sheets.spreadsheet_id in .careeros/config.yaml (see README)."
        )
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet_name = config.sheets.get("worksheet", "Jobs")

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(SHEET_HEADERS))
        worksheet.append_row(SHEET_HEADERS)
    return worksheet


def _ensure_headers(worksheet: gspread.Worksheet) -> list[str]:
    """Return the worksheet's effective header row, additively migrating it to
    the current schema first. Any canonical SHEET_HEADERS column not already
    present is APPENDED at the end — existing columns, their order, and any
    user-added columns are never moved or removed.

    This is the migration path for sheets created before a header was added
    (e.g. a 15-column sheet predating `Company LinkedIn` / `Drive Folder`):
    the missing columns get appended, and because every read/write keys off
    the header BY NAME (never by hardcoded position), correctness is preserved
    regardless of the resulting column order."""
    values = worksheet.get_all_values()
    if not values or not any(values[0]):
        worksheet.update([SHEET_HEADERS], "A1")
        return list(SHEET_HEADERS)
    header = values[0]
    missing = [h for h in SHEET_HEADERS if h not in header]
    if missing:
        new_header = header + missing
        extra_cols = len(new_header) - worksheet.col_count
        if extra_cols > 0:
            worksheet.add_cols(extra_cols)
        worksheet.update([new_header], "A1")
        return new_header
    return header


def read_existing_job_ids(config: Config) -> set[str]:
    """Read back the Job ID column for dedupe-against-sheet. The column is
    located BY NAME from the live header (not by a hardcoded position), so a
    sheet on an older/wider/reordered schema still dedupes correctly."""
    worksheet = _open_worksheet(config)
    values = worksheet.get_all_values()
    if len(values) <= 1:
        return set()
    header = values[0]
    if "Job ID" not in header:
        return set()
    id_col = header.index("Job ID")
    return {row[id_col] for row in values[1:] if len(row) > id_col and row[id_col]}


def job_to_row(
    date: str, job: Job, evaluation: Eval,
    resume_path: str, cover_path: str, report_path: str,
    drive_folder_link: str = "",
    tier: str = "Apply",
    notes: str = "",
) -> list[Any]:
    """Build one Sheet row (in canonical SHEET_HEADERS order; append_rows then
    reorders it to the live header by name).

    `tier` is "Apply" (score >= threshold: full pipeline) or "Consider"
    (near-miss: Sheet-only). For a Consider row the caller passes blank
    resume/cover/report paths and blank drive_folder_link (no artifacts were
    generated) and a `notes` string explaining why it fell short of Apply.
    `drive_folder_link` is otherwise blank unless the optional Drive backup
    (P2.6) ran — sheets.py has no import dependency on drive.py; cli.py resolves
    the link and passes it in, keeping the two modules decoupled."""
    contact = job.contact
    return [
        date, job.company, job.company_linkedin or "", job.title,
        evaluation.score, evaluation.confidence, evaluation.recommendation,
        tier, job.apply_url, resume_path, cover_path, report_path,
        job.source,
        contact.name if contact else "",
        contact.linkedin if contact else "",
        contact.email if contact else "",
        drive_folder_link, notes,
        job.id,
    ]


def append_rows(config: Config, rows: list[list[Any]]) -> None:
    """Append rows (each in canonical SHEET_HEADERS order, e.g. from
    `job_to_row`). The rows are reordered to match the LIVE header BY NAME
    before appending, so columns always land under the right header even on a
    sheet whose column order/count differs from SHEET_HEADERS. Missing
    canonical columns are auto-added first (see `_ensure_headers`)."""
    if not rows:
        return
    worksheet = _open_worksheet(config)
    live_header = _ensure_headers(worksheet)
    aligned = []
    for row in rows:
        by_name = dict(zip(SHEET_HEADERS, row))
        aligned.append([by_name.get(col, "") for col in live_header])
    worksheet.append_rows(aligned, value_input_option="USER_ENTERED")
