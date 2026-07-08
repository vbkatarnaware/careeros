"""Tests for careeros/pipeline/threshold.py's select_final — the deterministic
backstop against the AI mislabeling a hard-rejected job as "apply"."""

from __future__ import annotations

from careeros.models import Eval, Rubric, Salary
from careeros.pipeline.threshold import select_above_threshold, select_final
from careeros.tests.conftest import FX_RATES, make_job, make_profile


def make_eval(**overrides) -> Eval:
    defaults = dict(
        id="job-1", score=4.5, confidence=0.8, recommendation="apply",
        strengths=["a", "b", "c"], weaknesses=["x", "y"], ats_keywords=[],
        company_summary="s", fit_paragraph="f",
        rubric=Rubric(role_fit=4.5, seniority_fit=4.5, skills_match=4.5, domain=4.5, logistics=1.5),
        prompt_version="v2", profile_version=1, job_hash="h",
    )
    defaults.update(overrides)
    return Eval(**defaults)


def test_high_score_apply_and_passing_constraints_is_selected():
    job = make_job(id="job-1", remote=True)
    ev = make_eval(id="job-1", score=4.5, recommendation="apply")
    profile = make_profile()
    selected, held_back = select_final([ev], 4.0, {"job-1": job}, profile, FX_RATES)
    assert selected == [ev]
    assert held_back == []


def test_regression_high_score_apply_but_hard_reject_location_is_held_back():
    """The actual QA bug: onsite-Bengaluru scored 4.1/"apply" because a
    10%-weighted logistics penalty was outweighed by strong role/skills
    scores. select_final must remove it regardless of score or recommendation."""
    job = make_job(id="job-1", remote=False, location="Bengaluru, Karnataka, India")
    ev = make_eval(id="job-1", score=4.1, recommendation="apply")
    profile = make_profile()  # only accepts Mumbai/Navi Mumbai onsite
    selected, held_back = select_final([ev], 4.0, {"job-1": job}, profile, FX_RATES)
    assert selected == []
    assert held_back == [ev]


def test_below_threshold_is_held_back_even_if_apply_and_constraints_pass():
    job = make_job(id="job-1", remote=True)
    ev = make_eval(id="job-1", score=3.5, recommendation="apply")
    profile = make_profile()
    selected, held_back = select_final([ev], 4.0, {"job-1": job}, profile, FX_RATES)
    assert selected == []
    assert held_back == [ev]


def test_recommendation_skip_is_held_back_even_above_threshold():
    """A high score with recommendation=skip (the eval prompt's own deal-
    breaker override) must not be selected."""
    job = make_job(id="job-1", remote=True)
    ev = make_eval(id="job-1", score=4.8, recommendation="skip")
    profile = make_profile()
    selected, held_back = select_final([ev], 4.0, {"job-1": job}, profile, FX_RATES)
    assert selected == []
    assert held_back == [ev]


def test_salary_hard_reject_also_removes_high_scoring_apply():
    job = make_job(
        id="job-1", remote=True,
        salary=Salary(min=500_000, max=500_000, currency="INR", unit="year"),
    )
    ev = make_eval(id="job-1", score=4.9, recommendation="apply")
    profile = make_profile()
    selected, held_back = select_final([ev], 4.0, {"job-1": job}, profile, FX_RATES)
    assert selected == []
    assert held_back == [ev]


def test_results_sorted_by_score_descending():
    job_a = make_job(id="a", remote=True)
    job_b = make_job(id="b", remote=True)
    ev_a = make_eval(id="a", score=4.2)
    ev_b = make_eval(id="b", score=4.8)
    profile = make_profile()
    selected, _ = select_final([ev_a, ev_b], 4.0, {"a": job_a, "b": job_b}, profile, FX_RATES)
    assert [e.id for e in selected] == ["b", "a"]


def test_select_above_threshold_is_score_only_no_constraint_check():
    """The plain dev-command variant intentionally does NOT re-check
    constraints — that's exactly why select_final exists for the real pipeline."""
    ev = make_eval(id="job-1", score=4.5)
    selected, held_back = select_above_threshold([ev], 4.0)
    assert selected == [ev]
    assert held_back == []
