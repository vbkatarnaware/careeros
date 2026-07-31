"""Tests for the v2.0 calibration wiring inside `_evaluate_finalize`
(careeros/cli/gate_evaluate.py). Complements careeros/tests/test_calibration.py
(the pure-function unit tests) with the CLI-level behavior: block-and-exit,
_calibration.json is written, a blocked eval is never cached, and the
same-date re-run stale-leftover contamination bug is fixed."""

from __future__ import annotations

import json

import pytest
import typer

from careeros import runmeta
from careeros.cache import Cache, eval_cache_key
from careeros.cli import _evaluate_finalize
from careeros.config import Config
from careeros.models import dumps

_VALID_PROFILE = (
    "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
)


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={"eval": "v2"},
        sheets={}, api={}, fx_rates={}, drive={"enabled": False},
        calibration={"enabled": True},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _honest_eval(job_id: str, job_hash: str, rubric: dict, recommendation="apply") -> dict:
    from careeros.calibration import weighted_score
    return {
        "id": job_id, "score": weighted_score(rubric), "confidence": 0.8,
        "recommendation": recommendation,
        "strengths": ["a", "b", "c"], "weaknesses": ["a real, specific weakness", "a second one"],
        "ats_keywords": [], "company_summary": "s", "fit_paragraph": "f",
        "rubric": rubric, "prompt_version": "v2", "profile_version": 1, "job_hash": job_hash,
    }


def _seed(cfg, date, job_id, company="Acme"):
    """Minimal gate/constraints scaffolding so jobs_by_id resolves company
    names for the clone check, and gate's kept-set drives relevance."""
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([{"id": job_id, "keep": True, "reason": "role-match", "confidence": 0.8}]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    eligible_path = constraints_dir / "eligible.json"
    existing = json.loads(eligible_path.read_text()) if eligible_path.exists() else []
    existing.append({"id": job_id, "company": company, "title": "Product Manager",
                     "apply_url": "https://x", "source": "fantastic-jobs"})
    with open(eligible_path, "w") as f:
        f.write(dumps(existing))


def test_arithmetic_violation_blocks_and_does_not_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-01"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        f.write(dumps([{"job": {"id": "bad-1"}, "job_hash": "hash-1"}]))
    _seed(cfg, date, "bad-1")

    rubric = {"role_fit": 4.8, "seniority_fit": 4.8, "skills_match": 4.5, "domain": 4.5, "logistics": 5.0}
    record = _honest_eval("bad-1", "hash-1", rubric)
    record["score"] = 4.4  # non-derived: rubric implies 4.7
    with open(stage_dir / "bad-1.json", "w") as f:
        f.write(dumps(record))

    with pytest.raises(typer.Exit):
        _evaluate_finalize(cfg, date)

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key("hash-1", 1, "v2")
    assert cache.get("evaluate", key) is None, "a blocked eval must never be cached"
    assert not (stage_dir / "_calibration.json").exists(), (
        "calibration.json is written only after a passing batch reaches the second pass"
    )


def test_clean_batch_writes_calibration_json_and_caches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-01"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        f.write(dumps([{"job": {"id": "good-1"}, "job_hash": "hash-2"}]))
    _seed(cfg, date, "good-1")

    rubric = {"role_fit": 3.5, "seniority_fit": 3.0, "skills_match": 2.5, "domain": 1.5, "logistics": 2.0}
    record = _honest_eval("good-1", "hash-2", rubric, recommendation="skip")
    with open(stage_dir / "good-1.json", "w") as f:
        f.write(dumps(record))

    _evaluate_finalize(cfg, date)

    assert (stage_dir / "_calibration.json").exists()
    findings = json.loads((stage_dir / "_calibration.json").read_text())
    assert {f["code"] for f in findings} == {
        "arithmetic", "prose_contradiction", "rubric_vector_clone", "uniformity", "dimension_repetition",
    }
    assert all(not f["fired"] for f in findings if f["severity"] == "block")

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key("hash-2", 1, "v2")
    assert cache.get("evaluate", key) is not None


def test_stale_leftover_from_earlier_same_date_attempt_is_ignored(tmp_path, monkeypatch):
    """Regression for the 2026-07-31 same-date re-run bug: a second
    `discover`+`normalize` for the same date left 06_evaluate/ eval files
    whose ids no longer existed in that run's job set. Those must be
    ignored entirely — not counted, not re-cached, not judged."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-01"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        f.write(dumps([{"job": {"id": "current-1"}, "job_hash": "hash-3"}]))
    _seed(cfg, date, "current-1")

    rubric = {"role_fit": 3.0, "seniority_fit": 3.0, "skills_match": 3.0, "domain": 3.0, "logistics": 3.0}
    current = _honest_eval("current-1", "hash-3", rubric, recommendation="skip")
    with open(stage_dir / "current-1.json", "w") as f:
        f.write(dumps(current))

    # A stale leftover: not in today's gate.keep set, not in _input.json.
    stale = _honest_eval("stale-from-earlier-attempt", "hash-stale", rubric)
    stale["score"] = 0.1  # would trip arithmetic hard if it were ever judged
    with open(stage_dir / "stale-from-earlier-attempt.json", "w") as f:
        f.write(dumps(stale))

    _evaluate_finalize(cfg, date)

    findings = json.loads((stage_dir / "_calibration.json").read_text())
    arithmetic = next(f for f in findings if f["code"] == "arithmetic")
    assert arithmetic["stat"]["n"] == 1, "the stale record must not be counted"
    assert not arithmetic["fired"]

    manifest = runmeta.load_manifest(cfg.runs_dir, date)
    assert manifest["stages"]["evaluate"]["count_out"] == 1, (
        "run.json must reflect only this run's relevant records, not stale leftovers"
    )


def test_legitimate_cache_hit_is_counted_but_not_calibration_judged(tmp_path, monkeypatch):
    """A cache-hit file (today's gate kept it, but it wasn't in _input.json
    because the cache already had it) must be included in count_out/caching,
    but must NOT be judged by calibration — re-judging already-accepted
    prior work would make a batch's verdict depend on what else was cached
    that day."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-01"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input.json", "w") as f:
        f.write(dumps([{"job": {"id": "fresh-1"}, "job_hash": "hash-4"}]))
    _seed(cfg, date, "fresh-1", company="FreshCo")
    _seed(cfg, date, "cache-hit-1", company="CachedCo")

    rubric = {"role_fit": 3.0, "seniority_fit": 3.0, "skills_match": 3.0, "domain": 3.0, "logistics": 3.0}
    fresh = _honest_eval("fresh-1", "hash-4", rubric, recommendation="skip")
    with open(stage_dir / "fresh-1.json", "w") as f:
        f.write(dumps(fresh))

    # Written directly during --prepare's cache-hit path; NOT in expected_ids.
    cache_hit = _honest_eval("cache-hit-1", "hash-old", rubric)
    cache_hit["score"] = 0.1  # non-derived on purpose -- must not block
    with open(stage_dir / "cache-hit-1.json", "w") as f:
        f.write(dumps(cache_hit))

    _evaluate_finalize(cfg, date)

    manifest = runmeta.load_manifest(cfg.runs_dir, date)
    assert manifest["stages"]["evaluate"]["count_out"] == 2

    findings = json.loads((stage_dir / "_calibration.json").read_text())
    arithmetic = next(f for f in findings if f["code"] == "arithmetic")
    assert arithmetic["stat"]["n"] == 1, "only the fresh record is calibration-judged"
    assert not arithmetic["fired"]
