"""Google Sheets integration. Deterministic — no AI involved.

Google Sheets is the primary OUTPUT for v1 (not a database). This module's
core operation is still new-rows-only for what `daily` writes — an existing
row is left for the human to edit freely (Status, Notes, whatever they want
to track there) unless explicitly targeted.

Phase 3 (v1.1) adds ONE narrow, deliberate exception to "never edit an
existing row": `update_row_by_job_id()`, used by `careeros backfill-drive`
and `careeros publish` to add Drive links to a specific row. It touches ONLY
the specific columns it's told to — never anything else in that row, so any
human notes/status elsewhere in the row are untouched.

P2.10 replaces the local-filesystem-path columns (Resume/Cover Letter/Report
Path) and the redundant Drive Folder column (there is only ever one project
folder) with real per-artifact Drive links, including two new artifacts the
Sheet previously had no column for: the Evaluation report and Application
Answers (see `careeros/apply/`). `_ensure_headers` now both ADDS missing
canonical columns and REMOVES the deprecated ones (by exact name only — a
user's own custom column is never touched), so an existing Sheet cleans
itself up on the next write; `migrate()` exposes the same pass standalone
(`careeros sheets migrate`) for cleaning up right now.

P2.11 adds a `Status` application-tracking column (dropdown: Not Applied
by default, Applied, Received Call, Interview, ...) and switches new rows
from appending at the bottom to inserting directly below the header — see
`append_rows`' docstring for why the newest run should read at the TOP, not
buried under a growing history. `migrate()` also does a ONE-TIME sort of
whatever's already in the Sheet by Date descending, so a Sheet from before
this change gets fixed too, not just future writes.
"""

from __future__ import annotations

from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from careeros.config import Config
from careeros.models import Eval, Job

SHEET_HEADERS = [
    "Date", "Company", "Role", "Score", "Tier", "Recommendation", "Confidence",
    "Apply URL", "Status", "Resume (Drive)", "Cover Letter (Drive)", "Evaluation (Drive)",
    "Deep Report (Drive)", "Application Answers (Drive)", "Notes",
    "Source", "Company LinkedIn", "Hiring Contact", "Contact LinkedIn", "Contact Email",
    "Job ID",
]

# `Status` (P2.11) is a human-tracking column, not a pipeline output — the
# pipeline only ever writes the DEFAULT value on a NEW row (see `job_to_row`);
# every later change to it is the candidate's own edit and, like Notes, is
# never touched again by `append_rows` or `sheets migrate` (both only ever
# reorder/reformat COLUMNS, never overwrite existing row DATA). The dropdown
# (see `_apply_formatting`) constrains it to these values so the column stays
# usable for filtering/reporting instead of drifting into free text.
STATUS_OPTIONS = [
    "Not Applied", "Applied", "Received Call", "Interview",
    "After Interview", "Ongoing / In Process", "Offer", "Rejected",
]
DEFAULT_STATUS = STATUS_OPTIONS[0]

# CareerOS-owned columns retired in P2.10 (v1.1): three local filesystem paths
# no human could click, and a Drive Folder link that's redundant once every
# job already has its own direct file links (there's only ever one project
# folder). Matched by EXACT name only in `_ensure_headers` — never by
# position — so only these specific retired names are ever removed; any
# other (including user-added) column is untouched.
DEPRECATED_HEADERS = ["Resume Path", "Cover Letter Path", "Report Path", "Drive Folder"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Header row styling + Score conditional-format colors (P2.10). Pastel fills
# chosen for legibility, not saturated color-blocking.
_HEADER_BG_COLOR = {"red": 0.85, "green": 0.88, "blue": 0.95}
_SCORE_GREEN = {"red": 0.85, "green": 0.92, "blue": 0.83}   # ~#D9EAD3
_SCORE_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.8}    # ~#FFF2CC


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
    """Return the worksheet's effective header row, migrating it to the
    current schema first: DEPRECATED_HEADERS columns are physically removed
    (exact name match only — never by position, never a user's own custom
    column), then any canonical SHEET_HEADERS column not already present is
    APPENDED at the end. Existing columns, their order, and any user-added
    columns are otherwise never moved or removed.

    This is also the migration path for a sheet created before a header was
    added (e.g. a 15-column sheet predating `Company LinkedIn`): the missing
    columns get appended, and because every read/write keys off the header
    BY NAME (never by hardcoded position), correctness is preserved
    regardless of the resulting column order."""
    values = worksheet.get_all_values()
    if not values or not any(values[0]):
        worksheet.update([SHEET_HEADERS], "A1")
        return list(SHEET_HEADERS)
    header = list(values[0])

    # Physically delete deprecated columns (highest index first, so removing
    # one never shifts the index of another still queued for removal).
    deprecated_indices = sorted(
        (header.index(h) for h in DEPRECATED_HEADERS if h in header), reverse=True
    )
    for idx in deprecated_indices:
        worksheet.delete_columns(idx + 1)  # gspread column indices are 1-indexed
        del header[idx]

    missing = [h for h in SHEET_HEADERS if h not in header]
    if missing:
        new_header = header + missing
        extra_cols = len(new_header) - worksheet.col_count
        if extra_cols > 0:
            worksheet.add_cols(extra_cols)
        worksheet.update([new_header], "A1")
        return new_header

    if deprecated_indices:
        # Columns were physically removed but nothing new needed adding —
        # still must rewrite row 1 so the deprecated names disappear from it.
        worksheet.update([header], "A1")
    return header


def _score_conditional_rule_exists(sheet_meta: dict, sheet_id: int, score_col: int) -> bool:
    """True if a conditional-format rule already targets `score_col` on this
    sheet — the idempotency check so repeated `_apply_formatting` calls (every
    `append_rows`, or an explicit `sheets migrate`) never stack duplicate
    rules on top of each other."""
    for sheet in sheet_meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != sheet_id:
            continue
        for cf in sheet.get("conditionalFormats", []):
            for rng in cf.get("ranges", []):
                if rng.get("startColumnIndex") == score_col:
                    return True
    return False


def _apply_formatting(worksheet: gspread.Worksheet, live_header: list[str]) -> None:
    """Idempotent cosmetic pass: bold header row (frozen), a Score
    conditional-format rule (>= threshold -> light green, below -> light
    yellow), and a Status dropdown (STATUS_OPTIONS) — each applies to every
    row automatically, including ones written after this runs (unbounded
    row range, no endRowIndex), never a one-time per-cell paint/rule a fresh
    row would miss. Safe to call on every write: the Score rule checks the
    sheet's existing conditional-format rules first so repeated calls never
    duplicate it; the Status dropdown is a `setDataValidation` call, which
    REPLACES whatever was on that range rather than stacking, so it's
    naturally idempotent with no existence check needed."""
    worksheet.format("1:1", {
        "textFormat": {"bold": True},
        "backgroundColor": _HEADER_BG_COLOR,
    })
    worksheet.freeze(rows=1)

    spreadsheet = worksheet.spreadsheet
    sheet_id = worksheet.id
    requests = []

    if "Score" in live_header:
        score_col = live_header.index("Score")  # 0-indexed, Sheets API convention
        meta = spreadsheet.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
        )
        if not _score_conditional_rule_exists(meta, sheet_id, score_col):
            score_range = {
                "sheetId": sheet_id, "startRowIndex": 1,
                "startColumnIndex": score_col, "endColumnIndex": score_col + 1,
            }
            requests += [
                {"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [score_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "4"}]},
                        "format": {"backgroundColor": _SCORE_GREEN},
                    },
                }}},
                {"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [score_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "4"}]},
                        "format": {"backgroundColor": _SCORE_YELLOW},
                    },
                }}},
            ]

    if "Status" in live_header:
        status_col = live_header.index("Status")
        requests.append({"setDataValidation": {
            "range": {
                "sheetId": sheet_id, "startRowIndex": 1,
                "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in STATUS_OPTIONS],
                },
                "showCustomUi": True,
                "strict": True,
            },
        }})

    if requests:
        spreadsheet.batch_update({"requests": requests})


def _canonical_order(header: list[str]) -> list[str]:
    """SHEET_HEADERS' columns (that are present) in canonical order, followed
    by any column not in SHEET_HEADERS (a user's own custom column) kept in
    its original relative order — never dropped or overwritten."""
    return [h for h in SHEET_HEADERS if h in header] + [h for h in header if h not in SHEET_HEADERS]


def _reorder_and_fill(worksheet: gspread.Worksheet, header: list[str]) -> list[str]:
    """One combined, idempotent pass over the live data (called after
    `_ensure_headers` has already added/removed columns):

    1. Reorder columns to match SHEET_HEADERS' canonical order, by NAME
       (see `_canonical_order`).
    2. Fill every blank data cell with "-" so no row/column ever reads as
       empty. The header row itself is never touched by this step.

    Both existing rows AND the header are rewritten in a single `update()`
    call only if something actually changed — a sheet already in canonical
    order with no blank cells makes no API call at all."""
    target = _canonical_order(header)
    values = worksheet.get_all_values()
    data_rows = values[1:] if values else []

    reorder_needed = target != header
    index_map = [header.index(h) for h in target]

    new_rows = []
    changed = reorder_needed
    for row in data_rows:
        padded = (row + [""] * len(header))[:len(header)]
        reordered = [padded[i] for i in index_map]
        filled = ["-" if cell == "" else cell for cell in reordered]
        if filled != reordered:
            changed = True
        new_rows.append(filled)

    if not changed:
        return target

    worksheet.update([target] + new_rows, "A1", value_input_option="USER_ENTERED")
    return target


def _sort_rows_by_date_desc(worksheet: gspread.Worksheet, header: list[str]) -> bool:
    """ONE-TIME fix (called only from `migrate()`, never from `append_rows`
    — see module docstring) for a Sheet whose history was built before
    P2.11, when every day's rows landed at the BOTTOM (oldest-on-top).
    `append_rows` now inserts new rows at the top going forward, so this
    never needs to run again on an already-current Sheet — it's here purely
    to fix what's already there.

    A stable sort (Python's `sorted` preserves original relative order among
    equal keys even with `reverse=True`) on the `Date` column string, which
    sorts correctly newest-first as long as dates are ISO `YYYY-MM-DD`
    (lexicographic order == chronological order) — no date parsing needed.
    Rows sharing a date keep their original relative order; only the
    day-to-day block order changes. No-ops (no API call) if already sorted.

    Returns whether anything changed."""
    if "Date" not in header:
        return False
    date_col = header.index("Date")
    values = worksheet.get_all_values()
    data_rows = values[1:] if values else []
    if len(data_rows) < 2:
        return False

    sorted_rows = sorted(
        data_rows, key=lambda r: r[date_col] if date_col < len(r) else "", reverse=True
    )
    if sorted_rows == data_rows:
        return False

    worksheet.update([header] + sorted_rows, "A1", value_input_option="USER_ENTERED")
    return True


def migrate(config: Config) -> dict[str, Any]:
    """Explicit, on-demand migration pass (`careeros sheets migrate`) — the
    same removal/addition + reorder/blank-fill + formatting logic
    `append_rows` already runs on every call, exposed standalone so an
    existing Sheet can be cleaned up right now rather than waiting for the
    next `daily` run. Also runs the one-time `_sort_rows_by_date_desc` fix
    (see its docstring) for a Sheet whose history predates P2.11. Returns
    {"removed": [...], "added": [...], "reordered": bool,
    "blanks_filled": bool, "date_sorted": bool} for the CLI to report."""
    worksheet = _open_worksheet(config)
    values = worksheet.get_all_values()
    before_header = values[0] if values and any(values[0]) else []
    had_blanks = any(cell == "" for row in values[1:] for cell in row)
    header_after_ensure = _ensure_headers(worksheet)
    reordered = header_after_ensure != _canonical_order(header_after_ensure)
    live_header = _reorder_and_fill(worksheet, header_after_ensure)
    _apply_formatting(worksheet, live_header)
    date_sorted = _sort_rows_by_date_desc(worksheet, live_header)
    return {
        "removed": [h for h in before_header if h not in live_header],
        "added": [h for h in live_header if h not in before_header],
        "reordered": reordered,
        "blanks_filled": had_blanks,
        "date_sorted": date_sorted,
    }


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
    resume_drive_link: str = "",
    cover_drive_link: str = "",
    eval_drive_link: str = "",
    deep_report_drive_link: str = "",
    answers_drive_link: str = "",
    tier: str = "Apply",
    notes: str = "",
    status: str = DEFAULT_STATUS,
) -> list[Any]:
    """Build one Sheet row (in canonical SHEET_HEADERS order; append_rows then
    reorders it to the live header by name).

    `tier` is "Apply" (score >= threshold: full pipeline) or "Consider"
    (near-miss: Sheet-only). For a Consider row the caller passes blank Drive
    links (no artifacts were generated) and a `notes` string explaining why
    it fell short of Apply.

    Every `*_drive_link` param is a direct, per-job clickable link to that
    job's own file in Drive — one click to the exact file, no local paths, no
    shared-folder link (P2.10 dropped both; see DEPRECATED_HEADERS). All are
    blank unless the corresponding artifact was actually generated AND
    uploaded. `answers_drive_link` may also be the literal string "Manual
    questions required" (see careeros/apply/) rather than a URL — the caller
    decides which to pass; this function just places whatever string it's
    given. sheets.py has no import dependency on drive.py; cli.py resolves
    the links and passes them in, keeping the two modules decoupled.

    `status` is ONLY ever set here, on a brand-new row — see STATUS_OPTIONS'
    comment for why the pipeline never touches it again afterward.

    Every blank field is rendered as "-", not "", so no cell in the Sheet
    ever reads as empty (same rule `_reorder_and_fill` applies retroactively
    to already-appended rows)."""
    contact = job.contact
    row = [
        date, job.company, job.title,
        evaluation.score, tier, evaluation.recommendation, evaluation.confidence,
        job.apply_url, status,
        resume_drive_link, cover_drive_link, eval_drive_link, deep_report_drive_link,
        answers_drive_link, notes,
        job.source, job.company_linkedin or "",
        contact.name if contact else "",
        contact.linkedin if contact else "",
        contact.email if contact else "",
        job.id,
    ]
    return ["-" if v == "" else v for v in row]


def append_rows(config: Config, rows: list[list[Any]]) -> None:
    """Insert rows (each in canonical SHEET_HEADERS order, e.g. from
    `job_to_row`) as a block directly below the header (row 2), pushing
    every existing row down — so each day's newest run reads at the TOP of
    the Sheet, not buried at the bottom after a growing history (P2.11).
    Kept the name `append_rows` (an insert, despite the name) since every
    caller already treats this as "add these rows to the Sheet" — renaming
    would touch every call site for no behavioral reason.

    The rows are reordered to match the LIVE header BY NAME first, so
    columns always land under the right header even on a sheet whose column
    order/count differs from SHEET_HEADERS. Missing canonical columns are
    auto-added and deprecated ones auto-removed first (see `_ensure_headers`),
    existing rows are reordered to canonical column order and blank-filled
    with "-" (see `_reorder_and_fill`), and formatting is (re)applied — so
    every write keeps the Sheet clean and styled, not just an explicit
    `sheets migrate`."""
    if not rows:
        return
    worksheet = _open_worksheet(config)
    live_header = _ensure_headers(worksheet)
    live_header = _reorder_and_fill(worksheet, live_header)
    _apply_formatting(worksheet, live_header)
    aligned = []
    for row in rows:
        by_name = dict(zip(SHEET_HEADERS, row))
        aligned.append([by_name.get(col, "") for col in live_header])
    worksheet.insert_rows(aligned, row=2, value_input_option="USER_ENTERED")


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
