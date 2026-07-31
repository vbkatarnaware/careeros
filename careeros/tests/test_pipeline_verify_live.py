"""Tests for careeros/pipeline/verify_live.py.

The two `test_catches_the_real_*` cases at the bottom are the reason this
module exists: both are the actual 2026-07-31 misses, where the provider's
own metadata was wrong and the pipeline scored honestly against bad data.
If either ever stops firing, the stage has lost its whole point."""

from __future__ import annotations

from careeros.pipeline.verify_live import (
    check_job, detect_work_arrangement, extract_required_years,
)


# ── extract_required_years ───────────────────────────────────────────────

def test_extracts_plus_form():
    assert extract_required_years("We require 7+ years of experience in product.") == [7]


def test_extracts_minimum_and_at_least_forms():
    assert extract_required_years("Minimum 5 years experience required.") == [5]
    assert extract_required_years("Candidates must have at least 4 years experience.") == [4]


def test_range_form_uses_the_lower_bound():
    """'5-10 years' means the bar is 5, not 10 — using the upper bound would
    overstate how exclusionary the posting is."""
    assert extract_required_years("Looking for 5-10 years of relevant experience.") == [5]


def test_ignores_years_with_no_requirement_context():
    """Company history is not a bar on the candidate."""
    assert extract_required_years("We have been building great products for 10 years.") == []


def test_ignores_implausible_year_counts():
    assert extract_required_years("Our 50 years experience serving customers.") == []


def test_returns_all_distinct_requirements_sorted():
    text = "Requires 3+ years experience. Senior track needs a minimum 8 years experience."
    assert extract_required_years(text) == [3, 8]


def test_no_years_mentioned_returns_empty():
    assert extract_required_years("A great role for a product person.") == []


# ── detect_work_arrangement ──────────────────────────────────────────────

def test_detects_hybrid_and_onsite_markers():
    assert "hybrid" in detect_work_arrangement("This role is Hybrid, 3 days in office.")
    assert "in office" in detect_work_arrangement("This role is Hybrid, 3 days in office.")


def test_detects_remote_markers():
    assert "fully remote" in detect_work_arrangement("This is a fully remote position.")


# ── check_job ────────────────────────────────────────────────────────────

_BASE = dict(job_id="j1", title="Product Manager", company="Acme",
             fetch_reason=None, max_years_ok=3,
             provider_said_remote=True, onsite_ok_cities=["Mumbai", "Navi Mumbai"],
             job_location="India")


def test_clean_posting_raises_no_concerns():
    c = check_job(**{**_BASE, "page_text": "Fully remote role. 2+ years experience required."})
    assert c.concerns == []
    assert c.fetched is True


def test_unfetchable_page_flags_inability_to_verify_not_a_job_problem():
    c = check_job(**{**_BASE, "page_text": None, "fetch_reason": "login_wall"})
    assert c.fetched is False
    assert len(c.concerns) == 1
    assert "could not read the live posting" in c.concerns[0]
    assert "login_wall" in c.concerns[0]


def test_years_below_candidate_bar_is_never_a_concern():
    c = check_job(**{**_BASE, "page_text": "Remote. Minimum 2 years experience."})
    assert c.concerns == []


def test_hybrid_in_an_accepted_onsite_city_is_not_flagged():
    c = check_job(**{**_BASE, "page_text": "Hybrid role, 3 days in office.",
                     "job_location": "Mumbai, Maharashtra, India"})
    assert c.concerns == []


def test_hybrid_outside_accepted_cities_is_flagged():
    c = check_job(**{**_BASE, "page_text": "Hybrid role, 3 days in office.",
                     "job_location": "Berlin, Germany"})
    assert any("said REMOTE" in x for x in c.concerns)


def test_provider_said_onsite_already_so_hybrid_is_not_a_contradiction():
    c = check_job(**{**_BASE, "page_text": "Hybrid role.", "provider_said_remote": False,
                     "job_location": "Berlin, Germany"})
    assert c.concerns == []


# ── the two real 2026-07-31 misses ───────────────────────────────────────

def test_catches_the_real_au_small_finance_bank_miss():
    """Provider said ai_experience_level '2-5' and sent a 1,509-char
    description that never mentioned an experience bar. The live posting
    asks for 7 years; the candidate has 3."""
    live_page = (
        "Technical Product Manager - AU Small Finance Bank. "
        "Seeking a Product Manager with 7+ years of experience in Commercial "
        "Banking and Lending products who can work with business stakeholders."
    )
    c = check_job(**{**_BASE, "page_text": live_page, "title": "Technical Product Manager",
                     "company": "AU SMALL FINANCE BANK", "provider_said_remote": False,
                     "job_location": "Navi Mumbai, Maharashtra, India"})
    assert c.max_years_required == 7
    assert any("7 years" in x for x in c.concerns), c.concerns


def test_catches_the_real_mdotm_miss():
    """Provider's `remote` field said True. The live BambooHR posting says
    'Milano (Hybrid)' — and Milan is not an accepted onsite city."""
    live_page = (
        "Product Manager - MDOTM. Milano (Hybrid). "
        "MDOTM is a global leading provider of AI-driven investment solutions."
    )
    c = check_job(**{**_BASE, "page_text": live_page, "title": "Product Manager",
                     "company": "MDOTM", "provider_said_remote": True,
                     "job_location": "Milan, Lombardy, Italy"})
    assert any("said REMOTE" in x for x in c.concerns), c.concerns
