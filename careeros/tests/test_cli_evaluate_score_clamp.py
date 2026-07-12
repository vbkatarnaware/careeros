"""Regression test for the 2026-07-12 scoring-contract bug: an eval with
recommendation == "skip" (a deal-breaker or a stated preference violation)
must never leave an apply-tier score sitting on disk or in the cache — a
"green" (score >= threshold) row must always mean recommendation == "apply".
Earlier, agents tried to force this by fudging the `logistics` rubric
dimension to 0.0, which corrupted the weighted score and the "why". The fix
is a deterministic clamp in `_evaluate_finalize`, independent of how the
eval was produced (script, one agent, another CLI's agent, etc.): any
skip-recommendation record scoring >= threshold gets its stored `score`
capped to `threshold - 0.1`, while the rubric dimensions stay untouched so
the honest fit reasoning remains legible."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.cli import _evaluate_finalize
from careeros.config import Config

_VALID_PROFILE = (
    "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
)


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={"eval": "v2"},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _eval_record(job_id: str, job_hash: str, score: float, recommendation: str) -> dict:
    return {
        "id": job_id, "score": score, "confidence": 0.9, "recommendation": recommendation,
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        # Honest, strong rubric dimensions on purpose — the clamp must fix
        # the score WITHOUT touching these, so the "why" stays legible.
        "rubric": {"role_fit": 4.8, "seniority_fit": 4.5, "skills_match": 4.9, "domain": 4.5, "logistics": 1.0},
        "prompt_version": "v2", "profile_version": 1, "job_hash": job_hash,
    }


def test_skip_recommendation_above_threshold_is_clamped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-07-12"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        json.dump([{"job": {"id": "onsite-utah-job"}, "job_hash": "hash-1"}], f)

    # This is exactly today's bug pattern: a genuinely strong fit (role/
    # skills/seniority/domain all high) but recommendation == "skip" because
    # of an onsite deal-breaker, with an honest (non-zeroed) logistics score
    # that alone can't pull the 4.4 average below the 4.0 threshold.
    record = _eval_record("onsite-utah-job", "hash-1", score=4.4, recommendation="skip")
    with open(stage_dir / "onsite-utah-job.json", "w") as f:
        json.dump(record, f)

    _evaluate_finalize(cfg, date)

    with open(stage_dir / "onsite-utah-job.json") as f:
        written = json.load(f)

    assert written["score"] < cfg.threshold, "a skip-recommendation eval must never score green"
    assert written["score"] == 3.9
    # Rubric dimensions must stay honest/untouched — only `score` is clamped.
    assert written["rubric"]["role_fit"] == 4.8
    assert written["rubric"]["logistics"] == 1.0
    assert written["recommendation"] == "skip"


def test_apply_recommendation_is_never_clamped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-07-12"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        json.dump([{"job": {"id": "remote-good-job"}, "job_hash": "hash-2"}], f)

    record = _eval_record("remote-good-job", "hash-2", score=4.6, recommendation="apply")
    with open(stage_dir / "remote-good-job.json", "w") as f:
        json.dump(record, f)

    _evaluate_finalize(cfg, date)

    with open(stage_dir / "remote-good-job.json") as f:
        written = json.load(f)

    assert written["score"] == 4.6, "an apply-recommendation eval must be left untouched"


def test_skip_recommendation_below_threshold_is_untouched(tmp_path, monkeypatch):
    """Sanity check the clamp doesn't fire on evals that already score below
    threshold — no needless rewrite of a file that was already correct."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-07-12"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        json.dump([{"job": {"id": "weak-fit-job"}, "job_hash": "hash-3"}], f)

    record = _eval_record("weak-fit-job", "hash-3", score=2.1, recommendation="skip")
    with open(stage_dir / "weak-fit-job.json", "w") as f:
        json.dump(record, f)

    _evaluate_finalize(cfg, date)

    with open(stage_dir / "weak-fit-job.json") as f:
        written = json.load(f)

    assert written["score"] == 2.1
