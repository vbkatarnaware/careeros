"""Tests for careeros/pipeline/threshold.py's partition_evals — the
deterministic three-tier gate (Apply / Consider / omit) and the backstop
against the AI mislabeling a hard-rejected job as "apply"."""

from __future__ import annotations

from careeros.models import Eval, Rubric, Salary
from careeros.pipeline.threshold import partition_evals
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


# ── partition_evals: two-tier APPLY / CONSIDER / omit (P2.8) ────────────────

def _part(evals, jobs):
    return partition_evals(evals, 4.0, 3.5, jobs, make_profile(), FX_RATES)


def test_partition_apply_tier_is_score_ge_threshold_apply_and_constraints():
    job = make_job(id="j", remote=True)
    ev = make_eval(id="j", score=4.2, recommendation="apply")
    apply_, consider_, omit_ = _part([ev], {"j": job})
    assert apply_ == [ev] and consider_ == [] and omit_ == []


def test_partition_consider_tier_is_near_miss_band():
    """3.5 <= score < 4.0 with passing constraints -> CONSIDER (Sheet-only)."""
    job = make_job(id="j", remote=True)
    ev = make_eval(id="j", score=3.7, recommendation="apply")
    apply_, consider_, omit_ = _part([ev], {"j": job})
    assert apply_ == [] and consider_ == [ev] and omit_ == []


def test_partition_below_consider_threshold_is_omitted():
    job = make_job(id="j", remote=True)
    ev = make_eval(id="j", score=3.4, recommendation="apply")
    apply_, consider_, omit_ = _part([ev], {"j": job})
    assert apply_ == [] and consider_ == [] and omit_ == [ev]


def test_partition_high_score_but_skip_recommendation_drops_to_consider():
    """A >=4.0 job the AI flagged non-apply is NOT Apply, but its score keeps it
    visible as a CONSIDER row rather than being silently dropped."""
    job = make_job(id="j", remote=True)
    ev = make_eval(id="j", score=4.6, recommendation="skip")
    apply_, consider_, omit_ = _part([ev], {"j": job})
    assert apply_ == [] and consider_ == [ev]


def test_partition_hard_constraint_failure_is_omitted_regardless_of_score():
    """A location deal-breaker is a hard no — omitted entirely, never surfaced
    as Consider even at a high score."""
    job = make_job(id="j", remote=False, location="Bengaluru, Karnataka, India")
    ev = make_eval(id="j", score=4.5, recommendation="apply")  # profile: Mumbai/Navi only
    apply_, consider_, omit_ = _part([ev], {"j": job})
    assert apply_ == [] and consider_ == [] and omit_ == [ev]


def test_partition_all_three_tiers_together_sorted():
    jobs = {i: make_job(id=i, remote=True) for i in ("hi", "mid", "lo")}
    evs = [make_eval(id="hi", score=4.5, recommendation="apply"),
           make_eval(id="mid", score=3.6, recommendation="apply"),
           make_eval(id="lo", score=2.0, recommendation="skip")]
    apply_, consider_, omit_ = _part(evs, jobs)
    assert [e.id for e in apply_] == ["hi"]
    assert [e.id for e in consider_] == ["mid"]
    assert [e.id for e in omit_] == ["lo"]


def test_partition_salary_hard_reject_omits_high_scoring_apply():
    """A salary deal-breaker (distinct from the location one above) is also a
    hard omit regardless of score/recommendation."""
    job = make_job(
        id="job-1", remote=True,
        salary=Salary(min=500_000, max=500_000, currency="INR", unit="year"),
    )
    ev = make_eval(id="job-1", score=4.9, recommendation="apply")
    apply_, consider_, omit_ = _part([ev], {"job-1": job})
    assert apply_ == [] and consider_ == [] and omit_ == [ev]


def test_partition_consider_boundary_is_inclusive():
    """score == consider_threshold exactly still lands in CONSIDER, not omit."""
    job = make_job(id="job-1", remote=True)
    ev = make_eval(id="job-1", score=3.5, recommendation="apply")
    apply_, consider_, omit_ = _part([ev], {"job-1": job})
    assert apply_ == [] and consider_ == [ev] and omit_ == []


def test_partition_apply_results_sorted_by_score_descending():
    job_a = make_job(id="a", remote=True)
    job_b = make_job(id="b", remote=True)
    ev_a = make_eval(id="a", score=4.2)
    ev_b = make_eval(id="b", score=4.8)
    apply_, _, _ = _part([ev_a, ev_b], {"a": job_a, "b": job_b})
    assert [e.id for e in apply_] == ["b", "a"]
