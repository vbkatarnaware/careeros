"""Tests for careeros/pipeline/constraints.py — the deterministic hard
deal-breaker module. This is the fix for the real QA bug (an onsite-Bengaluru
job scored "apply" because a 10%-weighted logistics penalty got outweighed by
strong role/skills scores); these tests exist to make sure that regression
can never come back silently."""

from __future__ import annotations

from careeros.models import Salary
from careeros.pipeline.constraints import annual_inr, evaluate_constraints
from careeros.tests.conftest import FX_RATES, make_job, make_profile


# ── annual_inr ───────────────────────────────────────────────────────────

def test_annual_inr_converts_year_inr():
    salary = Salary(min=1_800_000, max=1_800_000, currency="INR", unit="year")
    assert annual_inr(salary, FX_RATES) == 1_800_000


def test_annual_inr_converts_month_to_year():
    salary = Salary(min=150_000, max=150_000, currency="INR", unit="month")
    assert annual_inr(salary, FX_RATES) == 150_000 * 12


def test_annual_inr_converts_usd_to_inr():
    salary = Salary(min=20_000, max=20_000, currency="USD", unit="year")
    assert annual_inr(salary, FX_RATES) == 20_000 * 83.0


def test_annual_inr_none_when_salary_missing():
    assert annual_inr(None, FX_RATES) is None


def test_annual_inr_none_when_amount_missing():
    salary = Salary(min=None, max=None, currency="INR", unit="year")
    assert annual_inr(salary, FX_RATES) is None


def test_annual_inr_none_when_currency_unknown():
    salary = Salary(min=100_000, currency="XYZ", unit="year")
    assert annual_inr(salary, FX_RATES) is None


def test_annual_inr_none_when_unit_unknown():
    salary = Salary(min=100_000, currency="INR", unit="fortnight")
    assert annual_inr(salary, FX_RATES) is None


def test_annual_inr_uses_lower_of_min_max_conservatively():
    salary = Salary(min=1_000_000, max=2_000_000, currency="INR", unit="year")
    assert annual_inr(salary, FX_RATES) == 1_000_000


# ── evaluate_constraints: location ──────────────────────────────────────

def test_remote_job_always_passes_location():
    job = make_job(remote=True, location="Anywhere")
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_onsite_outside_accepted_cities_hard_rejects():
    """The exact QA regression: onsite Bengaluru, profile only accepts Mumbai/
    Navi Mumbai -> must fail, unconditionally, regardless of any other score."""
    job = make_job(remote=False, location="Bengaluru, Karnataka, India")
    profile = make_profile()
    result = evaluate_constraints(job, profile, FX_RATES)
    assert not result.passed
    assert any("Bengaluru" in r for r in result.reasons)


def test_onsite_in_accepted_city_passes():
    job = make_job(remote=False, location="Mumbai, Maharashtra, India")
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_onsite_city_match_is_case_and_substring_insensitive():
    job = make_job(remote=False, location="navi mumbai, Maharashtra, India")
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_unknown_work_arrangement_passes_location():
    """remote=None (unknown) never hard-rejects on location — we don't
    penalize missing data."""
    job = make_job(remote=None, location="Some City, Some Country")
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_onsite_with_unknown_location_passes_to_gate_not_rejected():
    """P2.2: an onsite job whose location is missing/empty must NOT be hard-
    rejected (it might be in an accepted city) — it's passed to the AI gate.
    Only a KNOWN, non-accepted onsite location is a hard reject."""
    for missing in (None, "", "   "):
        job = make_job(remote=False, location=missing)
        profile = make_profile()
        assert evaluate_constraints(job, profile, FX_RATES).passed, missing


# ── evaluate_constraints: salary ─────────────────────────────────────────

def test_missing_salary_never_rejects():
    job = make_job(remote=True, salary=None)
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_confidently_below_floor_salary_rejects():
    job = make_job(
        remote=True,
        salary=Salary(min=500_000, max=500_000, currency="INR", unit="year"),
    )
    profile = make_profile()  # floor_lpa=15 -> 1,500,000
    result = evaluate_constraints(job, profile, FX_RATES)
    assert not result.passed
    assert any("below floor" in r for r in result.reasons)


def test_salary_at_or_above_floor_passes():
    job = make_job(
        remote=True,
        salary=Salary(min=1_800_000, max=1_800_000, currency="INR", unit="year"),
    )
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_salary_margin_gives_benefit_of_doubt_near_floor():
    """SALARY_REJECT_MARGIN=0.9 means a salary at 95% of floor should NOT
    reject (only confidently-below-floor amounts do)."""
    job = make_job(
        remote=True,
        salary=Salary(min=1_425_000, max=1_425_000, currency="INR", unit="year"),  # 95% of 1.5M floor
    )
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_unparseable_salary_unit_never_rejects():
    job = make_job(
        remote=True,
        salary=Salary(min=100, currency="INR", unit="fortnight"),
    )
    profile = make_profile()
    assert evaluate_constraints(job, profile, FX_RATES).passed


# ── both fail at once ────────────────────────────────────────────────────

def test_both_location_and_salary_can_fail_together():
    job = make_job(
        remote=False,
        location="Bengaluru, Karnataka, India",
        salary=Salary(min=500_000, max=500_000, currency="INR", unit="year"),
    )
    profile = make_profile()
    result = evaluate_constraints(job, profile, FX_RATES)
    assert not result.passed
    assert len(result.reasons) == 2
