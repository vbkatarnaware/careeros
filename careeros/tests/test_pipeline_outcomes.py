"""Tests for careeros/pipeline/outcomes.py — the weighted-contribution-
shortfall `primary_gap` derivation and `build_outcomes`."""

from __future__ import annotations

from careeros.models import Eval, Rubric
from careeros.pipeline.outcomes import build_outcomes, primary_gap, shortfall_vector


def _rubric(**overrides):
    base = dict(role_fit=4.0, seniority_fit=4.0, skills_match=4.0, domain=4.0, logistics=4.0)
    base.update(overrides)
    return base


def make_eval(**overrides) -> Eval:
    defaults = dict(
        id="job-1", score=4.5, confidence=0.8, recommendation="apply",
        strengths=["a", "b", "c"], weaknesses=["x", "y"], ats_keywords=[],
        company_summary="s", fit_paragraph="f",
        rubric=Rubric(**_rubric()),
        prompt_version="v2", profile_version=1, job_hash="h",
    )
    defaults.update(overrides)
    return Eval(**defaults)


def test_shortfall_vector_weights_by_rubric_weight():
    r = _rubric(role_fit=3.0, logistics=1.0)  # role_fit gap=2.0*0.30=0.6; logistics gap=4.0*0.10=0.4
    sv = shortfall_vector(r)
    assert sv["role_fit"] == 0.6
    assert sv["logistics"] == 0.4


def test_primary_gap_prefers_weighted_role_fit_over_raw_bigger_logistics_gap():
    """The exact failure mode raw argmin gets wrong: logistics sits lower in
    RAW terms (2.5 vs role_fit's 3.5) but role_fit's 30% weight makes IT the
    actionable primary gap, not the 10%-weighted logistics."""
    r = _rubric(role_fit=3.5, seniority_fit=4.0, skills_match=4.0, domain=4.0, logistics=2.5)
    gap, secondary, sv = primary_gap(r, "skip" if False else "apply")
    # role_fit shortfall = 0.30*(5-3.5) = 0.45; logistics shortfall = 0.10*(5-2.5) = 0.25
    assert gap == "role_fit"
    assert sv["role_fit"] > sv["logistics"]


def test_primary_gap_is_deal_breaker_for_skip_recommendation_regardless_of_rubric():
    """A skip recommendation (JD deal-breaker) must never blame a rubric
    dimension -- the rubric can look strong on a job skipped for a visa bar,
    and a tuner chasing 'domain' there would be chasing the wrong thing."""
    r = _rubric(role_fit=4.8, seniority_fit=4.8, skills_match=4.5, domain=4.5, logistics=0.5)
    gap, secondary, sv = primary_gap(r, "skip")
    assert gap == "deal_breaker"
    assert secondary is None


def test_primary_gap_none_when_every_dimension_near_ceiling():
    r = _rubric(role_fit=4.9, seniority_fit=4.8, skills_match=4.9, domain=4.7, logistics=5.0)
    gap, secondary, sv = primary_gap(r, "apply")
    assert gap is None and secondary is None


def test_primary_gap_ties_break_by_fixed_gap_order_not_dict_order():
    # role_fit and skills_match both produce shortfall 0.30*(5-3.0)=0.6 and
    # 0.25*(5-2.6)=0.6 respectively -- construct an exact tie.
    r = _rubric(role_fit=3.0, skills_match=2.6, seniority_fit=5.0, domain=5.0, logistics=5.0)
    gap, secondary, sv = primary_gap(r, "apply")
    assert abs(sv["role_fit"] - sv["skills_match"]) < 1e-9
    assert gap == "role_fit"  # GAP_ORDER = (role_fit, skills_match, ...) -> role_fit wins ties


def test_build_outcomes_covers_all_three_tiers_including_omit():
    apply_ev = make_eval(id="a", score=4.5, recommendation="apply",
                         rubric=Rubric(**_rubric(role_fit=4.8, seniority_fit=4.8, skills_match=4.5, domain=4.5, logistics=4.8)))
    consider_ev = make_eval(id="c", score=3.6, recommendation="apply",
                            rubric=Rubric(**_rubric()))
    omit_ev = make_eval(id="o", score=2.0, recommendation="skip",
                        rubric=Rubric(**_rubric(role_fit=1.5)))
    outcomes = build_outcomes([apply_ev], [consider_ev], [omit_ev],
                             tiers_by_id={"a": ["global_remote"], "c": None, "o": ["onsite"]})
    by_id = {o.id: o for o in outcomes}
    assert by_id["a"].tier == "apply" and by_id["a"].tiers == ["global_remote"]
    assert by_id["c"].tier == "consider"
    assert by_id["o"].tier == "omit" and by_id["o"].primary_gap == "deal_breaker"


def test_job_outcome_to_dict_roundtrips_json_serializable():
    import json
    ev = make_eval(id="x", score=4.0, recommendation="apply", rubric=Rubric(**_rubric()))
    outcomes = build_outcomes([ev], [], [], tiers_by_id={})
    json.dumps(outcomes[0].to_dict())  # must not raise
