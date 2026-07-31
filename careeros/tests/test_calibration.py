"""Tests for careeros/calibration.py.

The regression test at the bottom (`test_reproduces_2026_07_forensics`) is
the single most important test in the v2.0 release: it replays the real
eval records from the four historical batches that triggered this whole
release and asserts the calibration harness draws exactly the line the
forensic investigation drew by hand. If this test ever needs its
tolerances loosened to pass, that is a five-alarm signal, not a nuisance.
"""

from __future__ import annotations

import json
from pathlib import Path

from careeros.calibration import (
    DEFAULT_ARITHMETIC_TOLERANCE,
    check_arithmetic,
    check_dimension_repetition,
    check_prose_contradiction,
    check_rubric_vector_clones,
    check_uniformity,
    run_calibration,
    weighted_score,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _rubric(**overrides):
    base = dict(role_fit=4.0, seniority_fit=4.0, skills_match=4.0, domain=4.0, logistics=4.0)
    base.update(overrides)
    return base


def _eval(id="j1", score=4.0, rubric=None, weaknesses=None, recommendation="apply"):
    return {
        "id": id, "score": score, "rubric": rubric or _rubric(),
        "weaknesses": weaknesses or ["some weakness", "another weakness"],
        "recommendation": recommendation,
    }


# ── weighted_score ───────────────────────────────────────────────────────

def test_weighted_score_matches_contract():
    rubric = dict(role_fit=4.5, seniority_fit=4.0, skills_match=4.0, domain=3.5, logistics=5.0)
    # 0.3*4.5 + 0.2*4.0 + 0.25*4.0 + 0.15*3.5 + 0.1*5.0 = 1.35+0.8+1.0+0.525+0.5 = 4.175 -> 4.2
    assert weighted_score(rubric) == 4.2


# ── check_arithmetic ─────────────────────────────────────────────────────

def test_arithmetic_clean_when_score_matches_rubric():
    records = [_eval(rubric=_rubric(), score=weighted_score(_rubric()))]
    finding = check_arithmetic(records)
    assert not finding.fired
    assert finding.stat["violations"] == 0


def test_arithmetic_fires_on_non_derived_score():
    rubric = _rubric(role_fit=4.8, seniority_fit=4.8, skills_match=4.5, domain=4.5, logistics=5.0)
    # unrounded weighted sum here is 4.715 -> honest round is 4.7; write 4.4 instead.
    records = [_eval(id="bad-1", score=4.4, rubric=rubric)]
    finding = check_arithmetic(records)
    assert finding.fired
    assert finding.severity == "block"
    assert "bad-1" in finding.detail[0]


def test_arithmetic_tolerates_the_float_rounding_ceiling():
    # Two independent roundings (rubric inputs, then the weighted sum) can
    # legitimately disagree with the raw unrounded sum by up to ~0.05.
    rubric = _rubric(role_fit=4.05, seniority_fit=3.95, skills_match=4.0, domain=4.0, logistics=4.0)
    unrounded = sum(w * rubric[d] for d, w in
                    dict(role_fit=.3, seniority_fit=.2, skills_match=.25, domain=.15, logistics=.1).items())
    records = [_eval(score=round(unrounded, 1), rubric=rubric)]
    finding = check_arithmetic(records, tol=DEFAULT_ARITHMETIC_TOLERANCE)
    assert not finding.fired


# ── check_prose_contradiction ────────────────────────────────────────────

def test_prose_contradiction_fires_on_named_seniority_mismatch():
    records = [_eval(
        id="principal-1",
        rubric=_rubric(seniority_fit=3.8),
        weaknesses=["Principal PM role is senior for candidate's tenure", "Domain is new"],
    )]
    finding = check_prose_contradiction(records)
    assert finding.fired
    assert "principal-1" in finding.detail[0]


def test_prose_contradiction_silent_when_seniority_fit_already_low():
    records = [_eval(
        rubric=_rubric(seniority_fit=2.0),
        weaknesses=["This role is a significant stretch for the candidate's tenure"],
    )]
    finding = check_prose_contradiction(records)
    assert not finding.fired


def test_prose_contradiction_ignores_domain_hedges():
    # "new to candidate" / "is new" about DOMAIN is a legitimate hedge, not a
    # contradiction — the check must not generalize past seniority.
    records = [_eval(
        rubric=_rubric(seniority_fit=4.0, domain=4.2),
        weaknesses=["Spend management domain is new to candidate"],
    )]
    finding = check_prose_contradiction(records)
    assert not finding.fired


# ── check_rubric_vector_clones ───────────────────────────────────────────

def test_clone_check_requires_both_record_count_and_company_span():
    same_rubric = _rubric(role_fit=4.8, seniority_fit=4.8, skills_match=4.5, domain=4.5, logistics=5.0)
    records = [_eval(id=f"j{i}", rubric=same_rubric) for i in range(4)]
    jobs_by_id = {
        "j0": {"company": "Empower"}, "j1": {"company": "Kira"},
        "j2": {"company": "Swym"}, "j3": {"company": "Tria"},
    }
    finding = check_rubric_vector_clones(records, jobs_by_id, min_records=4, min_companies=3)
    assert finding.fired
    assert finding.stat["groups_flagged"] == 1


def test_clone_check_silent_on_genuine_same_company_duplicate():
    same_rubric = _rubric()
    records = [_eval(id=f"j{i}", rubric=same_rubric) for i in range(4)]
    # All 4 are the same company (e.g. two postings dedupe missed) — no
    # cross-company collision, so this must NOT block.
    jobs_by_id = {f"j{i}": {"company": "Kotak"} for i in range(4)}
    finding = check_rubric_vector_clones(records, jobs_by_id, min_records=4, min_companies=3)
    assert not finding.fired


def test_clone_check_silent_below_min_records():
    same_rubric = _rubric(role_fit=4.9)
    records = [_eval(id=f"j{i}", rubric=same_rubric) for i in range(3)]
    jobs_by_id = {f"j{i}": {"company": f"Co{i}"} for i in range(3)}
    finding = check_rubric_vector_clones(records, jobs_by_id, min_records=4, min_companies=3)
    assert not finding.fired


# ── check_uniformity (WARN, never block) ─────────────────────────────────

def test_uniformity_warns_on_tight_high_passing_batch():
    records = [_eval(id=f"j{i}", score=4.4 + (i % 2) * 0.05) for i in range(14)]
    finding = check_uniformity(records, threshold=4.0, min_n=10)
    assert finding.severity == "warn"
    assert finding.fired


def test_uniformity_silent_on_honest_spread_even_at_high_pass_rate():
    # Mirrors 07-28: 89% pass rate, real dispersion. Must NOT fire.
    scores = [2.9, 3.6, 4.0, 4.1, 4.2, 4.2, 4.3, 4.4, 4.4, 4.5, 4.5, 4.6, 4.7, 4.7, 4.8, 4.8, 4.9, 4.9]
    records = [_eval(id=f"j{i}", score=s) for i, s in enumerate(scores)]
    finding = check_uniformity(records, threshold=4.0, min_n=10)
    assert not finding.fired


def test_uniformity_silent_below_min_batch_size():
    records = [_eval(id=f"j{i}", score=4.4) for i in range(5)]
    finding = check_uniformity(records, threshold=4.0, min_n=10)
    assert not finding.fired


# ── check_dimension_repetition (WARN, never block) ───────────────────────

def test_dimension_repetition_fires_on_repeated_non_exempt_dimension():
    records = [_eval(id=f"j{i}", rubric=_rubric(logistics=4.2)) for i in range(11)]
    finding = check_dimension_repetition(records, min_n=10)
    # logistics is exempt by default; role_fit/seniority_fit/skills_match/domain
    # all repeat too in this fixture (all built from the same base _rubric()).
    assert finding.fired
    assert "logistics" not in " ".join(finding.detail)


def test_dimension_repetition_exempts_logistics_by_design():
    records = [
        _eval(id=f"j{i}", rubric=_rubric(
            role_fit=3.0 + (i % 5) * 0.3,
            seniority_fit=2.0 + (i % 4) * 0.5,
            skills_match=3.0 + (i % 3) * 0.4,
            domain=2.0 + (i % 5) * 0.3,
            logistics=4.2,
        ))
        for i in range(11)
    ]
    finding = check_dimension_repetition(records, min_n=10)
    assert not finding.fired


# ── run_calibration end-to-end on synthetic data ─────────────────────────

def test_run_calibration_returns_all_five_checks_always():
    records = [_eval(id=f"j{i}", score=weighted_score(_rubric())) for i in range(3)]
    findings = run_calibration(records, jobs_by_id={}, threshold=4.0)
    assert {f.code for f in findings} == {
        "arithmetic", "prose_contradiction", "rubric_vector_clone",
        "uniformity", "dimension_repetition",
    }
    assert all(f.fired is False for f in findings)  # too small a batch, all silent


# ── the forensic regression test ─────────────────────────────────────────

def _load_historical(date: str) -> tuple[list[dict], dict[str, dict]]:
    data = json.loads((FIXTURES / "calibration_historical.json").read_text())[date]
    return data["records"], data["jobs_by_id"]


def test_reproduces_2026_07_forensics():
    """This is the release's acceptance test. It must reproduce, from the
    real eval records, exactly what the forensic investigation found by
    hand: 07-28 and 07-31 are honest; 07-29 and 07-30 are not."""
    expectations = {
        "2026-07-28": {"blocked": False, "arithmetic_violations": 0},
        "2026-07-29": {"blocked": True, "arithmetic_violations": 13},
        "2026-07-30": {"blocked": True, "arithmetic_violations": 13},
        "2026-07-31": {"blocked": False, "arithmetic_violations": 0},
    }
    for date, expected in expectations.items():
        records, jobs_by_id = _load_historical(date)
        findings = run_calibration(records, jobs_by_id, threshold=4.0)
        by_code = {f.code: f for f in findings}
        arithmetic = by_code["arithmetic"]

        assert arithmetic.stat["violations"] == expected["arithmetic_violations"], (
            f"{date}: expected {expected['arithmetic_violations']} arithmetic "
            f"violations, got {arithmetic.stat['violations']}"
        )
        any_block_fired = any(f.fired and f.severity == "block" for f in findings)
        assert any_block_fired == expected["blocked"], (
            f"{date}: expected blocked={expected['blocked']}, "
            f"findings={[f.code for f in findings if f.fired]}"
        )
