"""Tests for the pure, network-free parts of careeros/sheets.py: row shape
and header consistency. Anything touching gspread/live credentials is out of
scope (see README's Testing section)."""

from __future__ import annotations

from careeros.models import Contact, Eval, Rubric
from careeros.sheets import SHEET_HEADERS, job_to_row
from careeros.tests.conftest import make_job


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
    """read_existing_job_ids() relies on Job ID being the LAST column — this
    invariant must survive any future header additions."""
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
