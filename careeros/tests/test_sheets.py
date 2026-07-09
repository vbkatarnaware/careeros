"""Tests for the pure, network-free parts of careeros/sheets.py: row shape,
header consistency, and the header-name-keyed read/write + additive migration
(mocked worksheet — no gspread/live credentials). Anything touching real
credentials is out of scope (see README's Testing section)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from careeros.models import Contact, Eval, Rubric
from careeros.sheets import (
    SHEET_HEADERS,
    _ensure_headers,
    append_rows,
    job_to_row,
    read_existing_job_ids,
)
from careeros.tests.conftest import make_job

# The 15-column schema shipped BEFORE "Company LinkedIn" and "Drive Folder"
# were added. Sheets created then have this header; the migration + name-keyed
# read/write must keep working against it (regression for the P2.8 schema-drift
# bug where positional indexing silently broke dedupe + misaligned appends).
OLD_15_COL_HEADER = [
    "Date", "Company", "Role", "Score", "Confidence", "Recommendation",
    "Apply URL", "Resume Path", "Cover Letter Path", "Report Path",
    "Source", "Hiring Contact", "Contact LinkedIn", "Contact Email", "Job ID",
]


def make_eval(**overrides) -> Eval:
    defaults = dict(
        id="job-1", score=4.2, confidence=0.8, recommendation="apply",
        strengths=["a", "b", "c"], weaknesses=["x", "y"], ats_keywords=[],
        company_summary="s", fit_paragraph="f",
        rubric=Rubric(role_fit=4, seniority_fit=4, skills_match=4, domain=4, logistics=4),
        prompt_version="v2", profile_version=1, job_hash="h",
    )
    defaults.update(overrides)
    return Eval(**defaults)


def test_job_to_row_length_matches_headers():
    job = make_job(id="job-1", company_linkedin="https://www.linkedin.com/company/acme")
    row = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md")
    assert len(row) == len(SHEET_HEADERS)


def test_job_to_row_includes_company_linkedin_in_the_right_column():
    job = make_job(id="job-1", company_linkedin="https://www.linkedin.com/company/acme")
    row = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md")
    idx = SHEET_HEADERS.index("Company LinkedIn")
    assert row[idx] == "https://www.linkedin.com/company/acme"


def test_job_to_row_company_linkedin_blank_when_missing():
    job = make_job(id="job-1", company_linkedin=None)
    row = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md")
    idx = SHEET_HEADERS.index("Company LinkedIn")
    assert row[idx] == ""


def test_job_id_is_still_the_last_column():
    """Job ID stays last in the CANONICAL schema by convention (readable, and
    new columns append before it). Correctness no longer depends on this —
    read_existing_job_ids() locates Job ID by header NAME — but keeping the
    convention avoids churn for anyone eyeballing the sheet."""
    assert SHEET_HEADERS[-1] == "Job ID"


def test_job_to_row_drive_folder_blank_by_default():
    """No drive_folder_link passed (Drive disabled or not run) -> blank cell,
    not an error — sheets.py never depends on drive.py."""
    job = make_job(id="job-1")
    row = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md")
    assert row[SHEET_HEADERS.index("Drive Folder")] == ""


def test_job_to_row_includes_drive_folder_link_when_given():
    job = make_job(id="job-1")
    row = job_to_row(
        "2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md",
        drive_folder_link="https://drive.google.com/drive/folders/abc123",
    )
    assert row[SHEET_HEADERS.index("Drive Folder")] == "https://drive.google.com/drive/folders/abc123"


def test_job_to_row_contact_fields_unaffected_by_company_linkedin_addition():
    job = make_job(id="job-1", contact=Contact(name="Jane", linkedin="https://li/jane", email="j@x.com"))
    row = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md")
    assert row[SHEET_HEADERS.index("Hiring Contact")] == "Jane"
    assert row[SHEET_HEADERS.index("Contact LinkedIn")] == "https://li/jane"
    assert row[SHEET_HEADERS.index("Contact Email")] == "j@x.com"


def test_job_to_row_defaults_to_apply_tier_blank_notes():
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval(), "r.md", "c.md", "rep.md")
    assert row[SHEET_HEADERS.index("Tier")] == "Apply"
    assert row[SHEET_HEADERS.index("Notes")] == ""


def test_job_to_row_consider_tier_blank_artifacts_with_note():
    """A CONSIDER row: no artifacts (blank path cells), tier label, and a note."""
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval(),
                     resume_path="", cover_path="", report_path="",
                     tier="Consider", notes="Consider (scored 3.7, below 4): domain gap")
    assert row[SHEET_HEADERS.index("Tier")] == "Consider"
    assert row[SHEET_HEADERS.index("Notes")].startswith("Consider (scored 3.7")
    assert row[SHEET_HEADERS.index("Resume Path")] == ""
    assert row[SHEET_HEADERS.index("Cover Letter Path")] == ""
    assert row[SHEET_HEADERS.index("Report Path")] == ""


# ── schema-drift regression (P2.8): name-keyed read/write + additive migrate ──

def _mock_ws(rows):
    """A fake gspread Worksheet returning `rows` from get_all_values()."""
    ws = MagicMock()
    ws.get_all_values.return_value = rows
    ws.col_count = len(rows[0]) if rows else 0
    return ws


def test_read_existing_job_ids_finds_id_by_name_on_old_15col_sheet():
    """THE regression: on a 15-col sheet (Job ID at index 14, not the code's
    17-col index 16), positional indexing returned an empty set and silently
    broke dedupe. Name-keyed lookup must return the real ids."""
    data = [
        OLD_15_COL_HEADER,
        ["2026-07-07", "Razorpay", "PM II", "4.5", "0.8", "apply", "u", "r", "c", "rep", "src", "", "", "", "id-aaa"],
        ["2026-07-08", "Bjak", "PM", "4.2", "0.7", "apply", "u", "r", "c", "rep", "src", "", "", "", "id-bbb"],
    ]
    with patch("careeros.sheets._open_worksheet", return_value=_mock_ws(data)):
        ids = read_existing_job_ids(MagicMock())
    assert ids == {"id-aaa", "id-bbb"}  # NOT empty


def test_ensure_headers_additively_appends_missing_columns():
    ws = _mock_ws([OLD_15_COL_HEADER])
    result = _ensure_headers(ws)
    # missing canonical columns are appended at the END (in SHEET_HEADERS order);
    # nothing existing is reordered. An old 15-col sheet is missing 4 columns.
    expected_missing = [h for h in SHEET_HEADERS if h not in OLD_15_COL_HEADER]
    assert result == OLD_15_COL_HEADER + expected_missing
    ws.add_cols.assert_called_once_with(len(expected_missing))
    written = ws.update.call_args.args[0]
    assert written == [OLD_15_COL_HEADER + expected_missing]


def test_ensure_headers_noop_when_already_current():
    ws = _mock_ws([list(SHEET_HEADERS)])
    result = _ensure_headers(ws)
    assert result == list(SHEET_HEADERS)
    ws.update.assert_not_called()
    ws.add_cols.assert_not_called()


def test_ensure_headers_writes_header_on_empty_sheet():
    ws = _mock_ws([])
    result = _ensure_headers(ws)
    assert result == list(SHEET_HEADERS)
    assert ws.update.call_args.args[0] == [list(SHEET_HEADERS)]


def test_append_rows_realigns_canonical_row_to_drifted_live_header():
    """A canonical (17-wide, SHEET_HEADERS order) row must be reordered to the
    live header BY NAME, so no cell lands under the wrong column even when the
    live sheet is the migrated 15->17 layout (Job ID stays at index 14, the two
    new columns are last)."""
    ws = _mock_ws([OLD_15_COL_HEADER])  # -> _ensure_headers appends the missing cols
    job = make_job(id="id-xyz", company="Acme", company_linkedin="https://li/acme")
    canonical = job_to_row("2026-07-08", job, make_eval(), "r.md", "c.md", "rep.md",
                           drive_folder_link="https://drive/f")
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        append_rows(MagicMock(), [canonical])

    appended = ws.append_rows.call_args.args[0][0]
    live_header = OLD_15_COL_HEADER + [h for h in SHEET_HEADERS if h not in OLD_15_COL_HEADER]
    by_name = dict(zip(live_header, appended))
    assert by_name["Job ID"] == "id-xyz"
    assert by_name["Company"] == "Acme"
    assert by_name["Company LinkedIn"] == "https://li/acme"
    assert by_name["Drive Folder"] == "https://drive/f"
    assert by_name["Role"] == job.title  # Role cell is NOT clobbered by Company LinkedIn
