"""CLI-level tests for the v2.0 changes to careeros/cli/pipeline.py's
`dedupe` and `threshold` commands: TTL-aware history, `--replay`, the
sheets.enabled guard fix, `07_select/omitted.json`, and the unconditional
`.careeros/processed.jsonl` write. Complements careeros/tests/test_dedupe.py
(pure functions) and test_threshold.py (partition_evals)."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.cli.pipeline import dedupe, threshold
from careeros.config import Config
from careeros.models import dumps
from careeros.tests.conftest import FX_RATES, make_job, make_profile


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={"enabled": False}, api={}, fx_rates=FX_RATES,
        drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _write_profile(tmp_path):
    (tmp_path / ".careeros").mkdir(exist_ok=True)
    (tmp_path / ".careeros" / "profile.yaml").write_text(
        "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
        "headline: h\ntargets: [pm]\nexperience: []\n"
        "location: {remote: preferred, onsite_ok: [Mumbai]}\n"
        "comp: {floor_lpa: 15, target_lpa: [20, 28], currency: INR}\n"
    )


# ── dedupe: TTL, --replay, sheets-enabled guard ──────────────────────────

def test_dedupe_drops_job_recorded_in_processed_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    jobs = [
        make_job(id="already-processed", remote=True, company="Acme", title="Product Manager"),
        make_job(id="brand-new", remote=True, company="Globex", title="Growth Product Manager"),
    ]
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    processed_path = cfg.careeros_dir / "processed.jsonl"
    with open(processed_path, "w") as f:
        f.write(json.dumps({"id": "already-processed", "date": "2026-07-25",
                             "terminal_stage": "gate", "reason": "role-mismatch", "score": None}) + "\n")

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg):
        dedupe(date=date, against_sheet=False, replay=False)

    unique = json.loads((runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "unique.json").read_text())
    assert [j["id"] for j in unique] == ["brand-new"]

    dropped = json.loads((runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "dropped.json").read_text())
    assert dropped[0]["id"] == "already-processed"
    assert dropped[0]["_drop_reason"] == "history"


def test_dedupe_replay_bypasses_history_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    jobs = [make_job(id="already-processed", remote=True)]
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    processed_path = cfg.careeros_dir / "processed.jsonl"
    with open(processed_path, "w") as f:
        f.write(json.dumps({"id": "already-processed", "date": "2026-07-31",
                             "terminal_stage": "select", "reason": "apply", "score": 4.5}) + "\n")

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg):
        dedupe(date=date, against_sheet=False, replay=True)

    unique = json.loads((runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "unique.json").read_text())
    assert [j["id"] for j in unique] == ["already-processed"], "--replay must let it through despite recent history"


def test_dedupe_skips_sheet_check_when_sheets_disabled(tmp_path, monkeypatch):
    """Regression: --against-sheet used to call the live Sheets API even
    with sheets.enabled: false, relying on a RuntimeError to no-op."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg(sheets={"enabled": False})
    date = "2026-08-01"

    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    jobs = [make_job(id="a", remote=True)]
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg), \
         patch("careeros.cli.pipeline.sheets_mod.read_existing_job_ids") as mock_read:
        dedupe(date=date, against_sheet=True, replay=False)
    mock_read.assert_not_called()


# ── threshold: omitted.json + processed.jsonl ────────────────────────────

def test_threshold_writes_omitted_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    job = make_job(id="weak-job", remote=True)
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([job.to_dict()]))

    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    weak_eval = {
        "id": "weak-job", "score": 2.5, "confidence": 0.8, "recommendation": "skip",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 2, "seniority_fit": 2, "skills_match": 2, "domain": 3, "logistics": 3},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h1",
    }
    with open(eval_dir / "weak-job.json", "w") as f:
        f.write(dumps(weak_eval))

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg):
        threshold(date=date, min_score=None, consider_min=None)

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    omitted = json.loads((select_dir / "omitted.json").read_text())
    assert [e["id"] for e in omitted] == ["weak-job"]


def test_threshold_records_every_terminal_stage_to_processed_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    apply_job = make_job(id="apply-job", remote=True)
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([apply_job.to_dict()]))

    # constraints-rejected job (never reaches gate/evaluate)
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "rejected.json", "w") as f:
        f.write(dumps([{**make_job(id="rejected-job").to_dict(), "_reject_reasons": ["onsite outside onsite_ok"]}]))

    # gate-dropped job (never reaches evaluate)
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([
            {"id": "apply-job", "keep": True, "reason": "role-match", "confidence": 0.8},
            {"id": "gate-dropped-job", "keep": False, "reason": "seniority-mismatch", "confidence": 0.7},
        ]))

    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    apply_eval = {
        "id": "apply-job", "score": 4.5, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4.5, "seniority_fit": 4.5, "skills_match": 4.5, "domain": 4.5, "logistics": 4.5},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h2",
    }
    with open(eval_dir / "apply-job.json", "w") as f:
        f.write(dumps(apply_eval))

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg):
        threshold(date=date, min_score=None, consider_min=None)

    processed_path = cfg.careeros_dir / "processed.jsonl"
    lines = [json.loads(line) for line in processed_path.read_text().splitlines()]
    by_id = {r["id"]: r for r in lines}

    assert by_id["rejected-job"]["terminal_stage"] == "constraints"
    assert by_id["gate-dropped-job"]["terminal_stage"] == "gate"
    assert by_id["apply-job"]["terminal_stage"] == "select"
    assert by_id["apply-job"]["reason"] == "apply"
    assert by_id["apply-job"]["score"] == 4.5


def test_threshold_writes_outcomes_json_with_primary_gap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    job = make_job(id="weak-job", remote=True, company="Acme", title="Product Manager")
    job_dict = job.to_dict()
    job_dict["tiers"] = ["onsite"]
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([job_dict]))

    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    weak_eval = {
        "id": "weak-job", "score": 2.3, "confidence": 0.8, "recommendation": "skip",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 1.5, "seniority_fit": 3.0, "skills_match": 2.0, "domain": 1.8, "logistics": 3.8},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h1",
    }
    with open(eval_dir / "weak-job.json", "w") as f:
        f.write(dumps(weak_eval))

    from unittest.mock import patch
    with patch("careeros.cli.pipeline._config", return_value=cfg):
        threshold(date=date, min_score=None, consider_min=None)

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    outcomes = json.loads((select_dir / "outcomes.json").read_text())
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["id"] == "weak-job"
    assert o["tier"] == "omit"
    assert o["primary_gap"] == "deal_breaker"  # recommendation == "skip"
    assert o["tiers"] == ["onsite"]
