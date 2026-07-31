"""Tests for careeros/pipeline/ledger.py — per-tier aggregation, arming
checks, and the quarantine mechanism."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.config import Config
from careeros.models import dumps
from careeros.pipeline.ledger import (
    aggregate_entries, compute_arming, compute_run_tier_stats,
    load_quarantine, render_ledger_markdown,
)


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed_run(cfg, date, jobs_with_tiers, processed_records, outcomes):
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps(jobs_with_tiers))
    with open(cfg.careeros_dir / "processed.jsonl", "a") as f:
        for r in processed_records:
            f.write(json.dumps(r) + "\n")
    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "outcomes.json", "w") as f:
        f.write(dumps(outcomes))


def test_compute_run_tier_stats_attributes_records_to_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    jobs = [
        {"id": "a", "tiers": ["global_remote"]},
        {"id": "b", "tiers": ["india_remote"]},
        {"id": "c", "tiers": ["global_remote", "india_remote"]},  # cross-location union
    ]
    processed = [
        {"id": "a", "date": date, "terminal_stage": "constraints", "reason": "onsite outside onsite_ok", "score": None},
        {"id": "b", "date": date, "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "c", "date": date, "terminal_stage": "select", "reason": "apply", "score": 4.5},
    ]
    outcomes = [
        {"id": "c", "tier": "apply", "score": 4.5, "recommendation": "apply",
         "shortfall": {"role_fit": 0.1, "seniority_fit": 0.1, "skills_match": 0.1, "domain": 0.1, "logistics": 0.1},
         "primary_gap": None, "secondary_gap": None, "tiers": ["global_remote", "india_remote"]},
    ]
    _seed_run(cfg, date, jobs, processed, outcomes)

    entries = compute_run_tier_stats(cfg, date)
    by_tier = {e.tier: e for e in entries}

    assert by_tier["global_remote"].decision["records"] == 2  # a, c
    assert by_tier["india_remote"].decision["records"] == 2  # b, c
    assert by_tier["global_remote"].decision["constraints_rejected"] == 1
    assert by_tier["india_remote"].decision["gate_dropped"] == 1
    assert by_tier["global_remote"].descriptive["apply"] == 1
    assert by_tier["india_remote"].descriptive["apply"] == 1  # job c counted in BOTH tiers


def test_compute_run_tier_stats_gate_keep_rate_excludes_constraints_rejected(tmp_path, monkeypatch):
    """Gate-keep rate is of jobs that REACHED the gate -- a constraints-
    rejected job never reached gate at all and must not dilute the rate."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"
    jobs = [{"id": "a", "tiers": ["onsite"]}, {"id": "b", "tiers": ["onsite"]}, {"id": "c", "tiers": ["onsite"]}]
    processed = [
        {"id": "a", "date": date, "terminal_stage": "constraints", "reason": "salary floor", "score": None},
        {"id": "b", "date": date, "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "c", "date": date, "terminal_stage": "select", "reason": "omit", "score": 2.0},
    ]
    _seed_run(cfg, date, jobs, processed, [])
    entries = compute_run_tier_stats(cfg, date)
    onsite = entries[0]
    # gate pool = gate_dropped(1) + reached_select(1) = 2; keep rate = 1/2
    assert onsite.decision["gate_keep_rate"] == 0.5
    assert onsite.decision["constraints_rejection_rate"] == round(1 / 3, 4)


def test_aggregate_entries_pools_across_runs():
    from careeros.pipeline.ledger import TierLedgerEntry
    e1 = TierLedgerEntry(date="2026-07-01", tier="onsite",
                         decision={"records": 10, "constraints_rejected": 1, "gate_dropped": 2,
                                   "gate_keep_rate": 0.8, "constraints_rejection_rate": 0.1,
                                   "reject_reason_histogram": {"role-mismatch": 2}},
                         descriptive={"evaluated": 8, "apply": 1, "consider": 1, "omit": 6,
                                     "mean_score": 3.0, "mean_shortfall": None})
    e2 = TierLedgerEntry(date="2026-07-08", tier="onsite",
                         decision={"records": 12, "constraints_rejected": 0, "gate_dropped": 3,
                                   "gate_keep_rate": 0.75, "constraints_rejection_rate": 0.0,
                                   "reject_reason_histogram": {"role-mismatch": 3}},
                         descriptive={"evaluated": 9, "apply": 2, "consider": 0, "omit": 7,
                                     "mean_score": 3.2, "mean_shortfall": None})
    summary = aggregate_entries([e1, e2])
    assert summary["onsite"]["num_runs"] == 2
    assert summary["onsite"]["total_records"] == 22
    assert summary["onsite"]["apply_or_consider_events"] == 4  # (1+1) + (2+0)
    assert summary["onsite"]["top_reject_reasons"] == [("role-mismatch", 5)]


def test_compute_arming_requires_all_three_floors():
    summary = {
        "onsite": {"dates": ["2026-07-01", "2026-07-31"], "total_records": 500, "apply_or_consider_events": 10},
        "global_remote": {"dates": ["2026-07-25", "2026-07-31"], "total_records": 50, "apply_or_consider_events": 2},
    }
    arming = compute_arming(summary, min_days=28, min_records=400, min_events=8)
    assert arming["onsite"]["armed"] is True
    assert arming["global_remote"]["armed"] is False
    assert "records" in arming["global_remote"]["reason"] or "history" in arming["global_remote"]["reason"]


def test_render_ledger_markdown_handles_empty_summary():
    md = render_ledger_markdown({}, arming={})
    assert "No data yet" in md


def test_load_quarantine_reads_dates_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    learning_dir = cfg.careeros_dir / "learning"
    learning_dir.mkdir(parents=True)
    (learning_dir / "quarantine.json").write_text(json.dumps({"dates": ["2026-07-29", "2026-07-30"]}))
    assert load_quarantine(cfg) == {"2026-07-29", "2026-07-30"}


def test_load_quarantine_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    assert load_quarantine(cfg) == set()
