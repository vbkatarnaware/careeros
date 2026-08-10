"""Tests for careeros/cli/gate_evaluate.py's Gate-input selection: the
per-company fairness cap (with rotation) and the overall gate_max_jobs cost
ceiling. Both apply ONLY to what's sent to the AI Gate — eligible.json is
never touched, so nothing discovered is ever permanently lost; a capped job
is simply a candidate again on a future run.
"""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.cli._shared import _today
from careeros.cli.gate_evaluate import (
    _company_cap_select,
    _gate_prepare,
    _gate_rank_key,
    _load_rotation_state,
    _overall_cap_select,
)
from careeros.config import Config
from careeros.models import Profile


def _cfg(**overrides) -> Config:
    base = dict(threshold=4.0, consider_threshold=3.5, gate_batch_size=50,
                description_max_chars=4000, gate_description_max_chars=900)
    base.update(overrides)
    return Config(**base)


def _profile(**overrides) -> Profile:
    base = dict(
        version=1, candidate={}, headline="", targets=[], experience=[],
        role_priorities=["Product Manager", "Founder's Office"],
        work_mode_priority=["india_remote", "global_remote", "mumbai_onsite"],
    )
    base.update(overrides)
    return Profile(**base)


def _job(id_, company, posted_at="2026-08-09", **overrides):
    job = {"id": id_, "company": company, "title": "Product Manager", "posted_at": posted_at}
    job.update(overrides)
    return job


# ── _company_cap_select ───────────────────────────────────────────────

def test_company_cap_disabled_returns_everything():
    jobs = [_job("1", "Acme"), _job("2", "Acme")]
    kept, dropped = _company_cap_select(jobs, max_per_company=None, rotation={})
    assert kept == jobs
    assert dropped == []


def test_company_cap_keeps_at_most_n_per_company():
    jobs = [_job(str(i), "Kotak") for i in range(10)]
    kept, dropped = _company_cap_select(jobs, max_per_company=5, rotation={})
    assert len(kept) == 5
    assert len(dropped) == 5


def test_company_cap_does_not_affect_other_companies():
    jobs = [_job("k1", "Kotak"), _job("k2", "Kotak"), _job("r1", "Ramp")]
    kept, dropped = _company_cap_select(jobs, max_per_company=1, rotation={})
    kept_ids = {j["id"] for j in kept}
    assert "r1" in kept_ids
    assert len(kept) == 2  # one Kotak + Ramp


def test_company_cap_normalizes_company_name_variants():
    jobs = [_job("1", "Acme Inc."), _job("2", "  Acme   Inc  "), _job("3", "Acme Inc.")]
    kept, dropped = _company_cap_select(jobs, max_per_company=2, rotation={})
    assert len(kept) == 2
    assert len(dropped) == 1


def test_company_cap_prioritizes_never_shown_jobs_over_previously_shown():
    jobs = [_job("old1", "Kotak"), _job("old2", "Kotak"), _job("new1", "Kotak")]
    rotation = {"old1": "2026-08-08", "old2": "2026-08-08"}  # already shown yesterday
    kept, dropped = _company_cap_select(jobs, max_per_company=1, rotation=rotation)
    assert kept[0]["id"] == "new1"  # never-shown wins over already-shown


def test_company_cap_rotates_among_previously_shown_jobs():
    # Both shown before, but old1 longer ago -> old1 should win the rotation slot.
    jobs = [_job("old1", "Kotak"), _job("old2", "Kotak")]
    rotation = {"old1": "2026-08-01", "old2": "2026-08-08"}
    kept, dropped = _company_cap_select(jobs, max_per_company=1, rotation=rotation)
    assert kept[0]["id"] == "old1"


def test_company_cap_breaks_ties_by_recency():
    jobs = [_job("stale", "Kotak", posted_at="2026-08-01"), _job("fresh", "Kotak", posted_at="2026-08-09")]
    kept, _ = _company_cap_select(jobs, max_per_company=1, rotation={})
    assert kept[0]["id"] == "fresh"


# ── _gate_rank_key / _overall_cap_select ────────────────────────────────

def test_gate_rank_key_prefers_higher_priority_tier():
    profile = _profile()
    india = {"tiers": ["india_remote"]}
    global_ = {"tiers": ["global_remote"]}
    assert _gate_rank_key(india, profile) < _gate_rank_key(global_, profile)


def test_gate_rank_key_prefers_higher_priority_role():
    profile = _profile()
    pm = {"tiers": [], "title": "Product Manager"}
    fo = {"tiers": [], "title": "Founder's Office Associate"}
    assert _gate_rank_key(pm, profile) < _gate_rank_key(fo, profile)


def test_overall_cap_select_disabled_returns_everything():
    jobs = [_job(str(i), "Acme") for i in range(5)]
    kept, dropped = _overall_cap_select(jobs, gate_max_jobs=None, profile=_profile())
    assert kept == jobs
    assert dropped == []


def test_overall_cap_select_truncates_to_gate_max_jobs():
    jobs = [_job(str(i), "Acme") for i in range(10)]
    kept, dropped = _overall_cap_select(jobs, gate_max_jobs=3, profile=_profile())
    assert len(kept) == 3
    assert len(dropped) == 7


def test_overall_cap_select_keeps_higher_ranked_jobs():
    profile = _profile()
    india = _job("india", "Acme", tiers=["india_remote"])
    global_ = _job("global", "Acme", tiers=["global_remote"])
    kept, dropped = _overall_cap_select([global_, india], gate_max_jobs=1, profile=profile)
    assert kept[0]["id"] == "india"
    assert dropped[0]["id"] == "global"


# ── _gate_prepare: end to end, eligible.json untouched, rotation persists ──

def test_gate_prepare_company_cap_does_not_touch_eligible_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(max_jobs_per_company_per_run=2)
    date = _today()

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    jobs = [_job(str(i), "Kotak") for i in range(5)]
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump(jobs, f)

    _gate_prepare(cfg, date)

    with open(constraints_dir / "eligible.json") as f:
        assert len(json.load(f)) == 5  # all 5 still present, nothing deleted

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "_input_0.json") as f:
        assert len(json.load(f)) == 2  # only 2 sent to the Gate

    with open(gate_dir / "_selection_meta.json") as f:
        meta = json.load(f)
    assert meta["dropped_by_company_cap"] == 3


def test_gate_prepare_rotation_state_persists_and_advances_next_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(max_jobs_per_company_per_run=1)

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, "2026-08-09", "constraints")
    jobs = [_job("a", "Kotak", posted_at="2026-08-09"), _job("b", "Kotak", posted_at="2026-08-09")]
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump(jobs, f)
    _gate_prepare(cfg, "2026-08-09")

    gate_dir_1 = runmeta.stage_dir(cfg.runs_dir, "2026-08-09", "gate")
    with open(gate_dir_1 / "_input_0.json") as f:
        first_run_ids = {j["id"] for j in json.load(f)}
    assert len(first_run_ids) == 1

    rotation = _load_rotation_state(cfg)
    assert rotation.get(next(iter(first_run_ids))) == "2026-08-09"

    # Same two jobs still eligible the next day (never processed to
    # threshold, so never suppressed) -> the OTHER job should win this time.
    constraints_dir_2 = runmeta.stage_dir(cfg.runs_dir, "2026-08-10", "constraints")
    with open(constraints_dir_2 / "eligible.json", "w") as f:
        json.dump(jobs, f)
    _gate_prepare(cfg, "2026-08-10")

    gate_dir_2 = runmeta.stage_dir(cfg.runs_dir, "2026-08-10", "gate")
    with open(gate_dir_2 / "_input_0.json") as f:
        second_run_ids = {j["id"] for j in json.load(f)}
    assert second_run_ids != first_run_ids


def test_gate_prepare_no_selection_meta_when_nothing_capped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()  # both caps off
    date = _today()

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump([_job("1", "Acme")], f)

    _gate_prepare(cfg, date)

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    assert not (gate_dir / "_selection_meta.json").exists()


def test_gate_prepare_gate_max_jobs_missing_profile_fails_soft(tmp_path, monkeypatch, capsys):
    """gate_max_jobs needs profile.yaml (for tier/role ranking) but the
    company cap doesn't — a missing profile should degrade to 'no volume
    cap this run', not crash the whole gate stage."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(gate_max_jobs=1)
    date = _today()

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump([_job("1", "Acme"), _job("2", "Acme")], f)

    _gate_prepare(cfg, date)  # must not raise

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "_input_0.json") as f:
        assert len(json.load(f)) == 2  # cap skipped, both jobs sent
