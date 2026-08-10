"""Tests for the three independent freshness clocks added to `careeros
doctor` (2026-08-10 gap audit): job/source snapshot freshness (local
dataset cache), reference-registry freshness, and watchlist freshness.
All offline — no network calls, matching test_doctor.py's pattern."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from careeros.cli import _CheckStatus, _run_doctor_checks
from careeros.config import Config


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, api={}, fx_rates={}, drive={"enabled": False},
        providers={"ats-dataset": {"enabled": True}},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _status_for(results, label_substr):
    for status, label, detail in results:
        if label_substr in label:
            return status, detail
    return None, None


def _init_careeros_dir(tmp_path):
    d = tmp_path / ".careeros"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "profile.yaml", "w") as f:
        yaml.safe_dump({"version": 1, "candidate": {}, "headline": "", "targets": [], "experience": []}, f)
    return d


# ── job snapshot freshness (local dataset cache) ────────────────────────

def test_snapshot_freshness_warns_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path)
    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Job snapshot freshness")
    assert status == _CheckStatus.WARN
    assert "no cached slices" in detail


def test_snapshot_freshness_reports_recent_cache_as_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    dataset_dir = careeros_dir / "dataset"
    dataset_dir.mkdir()
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    (dataset_dir / f"greenhouse-{recent.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Job snapshot freshness")
    assert status == _CheckStatus.PASS
    assert "0." in detail  # under 1 day old


def test_snapshot_freshness_warns_when_cache_is_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    dataset_dir = careeros_dir / "dataset"
    dataset_dir.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    (dataset_dir / f"greenhouse-{old.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Job snapshot freshness")
    assert status == _CheckStatus.WARN
    assert "10." in detail


def test_snapshot_freshness_uses_newest_of_multiple_slices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    dataset_dir = careeros_dir / "dataset"
    dataset_dir.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    (dataset_dir / f"greenhouse-{old.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")
    (dataset_dir / f"lever-{recent.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")

    results = _run_doctor_checks(_cfg())
    status, _ = _status_for(results, "Job snapshot freshness")
    assert status == _CheckStatus.PASS  # driven by the newest file, not the oldest


# ── reference registry freshness ─────────────────────────────────────────

def test_registry_freshness_warns_when_never_synced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path)
    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Reference registry freshness")
    assert status == _CheckStatus.WARN
    assert "never synced" in detail


def test_registry_freshness_passes_after_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    registry_dir = careeros_dir / "registry"
    registry_dir.mkdir()
    meta = {"row_count": 79906, "last_synced_at": "2026-08-10T00:31:47Z", "imported_at": "2026-08-10T00:31:47Z"}
    with open(registry_dir / "reference_meta.json", "w") as f:
        json.dump(meta, f)

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Reference registry freshness")
    assert status == _CheckStatus.PASS
    assert "79906" in detail


# ── watchlist freshness ──────────────────────────────────────────────────

def test_watchlist_freshness_warns_when_no_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path)
    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Watchlist freshness")
    assert status == _CheckStatus.WARN
    assert "no entries" in detail


def test_watchlist_freshness_warns_when_entries_never_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}]}, f)

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Watchlist freshness")
    assert status == _CheckStatus.WARN
    assert "none ever checked" in detail


def test_watchlist_freshness_passes_after_a_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}]}, f)
    with open(careeros_dir / "watchlist_state.json", "w") as f:
        json.dump({"Acme::greenhouse:acme": {"verification_status": "live", "last_checked_at": "2026-08-10"}}, f)

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Watchlist freshness")
    assert status == _CheckStatus.PASS
    assert "2026-08-10" in detail


# ── watchlist mapping status (verification_status breakdown) ────────────

def test_watchlist_status_absent_when_never_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}]}, f)

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Watchlist mapping status")
    assert status is None  # no row at all — nothing to report yet


def test_watchlist_status_counts_by_verification_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [
            {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
            {"name": "Ghost Co", "ats": "greenhouse", "slug": "ghost"},
        ]}, f)
    with open(careeros_dir / "watchlist_state.json", "w") as f:
        json.dump({
            "Acme::greenhouse:acme": {"verification_status": "live"},
            "Ghost Co::greenhouse:ghost": {"verification_status": "stale"},
        }, f)

    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Watchlist mapping status")
    assert status == _CheckStatus.WARN  # a stale mapping present
    assert "live=1" in detail and "stale=1" in detail


def test_watchlist_status_passes_when_all_live(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}]}, f)
    with open(careeros_dir / "watchlist_state.json", "w") as f:
        json.dump({"Acme::greenhouse:acme": {"verification_status": "live"}}, f)

    results = _run_doctor_checks(_cfg())
    status, _ = _status_for(results, "Watchlist mapping status")
    assert status == _CheckStatus.PASS


# ── Layer 2A output visibility ──────────────────────────────────────────

def test_layer2a_output_absent_when_never_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path)
    results = _run_doctor_checks(_cfg(providers={"ats-dataset": {"enabled": True}, "ats-watchlist": {"enabled": True}}))
    status, _ = _status_for(results, "Layer 2A output")
    assert status is None


def test_layer2a_output_reports_jobs_produced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = _init_careeros_dir(tmp_path)
    run_dir = careeros_dir / "runs" / "2026-08-10" / "01_discover"
    run_dir.mkdir(parents=True)
    raw = {"providers": ["ats-watchlist"], "items": {"ats-watchlist": []},
           "meta": {"ats-watchlist": {"records": 19, "skipped": False}}}
    with open(run_dir / "raw.json", "w") as f:
        json.dump(raw, f)

    results = _run_doctor_checks(_cfg(providers={"ats-dataset": {"enabled": True}, "ats-watchlist": {"enabled": True}}))
    status, detail = _status_for(results, "Layer 2A output")
    assert status == _CheckStatus.PASS
    assert "19 job(s)" in detail
