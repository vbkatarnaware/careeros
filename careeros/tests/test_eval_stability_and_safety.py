"""Regression tests for the 2026-08-13 decision-stability guard and token
safety halt, added after a real investigation found that Claude Sonnet 5
has no available determinism control (temperature/top_p/top_k are removed
from the API entirely) and that identical Evaluate content re-run
independently can genuinely cross the Apply/Skip boundary — measured live,
the same job scored 4.1 in production and 3.6/3.0 in two independent
re-runs of byte-identical content. Neither guard changes the rubric,
threshold, or model; both are pure safety backstops around the existing
cache and batch-dispatch machinery."""

from __future__ import annotations

import json

import pytest
import typer

from careeros import runmeta
from careeros.cache import Cache, eval_cache_key
from careeros.cli import (
    _evaluate_finalize,
    _evaluate_prepare,
    _evaluate_record_usage,
    _usage_zone,
)
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
        calibration={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _eval_record(job_id: str, job_hash: str, score: float, recommendation: str) -> dict:
    return {
        "id": job_id, "score": score, "confidence": 0.9, "recommendation": recommendation,
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4.0, "seniority_fit": 4.0, "skills_match": 4.0, "domain": 4.0, "logistics": 4.0},
        "prompt_version": "v2", "profile_version": 1, "job_hash": job_hash,
    }


def _seed_finalize_inputs(cfg: Config, date: str, job_id: str, job_hash: str, record: dict) -> None:
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input_0.json", "w") as f:
        json.dump([{"job": {"id": job_id}, "job_hash": job_hash}], f)
    with open(stage_dir / f"{job_id}.json", "w") as f:
        json.dump(record, f)


# ── decision-stability guard ─────────────────────────────────────────────

def test_apply_to_skip_conflict_preserves_cache_and_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-flip-1"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    old_record = _eval_record("job-1", job_hash, score=4.1, recommendation="apply")
    cache.put("evaluate", key, old_record)

    new_record = _eval_record("job-1", job_hash, score=3.6, recommendation="skip")
    _seed_finalize_inputs(cfg, date, "job-1", job_hash, new_record)

    _evaluate_finalize(cfg, date)

    # Cache still holds the OLD (apply) decision — never overwritten.
    still_cached = cache.get("evaluate", key)
    assert still_cached["recommendation"] == "apply"
    assert still_cached["score"] == 4.1

    # Today's own 06_evaluate/job-1.json must ALSO stay on the old,
    # authoritative decision — the unreviewed new "skip" must never reach
    # threshold/artifacts/apply/sheets, which all read this file directly.
    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-1.json"))
    assert on_disk["recommendation"] == "apply", "today's job file must preserve Apply, not the unreviewed Skip"
    assert on_disk["score"] == 4.1
    assert on_disk["id"] == "job-1"  # id stays today's, even though the score/recommendation are the old ones

    conflicts_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"
    assert conflicts_path.exists()
    conflicts = json.load(open(conflicts_path))
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["job_id"] == "job-1"
    assert c["old_recommendation"] == "apply" and c["old_score"] == 4.1
    assert c["new_recommendation"] == "skip" and c["new_score"] == 3.6
    assert "detected_at" in c


def test_skip_to_apply_conflict_preserves_cache_and_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-flip-2"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    old_record = _eval_record("job-2", job_hash, score=3.5, recommendation="skip")
    cache.put("evaluate", key, old_record)

    new_record = _eval_record("job-2", job_hash, score=4.2, recommendation="apply")
    _seed_finalize_inputs(cfg, date, "job-2", job_hash, new_record)

    _evaluate_finalize(cfg, date)

    still_cached = cache.get("evaluate", key)
    assert still_cached["recommendation"] == "skip", "the existing authoritative cache record must survive an Apply flip too"
    assert still_cached["score"] == 3.5

    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-2.json"))
    assert on_disk["recommendation"] == "skip", "today's job file must preserve Skip, not the unreviewed Apply"
    assert on_disk["score"] == 3.5
    assert on_disk["id"] == "job-2"

    conflicts = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"))
    assert len(conflicts) == 1
    assert conflicts[0]["old_recommendation"] == "skip"
    assert conflicts[0]["new_recommendation"] == "apply"


def test_conflict_keeps_cache_and_current_run_file_consistent(tmp_path, monkeypatch):
    """After a conflict, the cache and today's 06_evaluate/<id>.json must
    agree on every field that matters downstream — not just recommendation."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-consistency"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, _eval_record("job-c", job_hash, score=4.3, recommendation="apply"))

    _seed_finalize_inputs(cfg, date, "job-c", job_hash, _eval_record("job-c", job_hash, score=3.1, recommendation="skip"))

    _evaluate_finalize(cfg, date)

    cached = cache.get("evaluate", key)
    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-c.json"))
    for field in ("recommendation", "score", "rubric", "strengths", "weaknesses"):
        assert cached[field] == on_disk[field], f"cache and current-run file disagree on {field!r}"


def test_repeated_finalize_does_not_duplicate_conflict_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-repeat"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, _eval_record("job-r", job_hash, score=4.1, recommendation="apply"))

    _seed_finalize_inputs(cfg, date, "job-r", job_hash, _eval_record("job-r", job_hash, score=3.6, recommendation="skip"))

    _evaluate_finalize(cfg, date)
    conflicts_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"
    assert len(json.load(open(conflicts_path))) == 1

    # Re-running --finalize (its own documented idempotent-retry contract)
    # with the SAME unresolved conflict still present must not re-append it.
    _evaluate_finalize(cfg, date)
    _evaluate_finalize(cfg, date)
    conflicts = json.load(open(conflicts_path))
    assert len(conflicts) == 1, f"expected exactly one conflict entry after 3 --finalize calls, got {len(conflicts)}"

    # The authoritative value must still be correctly preserved after every repeat.
    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-r.json"))
    assert on_disk["recommendation"] == "apply"
    still_cached = cache.get("evaluate", key)
    assert still_cached["recommendation"] == "apply"


def test_same_recommendation_different_score_is_not_a_conflict(tmp_path, monkeypatch):
    """Score drift within the same recommendation is real (measured) noise,
    but it's not the thing this guard exists to catch — only a crossed
    Apply/Skip boundary is a conflict. The cache should update normally."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-drift"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, _eval_record("job-3", job_hash, score=2.0, recommendation="skip"))

    new_record = _eval_record("job-3", job_hash, score=2.6, recommendation="skip")
    _seed_finalize_inputs(cfg, date, "job-3", job_hash, new_record)

    _evaluate_finalize(cfg, date)

    updated = cache.get("evaluate", key)
    assert updated["score"] == 2.6, "same-recommendation drift should update the cache normally, not be blocked"

    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-3.json"))
    assert on_disk["score"] == 2.6, "today's job file must keep the fresh score, not be reverted like a real conflict"

    conflicts_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"
    assert not conflicts_path.exists(), "no conflict artifact should be written for a non-boundary-crossing drift"


def test_non_conflicting_evaluation_is_unchanged(tmp_path, monkeypatch):
    """A resolved/non-conflicting case: the fresh evaluation matches what's
    already cached (same recommendation). Nothing about the guard should
    touch it — it's neither reverted nor specially rewritten."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-resolved"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, _eval_record("job-ok", job_hash, score=4.4, recommendation="apply"))

    fresh = _eval_record("job-ok", job_hash, score=4.35, recommendation="apply")
    _seed_finalize_inputs(cfg, date, "job-ok", job_hash, fresh)

    _evaluate_finalize(cfg, date)

    on_disk = json.load(open(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "job-ok.json"))
    assert on_disk["score"] == 4.35, "matching recommendation: the fresh evaluation is used as-is, untouched"
    cached = cache.get("evaluate", key)
    assert cached["score"] == 4.35, "the cache should update to the fresh (matching-recommendation) evaluation"

    conflicts_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"
    assert not conflicts_path.exists()


def test_no_existing_cache_entry_is_not_a_conflict(tmp_path, monkeypatch):
    """The common, everyday case: a genuinely first-seen job has nothing to
    compare against, so it's never a conflict and just caches normally."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-first-seen"

    new_record = _eval_record("job-4", job_hash, score=4.0, recommendation="apply")
    _seed_finalize_inputs(cfg, date, "job-4", job_hash, new_record)

    _evaluate_finalize(cfg, date)

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cached = cache.get("evaluate", key)
    assert cached is not None and cached["recommendation"] == "apply"

    conflicts_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_stability_conflicts.json"
    assert not conflicts_path.exists()


def test_finalize_surfaces_conflict_count_in_run_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"
    job_hash = "hash-visible"

    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, _eval_record("job-5", job_hash, score=4.1, recommendation="apply"))

    _seed_finalize_inputs(cfg, date, "job-5", job_hash, _eval_record("job-5", job_hash, score=3.6, recommendation="skip"))

    _evaluate_finalize(cfg, date)

    manifest = runmeta.load_manifest(cfg.runs_dir, date)
    errors = manifest["stages"]["evaluate"]["errors"]
    assert any("stability" in e and "1 job" in e for e in errors), errors


# ── token safety halt ─────────────────────────────────────────────────

def test_usage_zone_thresholds():
    # Small batch: only the absolute red ceiling applies.
    assert _usage_zone(10000, batch_jobs=13) == "green"
    assert _usage_zone(15000, batch_jobs=13) == "red"
    # Large batch (>=25 jobs): yellow/red zones kick in.
    assert _usage_zone(5000, batch_jobs=50) == "green"
    assert _usage_zone(7000, batch_jobs=50) == "yellow"
    assert _usage_zone(9500, batch_jobs=50) == "red"
    assert _usage_zone(15000, batch_jobs=50) == "red"


def test_red_zone_usage_halts_further_batch_preparation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"

    with pytest.raises(typer.Exit):
        _evaluate_record_usage(cfg, date, tokens=750_000, jobs=50)  # 15,000/job -> red

    halt_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_HALT_TOKEN_SAFETY.json"
    assert halt_path.exists()

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([{"id": "job-x", "keep": True, "reason": "role-match", "confidence": 0.8}]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        f.write(dumps([{"id": "job-x", "title": "PM", "company": "X", "apply_url": "https://x", "description": "d"}]))

    with pytest.raises(typer.Exit):
        _evaluate_prepare(cfg, date)

    # No new batch should have been written — prepare refused before doing anything.
    assert not list(runmeta.stage_dir(cfg.runs_dir, date, "evaluate").glob("_input_*.json"))


def test_green_zone_usage_does_not_halt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"

    _evaluate_record_usage(cfg, date, tokens=250_000, jobs=50)  # 5,000/job -> green

    halt_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_HALT_TOKEN_SAFETY.json"
    assert not halt_path.exists()

    log_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_usage_log.json"
    log = json.load(open(log_path))
    assert len(log) == 1
    assert log[0]["zone"] == "green"

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        f.write(dumps([]))

    # Should proceed normally (no jobs to evaluate, but no halt raised either).
    _evaluate_prepare(cfg, date)


def test_nesting_flag_also_halts_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()
    date = "2026-08-13"

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_NESTING_DETECTED.json", "w") as f:
        json.dump({"detail": "batch 2 reported spawning a sub-agent"}, f)

    with pytest.raises(typer.Exit):
        _evaluate_prepare(cfg, date)
