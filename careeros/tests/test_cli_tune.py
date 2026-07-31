"""CLI-level tests for `careeros tune` (careeros/cli/tune.py) — the full
propose -> apply -> revert lifecycle against a seeded ledger, and the
guardrails that make it refuse to act before a tier is armed."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest
import typer

from careeros import runmeta
from careeros.config import Config
from careeros.models import dumps

tune_mod = sys.modules.get("careeros.cli.tune")
if tune_mod is None:
    import careeros.cli  # noqa: F401
    tune_mod = sys.modules["careeros.cli.tune"]


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={}, api={"limit": 100}, fx_rates={}, drive={"enabled": False},
        tuning={
            "overlay_path": ".careeros/tuning/overlay.yaml", "control_tier": "india_remote",
            "min_days": 28, "min_records": 400, "min_events": 8,
            "max_exclusion_entries": 8, "exclusion_sunset_days": 90,
            "tier_limit_max_delta_pct": 0.25, "cadence_days": 30,
        },
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed_armed_tier(cfg, tier_name, *, num_runs=29, records_per_run=20, events_per_run=1, start="2026-06-01"):
    """Seeds enough runs for `tier_name` to clear all three arming floors:
    >=28 days span, >=400 records, >=8 apply/consider events."""
    start_date = date.fromisoformat(start)
    for i in range(num_runs):
        d = (start_date + timedelta(days=i)).isoformat()
        jobs = [{"id": f"{d}-job-{j}", "tiers": [tier_name]} for j in range(records_per_run)]
        normalize_dir = runmeta.stage_dir(cfg.runs_dir, d, "normalize")
        with open(normalize_dir / "jobs.json", "w") as f:
            f.write(dumps(jobs))
        processed = []
        outcomes = []
        for j in range(records_per_run):
            jid = f"{d}-job-{j}"
            if j < events_per_run:
                reason = "apply"
                score = 4.2
            elif j < events_per_run + 3:
                reason = "gate-dropped"
                processed.append({"id": jid, "date": d, "terminal_stage": "gate", "reason": "role-mismatch", "score": None})
                continue
            else:
                reason = "omit"
                score = 2.0
            processed.append({"id": jid, "date": d, "terminal_stage": "select", "reason": reason, "score": score})
            outcomes.append({"id": jid, "tier": reason if reason != "gate-dropped" else "omit", "score": score,
                             "recommendation": "apply" if reason == "apply" else "skip",
                             "shortfall": {}, "primary_gap": None, "secondary_gap": None, "tiers": [tier_name]})
        with open(cfg.careeros_dir / "processed.jsonl", "a") as f:
            for r in processed:
                f.write(json.dumps(r) + "\n")
        select_dir = runmeta.stage_dir(cfg.runs_dir, d, "select")
        with open(select_dir / "outcomes.json", "w") as f:
            f.write(dumps(outcomes))
    return (start_date + timedelta(days=num_runs - 1)).isoformat()


def test_status_shows_armed_tier(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    _seed_armed_tier(cfg, "onsite")
    with patch("careeros.cli.tune._config", return_value=cfg):
        tune_mod._tune_status(cfg)
    out = capsys.readouterr().out
    assert "onsite" in out
    assert "ARMED" in out


def test_propose_rejected_for_unarmed_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    # A tiny amount of data -- nowhere near armed.
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, "2026-07-31", "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([{"id": "j1", "tiers": ["global_remote"]}]))
    select_dir = runmeta.stage_dir(cfg.runs_dir, "2026-07-31", "select")
    with open(select_dir / "outcomes.json", "w") as f:
        f.write(dumps([]))

    with patch("careeros.cli.tune._config", return_value=cfg), \
         patch("careeros.cli.tune._today", return_value="2026-07-31"):
        with pytest.raises(typer.Exit):
            tune_mod._tune_propose(
                cfg, tier="global_remote", field="title_exclusion_search",
                value="Brand", evidence="some evidence", sunset="2026-11-01",
            )


def test_full_propose_apply_revert_lifecycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    last_date = _seed_armed_tier(cfg, "onsite")

    with patch("careeros.cli.tune._config", return_value=cfg), \
         patch("careeros.cli.tune._today", return_value=last_date):
        tune_mod._tune_propose(
            cfg, tier="onsite", field="title_exclusion_search",
            value="Brand", evidence="11 records gate-dropped role-mismatch with 'Brand' in the title",
            sunset="2026-11-01",
        )

        proposal_path = cfg.careeros_dir / "tuning" / "proposal.json"
        assert proposal_path.exists()

        tune_mod._tune_apply(cfg)
        assert not proposal_path.exists(), "applying must clear the staged proposal"

        overlay = tune_mod._read_overlay_api(cfg)
        assert overlay["title_exclusion_search"] == ["Brand"]

        changes = tune_mod._load_changes(cfg)
        assert len(changes) == 1
        assert changes[0]["field"] == "title_exclusion_search"
        assert not changes[0]["reverted"]

        # A second proposal in the same cycle must be rejected (max one
        # change per cycle, across ALL tiers).
        with pytest.raises(typer.Exit):
            tune_mod._tune_propose(
                cfg, tier="onsite", field="title_exclusion_search",
                value="Marketing", evidence="more evidence", sunset="2026-11-01",
            )

        tune_mod._tune_revert(cfg)
        overlay_after_revert = tune_mod._read_overlay_api(cfg)
        assert overlay_after_revert.get("title_exclusion_search", []) == []

        changes_after = tune_mod._load_changes(cfg)
        assert changes_after[0]["reverted"] is True

        quarantine = json.loads((cfg.careeros_dir / "tuning" / "quarantine.json").read_text())
        assert len(quarantine["reverted_changes"]) == 1


def test_propose_requires_all_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.tune._config", return_value=cfg):
        with pytest.raises(typer.Exit):
            tune_mod._tune_propose(cfg, tier="onsite", field=None, value=None, evidence=None, sunset=None)


def test_apply_with_no_staged_proposal_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.tune._config", return_value=cfg):
        with pytest.raises(typer.Exit):
            tune_mod._tune_apply(cfg)


def test_revert_with_no_active_changes_is_a_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.tune._config", return_value=cfg):
        tune_mod._tune_revert(cfg)
    assert "No active" in capsys.readouterr().out


# ── --check-due (the automatic, daily-wired weekly check) ────────────────

def test_check_due_not_yet_due_is_a_fast_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    last_check_dir = cfg.careeros_dir / "tuning"
    last_check_dir.mkdir(parents=True)
    (last_check_dir / "last_check.json").write_text(json.dumps({"last_checked": "2026-07-30"}))

    with patch("careeros.cli.tune._config", return_value=cfg), \
         patch("careeros.cli.tune._today", return_value="2026-08-01"):
        tune_mod._tune_check_due(cfg)

    assert "Not due yet" in capsys.readouterr().out
    assert not (last_check_dir / "pending_flags.json").exists()


def test_check_due_with_no_armed_pattern_stays_quiet(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    with patch("careeros.cli.tune._config", return_value=cfg), \
         patch("careeros.cli.tune._today", return_value="2026-08-01"):
        tune_mod._tune_check_due(cfg)

    out = capsys.readouterr().out
    assert "nothing worth flagging" in out
    assert (cfg.careeros_dir / "tuning" / "last_check.json").exists()
    assert not (cfg.careeros_dir / "tuning" / "pending_flags.json").exists()


def test_check_due_surfaces_flag_and_writes_pending_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    last_date = _seed_armed_tier(cfg, "onsite", records_per_run=20, events_per_run=1)
    # _seed_armed_tier's gate-dropped jobs all use reason "role-mismatch" (3 per run x 29 runs = 87 >= 8)

    with patch("careeros.cli.tune._config", return_value=cfg), \
         patch("careeros.cli.tune._today", return_value=last_date):
        tune_mod._tune_check_due(cfg)

    out = capsys.readouterr().out
    assert "pattern(s) worth asking" in out
    assert "onsite" in out

    pending = json.loads((cfg.careeros_dir / "tuning" / "pending_flags.json").read_text())
    assert pending[0]["tier"] == "onsite"
    assert pending[0]["top_reason"] == "role-mismatch"

    last_check = json.loads((cfg.careeros_dir / "tuning" / "last_check.json").read_text())
    assert last_check["last_checked"] == last_date
