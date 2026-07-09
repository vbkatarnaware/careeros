"""Tests for careeros/cli.py's `_build_discovery_stats` (P2.9) — the
read-only join that feeds the Discovery KPI summary block. Reads only
01_discover/raw.json + .careeros/discovery_budget.json; fetches nothing.
Each test chdirs into a fresh tmp_path since Config's paths are cwd-relative
(same pattern as test_doctor.py)."""

from __future__ import annotations

import json

from careeros import budget, runmeta
from careeros.cli import _build_discovery_stats
from careeros.config import Config
from careeros.models import dumps


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, apify={}, api={"endpoint": "both"}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _write_raw(cfg: Config, date: str, *, provider: str, queries: list, items: list) -> None:
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "discover")
    with open(stage_dir / "raw.json", "w") as f:
        f.write(dumps({"provider": provider, "queries": queries, "items": items}))


def test_returns_none_when_no_raw_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _build_discovery_stats(_cfg(), "2026-07-08") is None


def test_splits_ats_vs_job_board_by_source_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    items = [{"source_type": "ats", "source": "greenhouse"}] * 3 + [{"source_type": "jb", "source": "linkedin"}]
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs", queries=["single"], items=items)
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert stats["ats_count"] == 3
    assert stats["jb_count"] == 1


def test_top_platforms_sorted_by_count_desc_capped_at_5(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    items = (
        [{"source_type": "ats", "source": "greenhouse"}] * 5
        + [{"source_type": "ats", "source": "ashby"}] * 2
        + [{"source_type": "ats", "source": "lever"}]
        + [{"source_type": "ats", "source": "workday"}]
        + [{"source_type": "ats", "source": "smartrecruiters"}]
        + [{"source_type": "ats", "source": "teamtailor"}]
    )
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs", queries=["single"], items=items)
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert stats["top_platforms"][0] == ("greenhouse", 5)
    assert len(stats["top_platforms"]) == 5


def test_requests_this_run_derived_from_query_count_and_endpoint_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(api={"endpoint": "both"})
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs",
               queries=["global_remote", "india_remote", "onsite"], items=[])
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert stats["requests_this_run"] == 3 * 2  # 3 tiers x both endpoints


def test_requests_this_run_single_endpoint_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(api={"endpoint": "active-ats"})
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs", queries=["a", "b"], items=[])
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert stats["requests_this_run"] == 2 * 1


def test_records_and_quota_reflect_rolling_week_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(api={"endpoint": "both", "plan": "free"})
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs", queries=["single"],
               items=[{"source_type": "ats"}] * 10)
    budget.save_state(cfg.careeros_dir, {"week_start": "2026-07-06", "records": 46, "requests": 6})
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert stats["records_this_run"] == 10
    assert stats["records_this_week"] == 46
    assert stats["records_quota"] == 500
    assert stats["requests_this_week"] == 6


def test_no_request_record_stats_for_non_fantastic_jobs_provider(tmp_path, monkeypatch):
    """The actor provider has its own cost model — no endpoint/requests
    concept to compute, so those keys are simply absent rather than wrong."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(provider="fantastic-jobs-actor")
    _write_raw(cfg, "2026-07-08", provider="fantastic-jobs-actor", queries=["single"], items=[])
    stats = _build_discovery_stats(cfg, "2026-07-08")
    assert "requests_this_run" not in stats
    assert "records_this_run" not in stats
