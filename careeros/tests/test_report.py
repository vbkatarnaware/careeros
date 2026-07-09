"""Tests for careeros/report.py's render_summary (P2.6 day-level executive
summary). render_daily_report is pre-existing, untouched code — out of scope
here (see README's Testing section)."""

from __future__ import annotations

from careeros.models import Eval, Rubric
from careeros.report import render_summary
from careeros.tests.conftest import make_job


def make_eval(**overrides) -> Eval:
    defaults = dict(
        id="job-1", score=4.2, confidence=0.8, recommendation="apply",
        strengths=["Strong domain fit", "b", "c"], weaknesses=["x", "y"], ats_keywords=[],
        company_summary="s", fit_paragraph="f",
        rubric=Rubric(role_fit=4, seniority_fit=4, skills_match=4, domain=4, logistics=4),
        prompt_version="v2", profile_version=1, job_hash="h",
    )
    defaults.update(overrides)
    return Eval(**defaults)


def test_summary_lists_selected_jobs_above_threshold():
    job = make_job(id="job-1", company="Bjak", title="Product Manager")
    ev = make_eval(id="job-1", score=4.4, recommendation="apply")
    md = render_summary("2026-07-08", {"totals": {}}, [ev], [], {"job-1": job}, threshold=4.0)
    assert "Bjak" in md
    assert "Product Manager" in md
    assert "4.4" in md
    assert "Apply — score ≥ 4.0 (1)" in md


def test_summary_excludes_below_threshold_jobs_from_apply_section():
    """A below-threshold job belongs in consider_evals, not apply_evals — the
    caller (`partition_evals`) already made that call; render_summary just
    renders whichever list it's given."""
    md = render_summary("2026-07-08", {"totals": {}}, [], [], {}, threshold=4.0)
    assert "Apply — score ≥ 4.0 (0)" in md
    assert "None today" in md


def test_summary_lists_near_miss_jobs_separately():
    job = make_job(id="job-1", company="Acme", title="PM")
    ev = make_eval(id="job-1", score=3.7, recommendation="apply")
    md = render_summary("2026-07-08", {"totals": {}}, [], [ev], {"job-1": job}, threshold=4.0)
    assert "Consider — near miss, 3.5" in md
    assert "3.7 Acme" in md


def test_summary_near_miss_includes_realistic_skip_recommendation():
    """Regression test (found live, 2026-07-08): eval_v2.md's own rule sets
    recommendation="skip" for EVERY sub-threshold score, near-miss or not —
    so a near-miss job's recommendation is realistically always "skip", never
    "apply". render_summary must render whatever partition_evals decided
    without re-filtering on recommendation, or Consider would always be empty."""
    job = make_job(id="job-1", company="YipitData", title="Technical PM")
    ev = make_eval(id="job-1", score=3.5, recommendation="skip")
    md = render_summary("2026-07-08", {"totals": {}}, [], [ev], {"job-1": job}, threshold=4.0)
    assert "Consider — near miss, 3.5–3.9 (1)" in md
    assert "3.5 YipitData" in md


def test_summary_consider_band_label_honors_configured_consider_threshold():
    """The Consider band LABEL tracks the configured consider_threshold, not a
    hardcoded value — so the displayed range stays consistent with whatever
    band the caller's partition_evals actually used."""
    job = make_job(id="job-1", company="Acme", title="PM")
    ev = make_eval(id="job-1", score=3.2, recommendation="skip")  # partition_evals already decided this is Consider
    md = render_summary("2026-07-08", {"totals": {}}, [], [ev], {"job-1": job},
                        threshold=4.0, consider_threshold=3.0)
    assert "Consider — near miss, 3.0–3.9 (1)" in md
    assert "3.2 Acme" in md


def test_summary_apply_section_only_shows_what_it_was_given():
    """render_summary trusts its caller completely — it does not re-check
    recommendation or constraints (that's partition_evals's job, upstream)."""
    md = render_summary("2026-07-08", {"totals": {}}, [], [], {}, threshold=4.0)
    assert "Apply — score ≥ 4.0 (0)" in md


def test_summary_reports_zero_selected_as_supply_not_failure():
    md = render_summary("2026-07-08", {"totals": {}}, [], [], {}, threshold=4.0)
    assert "supply-limited, not a run failure" in md


def test_summary_shows_cost_per_selected_job_when_available():
    manifest = {"totals": {"apify_cost_usd_total": 0.99, "cost_per_selected_job_usd": 0.2475, "selected": 4}}
    md = render_summary("2026-07-08", manifest, [], [], {}, threshold=4.0)
    assert "$0.9900" in md
    assert "$0.2475 per selected" in md


def test_summary_funnel_shows_only_recorded_stages():
    manifest = {"totals": {"discovered": 46, "eligible": 40}}
    md = render_summary("2026-07-08", manifest, [], [], {}, threshold=4.0)
    assert "Discovered: 46" in md
    assert "Eligible: 40" in md
    assert "Gated:" not in md


def test_summary_uses_top_strength_as_one_line_reason():
    job = make_job(id="job-1", company="Bjak", title="PM")
    ev = make_eval(id="job-1", score=4.4, strengths=["This is why it fits", "b", "c"])
    md = render_summary("2026-07-08", {"totals": {}}, [ev], [], {"job-1": job}, threshold=4.0)
    assert "This is why it fits" in md


def test_summary_apply_list_sorted_by_score_descending():
    jobs = {"a": make_job(id="a", company="A"), "b": make_job(id="b", company="B")}
    evals = [make_eval(id="a", score=4.1), make_eval(id="b", score=4.6)]
    md = render_summary("2026-07-08", {"totals": {}}, evals, [], jobs, threshold=4.0)
    assert md.index("**4.6** B") < md.index("**4.1** A")
