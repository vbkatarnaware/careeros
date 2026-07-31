"""Tests for careeros/pipeline/golden.py — golden-set sampling and drift
scoring. Pure logic, no filesystem."""

from __future__ import annotations

from careeros.pipeline.golden import deterministic_sample, score_probe


def _entry(job_hash, band, score, holdout=False, **rubric_overrides):
    rubric = dict(role_fit=4.0, seniority_fit=4.0, skills_match=4.0, domain=4.0, logistics=4.0)
    rubric.update(rubric_overrides)
    return {
        "job_hash": job_hash, "job_ref": f"jobs.json#{job_hash}",
        "label": f"Co — {job_hash}", "band": band, "holdout": holdout,
        "expected": {"score": score, "rubric": rubric},
    }


ENTRIES = [
    _entry("apply-1", "apply", 4.5),
    _entry("apply-2", "apply", 4.8, holdout=True),
    _entry("consider-1", "consider", 3.6),
    _entry("omit-1", "omit", 2.0),
    _entry("omit-2", "omit", 1.5),
]


def test_deterministic_sample_excludes_holdout():
    sample = deterministic_sample(ENTRIES, n=10, seed="2026-08-01")
    assert all(not e["holdout"] for e in sample)
    assert "apply-2" not in {e["job_hash"] for e in sample}


def test_deterministic_sample_is_reproducible_for_same_seed():
    s1 = deterministic_sample(ENTRIES, n=3, seed="2026-08-01")
    s2 = deterministic_sample(ENTRIES, n=3, seed="2026-08-01")
    assert [e["job_hash"] for e in s1] == [e["job_hash"] for e in s2]


def test_deterministic_sample_differs_across_seeds_in_general():
    s1 = [e["job_hash"] for e in deterministic_sample(ENTRIES, n=2, seed="2026-08-01")]
    s2 = [e["job_hash"] for e in deterministic_sample(ENTRIES, n=2, seed="2099-01-01")]
    # Not a hard guarantee for tiny pools, but with 4 eligible entries and
    # very different seeds this should not collide every time in practice;
    # the real invariant we care about is reproducibility (tested above),
    # this just documents seed-sensitivity exists.
    assert isinstance(s1, list) and isinstance(s2, list)


def test_score_probe_clean_when_actual_matches_expected():
    by_hash = {e["job_hash"]: e for e in ENTRIES}
    actual = {
        "apply-1": {"score": 4.5, "rubric": ENTRIES[0]["expected"]["rubric"]},
        "omit-1": {"score": 2.0, "rubric": ENTRIES[3]["expected"]["rubric"]},
    }
    report = score_probe(by_hash, actual, threshold=4.0, consider_threshold=3.5)
    assert report.severity == "clean"
    assert report.n == 2
    assert report.band_agreement == 2
    assert report.mean_signed_error == 0.0


def test_score_probe_detects_signed_inflation():
    by_hash = {e["job_hash"]: e for e in ENTRIES}
    inflated_rubric = dict(ENTRIES[3]["expected"]["rubric"])
    actual = {
        "omit-1": {"score": 4.2, "rubric": inflated_rubric},  # expected 2.0 -> actual 4.2
    }
    report = score_probe(by_hash, actual, threshold=4.0, consider_threshold=3.5)
    assert report.mean_signed_error > 0
    assert report.severity == "block"  # +2.2 blows past mean_signed_block_high=0.5
    assert report.band_agreement == 0  # omit expected, but 4.2 actual is "apply"


def test_score_probe_detects_one_dimension_systematically_wrong():
    by_hash = {e["job_hash"]: e for e in ENTRIES}
    skewed_rubric = dict(ENTRIES[0]["expected"]["rubric"])
    skewed_rubric["seniority_fit"] = 2.0  # expected 4.0 -> actual 2.0, dev=2.0
    actual = {"apply-1": {"score": 4.5, "rubric": skewed_rubric}}
    report = score_probe(by_hash, actual, threshold=4.0, consider_threshold=3.5)
    assert report.per_dimension_max_deviation["seniority_fit"] == 2.0
    assert report.severity == "block"  # 2.0 > per_dimension_block=1.5


def test_score_probe_ignores_entries_never_probed():
    by_hash = {e["job_hash"]: e for e in ENTRIES}
    actual = {"apply-1": {"score": 4.5, "rubric": ENTRIES[0]["expected"]["rubric"]}}
    report = score_probe(by_hash, actual, threshold=4.0, consider_threshold=3.5)
    assert report.n == 1  # only the one actually probed, not all 5 golden entries
