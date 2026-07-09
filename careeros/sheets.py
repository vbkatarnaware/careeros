"""Google Sheets integration. Deterministic — no AI involved.

Google Sheets is the primary OUTPUT for v1 (not a database). This module's
core operation is still append-only for new rows written by `daily` — an
existing row is left for the human to edit freely (status, notes, whatever
they want to track there) unless explicitly targeted.

Phase 3 (v1.1) adds ONE narrow, deliberate exception to "never edit an
existing row": `update_row_by_job_id()`, used only by `careeros
backfill-drive` to add Drive links to rows that predate Drive automation. It
touches ONLY the specific columns it's told to (Drive Folder, Resume/Cover
(Drive)) — never anything else in that row, so any human notes/status
elsewhere in the row are untouched.
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
    "Drive Folder", "Resume (Drive)", "Cover Letter (Drive)", "Notes", "Job ID",
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
    resume_drive_link: str = "",
    cover_drive_link: str = "",
    tier: str = "Apply",
    notes: str = "",
) -> list[Any]:
    """Build one Sheet row (in canonical SHEET_HEADERS order; append_rows then
    reorders it to the live header by name).

    `tier` is "Apply" (score >= threshold: full pipeline) or "Consider"
    (near-miss: Sheet-only). For a Consider row the caller passes blank
    resume/cover/report paths and blank drive links (no artifacts were
    generated) and a `notes` string explaining why it fell short of Apply.

    `drive_folder_link` points at the shared Drive folder (same link for
    every row under the Phase 3 flat layout); `resume_drive_link`/
    `cover_drive_link` are direct, per-job clickable links to that job's own
    Resume/Cover Letter file in Drive — one click to the exact file, no
    searching. All three are blank unless the optional Drive backup ran.
    sheets.py has no import dependency on drive.py; cli.py resolves the
    links and passes them in, keeping the two modules decoupled."""
    contact = job.contact
    return [
        date, job.company, job.company_linkedin or "", job.title,
        evaluation.score, evaluation.confidence, evaluation.recommendation,
        tier, job.apply_url, resume_path, cover_path, report_path,
        job.source,
        contact.name if contact else "",
        contact.linkedin if contact else "",
        contact.email if contact else "",
        drive_folder_link, resume_drive_link, cover_drive_link, notes,
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


def read_all_rows_with_job_id(config: Config) -> list[dict[str, Any]]:
    """Read every data row (excluding the header) as a {header_name: value}
    dict, plus a `_row_number` (1-indexed, matching gspread's convention —
    row 1 is the header, so the first data row is `_row_number: 2`) for
    later targeted updates. Used by `careeros backfill-drive` to find
    existing Apply-tier rows that predate Drive automation. Column lookup is
    BY NAME (via the live header), same as every other read in this module."""
    worksheet = _open_worksheet(config)
    values = worksheet.get_all_values()
    if len(values) <= 1:
        return []
    header = values[0]
    rows = []
    for i, row in enumerate(values[1:], start=2):
        record = {col: (row[j] if j < len(row) else "") for j, col in enumerate(header)}
        record["_row_number"] = i
        rows.append(record)
    return rows


def update_row_by_job_id(config: Config, job_id: str, updates: dict[str, str]) -> bool:
    """Update ONLY the named columns of the row matching `job_id`, in place —
    the one deliberate, narrow exception to this module's append-only design
    (see module docstring). Any other cell in that row (including anything a
    human typed into Notes or a custom column) is left completely untouched.

    Returns True if a matching row was found and updated, False if `job_id`
    isn't present in the sheet (nothing to update — not an error)."""
    worksheet = _open_worksheet(config)
    live_header = _ensure_headers(worksheet)
    values = worksheet.get_all_values()
    if len(values) <= 1 or "Job ID" not in live_header:
        return False
    id_col = live_header.index("Job ID")
    row_number = next(
        (i for i, row in enumerate(values[1:], start=2)
         if len(row) > id_col and row[id_col] == job_id),
        None,
    )
    if row_number is None:
        return False
    batch = []
    for col_name, value in updates.items():
        if col_name not in live_header:
            continue  # never silently add a column here; append_rows/_ensure_headers own that
        col_index = live_header.index(col_name) + 1  # gspread cell coordinates are 1-indexed
        batch.append({"range": gspread.utils.rowcol_to_a1(row_number, col_index), "values": [[value]]})
    if batch:
        worksheet.batch_update(batch, value_input_option="USER_ENTERED")
    return True
