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
    "Apply URL", "Resume Path", "Cover Letter Path", "Report Path",
    "Source", "Hiring Contact", "Contact LinkedIn", "Contact Email",
    "Drive Folder", "Job ID",
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


def read_existing_job_ids(config: Config) -> set[str]:
    """Job ID is the last column — read it back for dedupe-against-sheet."""
    worksheet = _open_worksheet(config)
    values = worksheet.get_all_values()
    if len(values) <= 1:
        return set()
    id_col = SHEET_HEADERS.index("Job ID")
    return {row[id_col] for row in values[1:] if len(row) > id_col and row[id_col]}


def job_to_row(
    date: str, job: Job, evaluation: Eval,
    resume_path: str, cover_path: str, report_path: str,
    drive_folder_link: str = "",
) -> list[Any]:
    """`drive_folder_link` is blank unless the optional Drive backup (P2.6,
    `drive.enabled: true`) ran successfully for this job — sheets.py has no
    import dependency on drive.py; the caller (cli.py) resolves the link and
    passes it in, keeping the two modules decoupled."""
    contact = job.contact
    return [
        date, job.company, job.company_linkedin or "", job.title,
        evaluation.score, evaluation.confidence, evaluation.recommendation,
        job.apply_url, resume_path, cover_path, report_path,
        job.source,
        contact.name if contact else "",
        contact.linkedin if contact else "",
        contact.email if contact else "",
        drive_folder_link,
        job.id,
    ]


def append_rows(config: Config, rows: list[list[Any]]) -> None:
    if not rows:
        return
    worksheet = _open_worksheet(config)
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
