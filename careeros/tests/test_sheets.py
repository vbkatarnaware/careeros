"""Tests for the pure, network-free parts of careeros/sheets.py: row shape,
header consistency, the header-name-keyed read/write + additive migration,
the P2.10 deprecated-column removal, and formatting (mocked worksheet — no
gspread/live credentials). Anything touching real credentials is out of
scope (see README's Testing section)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from careeros.models import Contact, Eval, Rubric
from careeros.sheets import (
    DEPRECATED_HEADERS,
    SHEET_HEADERS,
    STATUS_OPTIONS,
    _apply_formatting,
    _ensure_headers,
    _score_conditional_rule_exists,
    _sort_rows_by_date_desc,
    append_rows,
    job_to_row,
    migrate,
    read_all_rows_with_job_id,
    read_existing_job_ids,
    update_row_by_job_id,
)
from careeros.tests.conftest import make_job

# The 15-column schema shipped BEFORE "Company LinkedIn" and "Drive Folder"
# were added. Sheets created then have this header; the migration + name-keyed
# read/write must keep working against it (regression for the P2.8 schema-drift
# bug where positional indexing silently broke dedupe).
OLD_15_COL_HEADER = [
    "Date", "Company", "Role", "Score", "Confidence", "Recommendation",
    "Apply URL", "Resume Path", "Cover Letter Path", "Report Path",
    "Source", "Hiring Contact", "Contact LinkedIn", "Contact Email", "Job ID",
]

# The 21-column schema (P2.8-P2.9 era) that P2.10 migrates away from: three
# local-path columns + the shared Drive Folder link, both retired.
PRE_P2_10_HEADER = [
    "Date", "Company", "Company LinkedIn", "Role", "Score", "Confidence", "Recommendation",
    "Tier", "Apply URL", "Resume Path", "Cover Letter Path", "Report Path",
    "Source", "Hiring Contact", "Contact LinkedIn", "Contact Email",
    "Drive Folder", "Resume (Drive)", "Cover Letter (Drive)", "Notes", "Job ID",
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
    row = job_to_row("2026-07-08", job, make_eval())
    assert len(row) == len(SHEET_HEADERS)


def test_job_to_row_includes_company_linkedin_in_the_right_column():
    job = make_job(id="job-1", company_linkedin="https://www.linkedin.com/company/acme")
    row = job_to_row("2026-07-08", job, make_eval())
    idx = SHEET_HEADERS.index("Company LinkedIn")
    assert row[idx] == "https://www.linkedin.com/company/acme"


def test_job_to_row_company_linkedin_blank_when_missing():
    """Blank cells render as "-" so no cell in the Sheet ever reads empty."""
    job = make_job(id="job-1", company_linkedin=None)
    row = job_to_row("2026-07-08", job, make_eval())
    idx = SHEET_HEADERS.index("Company LinkedIn")
    assert row[idx] == "-"


def test_job_id_is_still_the_last_column():
    """Job ID stays last in the CANONICAL schema by convention (readable, and
    new columns append before it). Correctness no longer depends on this —
    read_existing_job_ids() locates Job ID by header NAME — but keeping the
    convention avoids churn for anyone eyeballing the sheet."""
    assert SHEET_HEADERS[-1] == "Job ID"


def test_sheet_headers_has_no_deprecated_columns():
    for h in DEPRECATED_HEADERS:
        assert h not in SHEET_HEADERS


def test_job_to_row_defaults_to_apply_tier_blank_notes():
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval())
    assert row[SHEET_HEADERS.index("Tier")] == "Apply"
    assert row[SHEET_HEADERS.index("Notes")] == "-"


def test_job_to_row_consider_tier_blank_drive_links_with_note():
    """A CONSIDER row: no Drive links, tier label, and a note."""
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval(),
                     tier="Consider", notes="Consider (scored 3.7, below 4): domain gap")
    assert row[SHEET_HEADERS.index("Tier")] == "Consider"
    assert row[SHEET_HEADERS.index("Notes")].startswith("Consider (scored 3.7")
    assert row[SHEET_HEADERS.index("Resume (Drive)")] == "-"
    assert row[SHEET_HEADERS.index("Evaluation (Drive)")] == "-"


def test_job_to_row_contact_fields_unaffected_by_company_linkedin_addition():
    job = make_job(id="job-1", contact=Contact(name="Jane", linkedin="https://li/jane", email="j@x.com"))
    row = job_to_row("2026-07-08", job, make_eval())
    assert row[SHEET_HEADERS.index("Hiring Contact")] == "Jane"
    assert row[SHEET_HEADERS.index("Contact LinkedIn")] == "https://li/jane"
    assert row[SHEET_HEADERS.index("Contact Email")] == "j@x.com"


# ── P2.10: new Drive-link columns, no local paths, no shared-folder link ────

def test_job_to_row_includes_all_five_drive_links():
    job = make_job(id="job-1")
    row = job_to_row(
        "2026-07-08", job, make_eval(),
        resume_drive_link="https://drive/resume.pdf",
        cover_drive_link="https://drive/cover.pdf",
        eval_drive_link="https://drive/eval.md",
        deep_report_drive_link="https://drive/deep.md",
        answers_drive_link="https://drive/answers.pdf",
    )
    assert row[SHEET_HEADERS.index("Resume (Drive)")] == "https://drive/resume.pdf"
    assert row[SHEET_HEADERS.index("Cover Letter (Drive)")] == "https://drive/cover.pdf"
    assert row[SHEET_HEADERS.index("Evaluation (Drive)")] == "https://drive/eval.md"
    assert row[SHEET_HEADERS.index("Deep Report (Drive)")] == "https://drive/deep.md"
    assert row[SHEET_HEADERS.index("Application Answers (Drive)")] == "https://drive/answers.pdf"


def test_job_to_row_drive_links_blank_by_default():
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval())
    for col in ("Resume (Drive)", "Cover Letter (Drive)", "Evaluation (Drive)",
                "Deep Report (Drive)", "Application Answers (Drive)"):
        assert row[SHEET_HEADERS.index(col)] == "-"


def test_job_to_row_answers_link_accepts_manual_required_label():
    """answers_drive_link may be the literal manual-required status string
    rather than a URL -- job_to_row just places whatever it's given; the
    caller (cli.py sheets_append) decides which to pass."""
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval(),
                     answers_drive_link="Manual questions required")
    assert row[SHEET_HEADERS.index("Application Answers (Drive)")] == "Manual questions required"


# ── P2.11: Status (application-tracking) column ──────────────────────────

def test_job_to_row_status_defaults_to_not_applied():
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval())
    assert row[SHEET_HEADERS.index("Status")] == "Not Applied"


def test_job_to_row_status_is_right_after_apply_url():
    assert SHEET_HEADERS.index("Status") == SHEET_HEADERS.index("Apply URL") + 1


def test_sheet_headers_column_grouping():
    """P2.11 (user-requested reorder): Status directly after Apply URL, then
    the five artifact/Drive columns + Notes as one block, then the
    Source/contact-info columns as another block, Job ID last."""
    idx = {h: i for i, h in enumerate(SHEET_HEADERS)}
    drive_block = [
        "Resume (Drive)", "Cover Letter (Drive)", "Evaluation (Drive)",
        "Deep Report (Drive)", "Application Answers (Drive)", "Notes",
    ]
    contact_block = ["Source", "Company LinkedIn", "Hiring Contact", "Contact LinkedIn", "Contact Email"]
    assert [idx[h] for h in drive_block] == list(range(idx["Status"] + 1, idx["Status"] + 1 + len(drive_block)))
    assert [idx[h] for h in contact_block] == list(
        range(idx["Notes"] + 1, idx["Notes"] + 1 + len(contact_block))
    )
    assert SHEET_HEADERS[-1] == "Job ID"


def test_job_to_row_status_accepts_override():
    row = job_to_row("2026-07-08", make_job(id="job-1"), make_eval(), status="Interview")
    assert row[SHEET_HEADERS.index("Status")] == "Interview"


# ── schema-drift regression (P2.8): name-keyed read/write + additive migrate ──

def _mock_ws(rows, spreadsheet=None):
    """A fake gspread Worksheet returning `rows` from get_all_values()."""
    ws = MagicMock()
    ws.get_all_values.return_value = rows
    ws.col_count = len(rows[0]) if rows else 0
    ws.spreadsheet = spreadsheet or MagicMock()
    return ws


def test_read_existing_job_ids_finds_id_by_name_on_old_15col_sheet():
    """THE regression: on a 15-col sheet (Job ID at index 14, not the code's
    current wider index), positional indexing returned an empty set and
    silently broke dedupe. Name-keyed lookup must return the real ids."""
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
    # Every deprecated column in OLD_15_COL_HEADER is removed, then every
    # canonical column not already present is appended at the end.
    for h in DEPRECATED_HEADERS:
        assert h not in result
    for h in SHEET_HEADERS:
        assert h in result


def test_ensure_headers_noop_when_already_current():
    ws = _mock_ws([list(SHEET_HEADERS)])
    result = _ensure_headers(ws)
    assert result == list(SHEET_HEADERS)
    ws.update.assert_not_called()
    ws.add_cols.assert_not_called()
    ws.delete_columns.assert_not_called()


def test_ensure_headers_writes_header_on_empty_sheet():
    ws = _mock_ws([])
    result = _ensure_headers(ws)
    assert result == list(SHEET_HEADERS)
    assert ws.update.call_args.args[0] == [list(SHEET_HEADERS)]


def test_ensure_headers_removes_deprecated_columns_by_exact_name():
    ws = _mock_ws([PRE_P2_10_HEADER])
    result = _ensure_headers(ws)
    for h in DEPRECATED_HEADERS:
        assert h not in result
    # Every column that WASN'T deprecated survives, in its original relative order.
    survivors = [h for h in PRE_P2_10_HEADER if h not in DEPRECATED_HEADERS]
    for h in survivors:
        assert h in result
    # The 3 new P2.10 columns got appended.
    for h in ("Evaluation (Drive)", "Deep Report (Drive)", "Application Answers (Drive)"):
        assert h in result


def test_ensure_headers_deletes_columns_highest_index_first():
    """Deleting lowest-index-first would shift later indices out from under
    a queued deletion -- must delete in descending order."""
    ws = _mock_ws([PRE_P2_10_HEADER])
    _ensure_headers(ws)
    deleted_indices = [call.args[0] for call in ws.delete_columns.call_args_list]
    assert deleted_indices == sorted(deleted_indices, reverse=True)


def test_ensure_headers_never_touches_a_users_own_custom_column():
    header = list(SHEET_HEADERS) + ["My Custom Notes"]
    ws = _mock_ws([header])
    result = _ensure_headers(ws)
    assert "My Custom Notes" in result
    ws.delete_columns.assert_not_called()


def test_ensure_headers_rewrites_row_when_only_removal_happened():
    """A sheet already has every canonical column PLUS a deprecated one --
    nothing to ADD, but row 1 must still be rewritten to drop the removed
    column's name."""
    header = list(SHEET_HEADERS) + ["Drive Folder"]
    ws = _mock_ws([header])
    result = _ensure_headers(ws)
    assert "Drive Folder" not in result
    ws.update.assert_called_once()


def test_append_rows_realigns_canonical_row_to_drifted_live_header():
    """A canonical row (SHEET_HEADERS order) must be reordered to the live
    header BY NAME, so no cell lands under the wrong column even on a sheet
    whose column order/count differs from SHEET_HEADERS."""
    ws = _mock_ws([OLD_15_COL_HEADER])  # -> _ensure_headers appends the missing cols
    job = make_job(id="id-xyz", company="Acme", company_linkedin="https://li/acme")
    canonical = job_to_row("2026-07-08", job, make_eval(), eval_drive_link="https://drive/eval.md")
    with patch("careeros.sheets._open_worksheet", return_value=ws), \
         patch("careeros.sheets._score_conditional_rule_exists", return_value=True):
        append_rows(MagicMock(), [canonical])

    # P2.11: new rows are INSERTED at row 2 (directly below the header), not
    # appended at the bottom -- see append_rows' docstring.
    assert ws.insert_rows.call_args.kwargs.get("row") == 2
    inserted = ws.insert_rows.call_args.args[0][0]
    # append_rows reorders the live header to canonical order before aligning
    # (see _reorder_and_fill) -- since every canonical column ends up present
    # (OLD_15_COL_HEADER's survivors + the columns _ensure_headers appends),
    # the final live header is exactly SHEET_HEADERS.
    by_name = dict(zip(SHEET_HEADERS, inserted))
    assert by_name["Job ID"] == "id-xyz"
    assert by_name["Company"] == "Acme"
    assert by_name["Company LinkedIn"] == "https://li/acme"
    assert by_name["Evaluation (Drive)"] == "https://drive/eval.md"
    assert by_name["Role"] == job.title  # Role cell is NOT clobbered by another column


def test_append_rows_applies_formatting():
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        append_rows(MagicMock(), [job_to_row("2026-07-08", make_job(id="job-1"), make_eval())])
    ws.format.assert_called_once()
    ws.freeze.assert_called_once_with(rows=1)
    ws.spreadsheet.batch_update.assert_called_once()


# ── migrate() / _apply_formatting(): P2.10 ───────────────────────────────

def test_migrate_reports_removed_and_added_columns():
    ws = _mock_ws([PRE_P2_10_HEADER])
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        result = migrate(MagicMock())
    assert set(result["removed"]) == set(DEPRECATED_HEADERS)
    assert set(result["added"]) == {
        "Status", "Evaluation (Drive)", "Deep Report (Drive)", "Application Answers (Drive)",
    }
    assert result["reordered"] is True  # PRE_P2_10_HEADER's order isn't canonical


def test_migrate_is_idempotent_on_an_already_current_sheet():
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        result = migrate(MagicMock())
    assert result == {
        "removed": [], "added": [], "reordered": False,
        "blanks_filled": False, "date_sorted": False,
    }


def test_migrate_reorders_and_fills_blanks_on_existing_data_rows():
    """The user's core P2.10 ask: existing rows get reordered to the
    canonical column sequence and every blank cell becomes "-", without
    losing any data."""
    header = list(SHEET_HEADERS)
    date_i, company_i, notes_i, job_id_i = (
        header.index("Date"), header.index("Company"), header.index("Notes"), header.index("Job ID"),
    )
    # A row with columns shuffled relative to canonical order, plus a blank cell.
    shuffled_header = [header[job_id_i], header[date_i], header[company_i], header[notes_i]]
    row = ["job-a", "2026-07-08", "Acme", ""]
    ws = _mock_ws([shuffled_header, row])
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        result = migrate(MagicMock())
    assert result["reordered"] is True
    assert result["blanks_filled"] is True
    written_rows = ws.update.call_args_list[-1].args[0]
    new_header, new_row = written_rows[0], written_rows[1]
    by_name = dict(zip(new_header, new_row))
    assert by_name["Job ID"] == "job-a"
    assert by_name["Date"] == "2026-07-08"
    assert by_name["Company"] == "Acme"
    assert by_name["Notes"] == "-"


# ── P2.11: one-time Date-descending sort for a pre-P2.11 Sheet ───────────

def test_sort_rows_by_date_desc_reorders_oldest_last():
    header = list(SHEET_HEADERS)
    date_i, company_i = header.index("Date"), header.index("Company")

    def _row(date, company):
        row = ["-"] * len(header)
        row[date_i], row[company_i] = date, company
        return row

    rows = [_row("2026-07-07", "Oldest"), _row("2026-07-10", "Newest"), _row("2026-07-08", "Middle")]
    ws = _mock_ws([header] + rows)
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        changed = _sort_rows_by_date_desc(ws, header)
    assert changed is True
    written = ws.update.call_args.args[0]
    companies = [row[company_i] for row in written[1:]]
    assert companies == ["Newest", "Middle", "Oldest"]


def test_sort_rows_by_date_desc_stable_within_same_date():
    """Rows sharing a date keep their original relative order -- only the
    day-to-day block order changes."""
    header = list(SHEET_HEADERS)
    date_i, company_i = header.index("Date"), header.index("Company")

    def _row(date, company):
        row = ["-"] * len(header)
        row[date_i], row[company_i] = date, company
        return row

    rows = [
        _row("2026-07-07", "OldA"), _row("2026-07-10", "NewA"),
        _row("2026-07-10", "NewB"), _row("2026-07-07", "OldB"),
    ]
    ws = _mock_ws([header] + rows)
    changed = _sort_rows_by_date_desc(ws, header)
    assert changed is True
    written = ws.update.call_args.args[0]
    companies = [row[company_i] for row in written[1:]]
    assert companies == ["NewA", "NewB", "OldA", "OldB"]


def test_sort_rows_by_date_desc_noop_when_already_sorted():
    header = list(SHEET_HEADERS)
    date_i = header.index("Date")

    def _row(date):
        row = ["-"] * len(header)
        row[date_i] = date
        return row

    rows = [_row("2026-07-10"), _row("2026-07-08"), _row("2026-07-07")]
    ws = _mock_ws([header] + rows)
    changed = _sort_rows_by_date_desc(ws, header)
    assert changed is False
    ws.update.assert_not_called()


def test_sort_rows_by_date_desc_noop_without_date_column():
    header = ["Company", "Role"]
    ws = _mock_ws([header, ["Acme", "PM"]])
    assert _sort_rows_by_date_desc(ws, header) is False
    ws.update.assert_not_called()


def test_migrate_reports_date_sorted_when_reordering_needed():
    header = list(SHEET_HEADERS)
    date_i, job_id_i = header.index("Date"), header.index("Job ID")

    def _row(date, job_id):
        row = ["-"] * len(header)
        row[date_i], row[job_id_i] = date, job_id
        return row

    rows = [_row("2026-07-07", "job-old"), _row("2026-07-10", "job-new")]
    ws = _mock_ws([header] + rows)
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        result = migrate(MagicMock())
    assert result["date_sorted"] is True


def test_apply_formatting_bolds_and_freezes_header():
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    _apply_formatting(ws, list(SHEET_HEADERS))
    ws.format.assert_called_once()
    fmt_args = ws.format.call_args.args
    assert fmt_args[1]["textFormat"]["bold"] is True
    ws.freeze.assert_called_once_with(rows=1)


def test_apply_formatting_adds_score_conditional_rules_once():
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.id = 42
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    _apply_formatting(ws, list(SHEET_HEADERS))
    ws.spreadsheet.batch_update.assert_called_once()
    requests = ws.spreadsheet.batch_update.call_args.args[0]["requests"]
    score_requests = [r for r in requests if "addConditionalFormatRule" in r]
    assert len(score_requests) == 2
    conditions = {
        r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["type"] for r in score_requests
    }
    assert conditions == {"NUMBER_GREATER_THAN_EQ", "NUMBER_LESS"}


def test_apply_formatting_skips_conditional_rules_when_already_present():
    """Idempotency: calling _apply_formatting twice must never stack
    duplicate conditional-format rules on the Score column. The Status
    dropdown's setDataValidation request still fires every time -- it
    REPLACES rather than stacks, so it needs no existence check (see
    _apply_formatting's docstring)."""
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.id = 42
    score_col = SHEET_HEADERS.index("Score")
    ws.spreadsheet.fetch_sheet_metadata.return_value = {
        "sheets": [{
            "properties": {"sheetId": 42},
            "conditionalFormats": [{"ranges": [{"startColumnIndex": score_col}]}],
        }]
    }
    _apply_formatting(ws, list(SHEET_HEADERS))
    ws.spreadsheet.batch_update.assert_called_once()
    requests = ws.spreadsheet.batch_update.call_args.args[0]["requests"]
    assert len(requests) == 1
    assert "setDataValidation" in requests[0]


def test_apply_formatting_sets_status_dropdown():
    ws = _mock_ws([list(SHEET_HEADERS)])
    ws.id = 42
    ws.spreadsheet.fetch_sheet_metadata.return_value = {"sheets": []}
    _apply_formatting(ws, list(SHEET_HEADERS))
    requests = ws.spreadsheet.batch_update.call_args.args[0]["requests"]
    status_requests = [r for r in requests if "setDataValidation" in r]
    assert len(status_requests) == 1
    rule = status_requests[0]["setDataValidation"]["rule"]
    values = [v["userEnteredValue"] for v in rule["condition"]["values"]]
    assert values == STATUS_OPTIONS
    assert rule["condition"]["type"] == "ONE_OF_LIST"
    status_col = SHEET_HEADERS.index("Status")
    rng = status_requests[0]["setDataValidation"]["range"]
    assert rng["startColumnIndex"] == status_col
    assert rng["endColumnIndex"] == status_col + 1
    # Unbounded row range (no endRowIndex) -- covers rows inserted later too.
    assert "endRowIndex" not in rng


def test_score_conditional_rule_exists_true_when_present():
    meta = {"sheets": [{"properties": {"sheetId": 1}, "conditionalFormats": [
        {"ranges": [{"startColumnIndex": 4}]},
    ]}]}
    assert _score_conditional_rule_exists(meta, sheet_id=1, score_col=4) is True


def test_score_conditional_rule_exists_false_when_absent():
    meta = {"sheets": [{"properties": {"sheetId": 1}, "conditionalFormats": []}]}
    assert _score_conditional_rule_exists(meta, sheet_id=1, score_col=4) is False


def test_apply_formatting_noop_when_no_score_column():
    ws = _mock_ws([["Date", "Company"]])
    _apply_formatting(ws, ["Date", "Company"])
    ws.spreadsheet.fetch_sheet_metadata.assert_not_called()
    ws.spreadsheet.batch_update.assert_not_called()


# ── read/update by job id (unchanged contract, new header shape) ────────

def _mock_ws_with_rows(header, rows):
    """rows: list of lists matching `header`'s column order."""
    ws = MagicMock()
    ws.get_all_values.return_value = [header] + rows
    ws.col_count = len(header)
    return ws


def test_read_all_rows_with_job_id_returns_dicts_plus_row_number():
    ws = _mock_ws_with_rows(list(SHEET_HEADERS), [
        ["2026-07-07"] + [""] * (len(SHEET_HEADERS) - 2) + ["job-a"],
        ["2026-07-08"] + [""] * (len(SHEET_HEADERS) - 2) + ["job-b"],
    ])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        rows = read_all_rows_with_job_id(MagicMock())
    assert len(rows) == 2
    assert rows[0]["Job ID"] == "job-a" and rows[0]["_row_number"] == 2
    assert rows[1]["Job ID"] == "job-b" and rows[1]["_row_number"] == 3


def test_read_all_rows_with_job_id_empty_sheet_returns_empty_list():
    ws = _mock_ws_with_rows(list(SHEET_HEADERS), [])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        assert read_all_rows_with_job_id(MagicMock()) == []


def test_update_row_by_job_id_touches_only_named_columns():
    header = list(SHEET_HEADERS)
    row = [""] * len(header)
    row[header.index("Job ID")] = "job-a"
    row[header.index("Notes")] = "a human wrote this — must survive untouched"
    ws = _mock_ws_with_rows(header, [row])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        found = update_row_by_job_id(MagicMock(), "job-a", {
            "Evaluation (Drive)": "https://drive/eval.md",
            "Resume (Drive)": "https://drive/r.pdf",
        })
    assert found is True
    batch_arg = ws.batch_update.call_args.args[0]
    updated_ranges = {b["range"]: b["values"][0][0] for b in batch_arg}
    assert len(batch_arg) == 2
    import gspread as gs
    expected_eval_cell = gs.utils.rowcol_to_a1(2, header.index("Evaluation (Drive)") + 1)
    expected_resume_cell = gs.utils.rowcol_to_a1(2, header.index("Resume (Drive)") + 1)
    assert updated_ranges[expected_eval_cell] == "https://drive/eval.md"
    assert updated_ranges[expected_resume_cell] == "https://drive/r.pdf"


def test_update_row_by_job_id_returns_false_when_job_id_not_found():
    header = list(SHEET_HEADERS)
    row = [""] * len(header)
    row[header.index("Job ID")] = "some-other-job"
    ws = _mock_ws_with_rows(header, [row])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        found = update_row_by_job_id(MagicMock(), "job-a", {"Resume (Drive)": "x"})
    assert found is False
    ws.batch_update.assert_not_called()


def test_update_row_by_job_id_ignores_unknown_column_names():
    """A defensive guard: update_row_by_job_id never silently invents a new
    column (including a now-deprecated one like "Drive Folder") — that's
    _ensure_headers/append_rows's job, not this function's."""
    header = list(SHEET_HEADERS)
    row = [""] * len(header)
    row[header.index("Job ID")] = "job-a"
    ws = _mock_ws_with_rows(header, [row])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        update_row_by_job_id(MagicMock(), "job-a", {"Drive Folder": "x"})
    ws.batch_update.assert_not_called()


def test_update_row_by_job_id_empty_sheet_returns_false():
    ws = _mock_ws_with_rows(list(SHEET_HEADERS), [])
    with patch("careeros.sheets._open_worksheet", return_value=ws):
        assert update_row_by_job_id(MagicMock(), "job-a", {"Resume (Drive)": "x"}) is False
