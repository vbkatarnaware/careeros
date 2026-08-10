"""Tests for careeros/cli/gate_evaluate.py's `_gate_input_job` — the Gate-
only description trim (v1.9, cfg.gate_description_max_chars). Measured
across this project's own past runs: 86% of gate tokens are the description
field, for a stage that only needs a keep/drop call — this trims what the
Gate sees WITHOUT touching eligible.json or what `evaluate` reads later."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.cli._shared import _today
from careeros.cli.gate_evaluate import _gate_input_job, _gate_prepare
from careeros.config import Config


def _cfg(**overrides) -> Config:
    base = dict(threshold=4.0, consider_threshold=3.5, gate_batch_size=50,
                description_max_chars=4000, gate_description_max_chars=900)
    base.update(overrides)
    return Config(**base)


# ── _gate_input_job ────────────────────────────────────────────────────

def test_gate_input_job_truncates_long_description():
    job = {"id": "1", "description": "x" * 2000}
    trimmed = _gate_input_job(job, max_chars=900)
    assert len(trimmed["description"]) == 901  # 900 chars + ellipsis
    assert trimmed["description"].endswith("…")


def test_gate_input_job_leaves_short_description_untouched():
    job = {"id": "1", "description": "short JD text"}
    trimmed = _gate_input_job(job, max_chars=900)
    assert trimmed["description"] == "short JD text"


def test_gate_input_job_handles_missing_description():
    job = {"id": "1"}
    trimmed = _gate_input_job(job, max_chars=900)
    assert trimmed == {"id": "1"}


def test_gate_input_job_does_not_mutate_original_dict():
    job = {"id": "1", "description": "x" * 2000}
    _gate_input_job(job, max_chars=900)
    assert len(job["description"]) == 2000  # original untouched


def test_gate_input_job_preserves_other_fields():
    job = {"id": "1", "title": "PM", "company": "Acme", "description": "x" * 2000}
    trimmed = _gate_input_job(job, max_chars=900)
    assert trimmed["title"] == "PM"
    assert trimmed["company"] == "Acme"


# ── _gate_prepare: end to end, eligible.json stays untouched ──────────

def test_gate_prepare_trims_input_but_not_eligible_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = _today()

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    long_description = "y" * 3000
    jobs = [{"id": "job1", "title": "PM", "company": "Acme", "description": long_description}]
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump(jobs, f)

    _gate_prepare(cfg, date)

    # eligible.json itself is untouched — evaluate reads this independently
    with open(constraints_dir / "eligible.json") as f:
        assert json.load(f)[0]["description"] == long_description

    # the gate's own input is trimmed
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "_input_0.json") as f:
        gate_input = json.load(f)
    assert len(gate_input[0]["description"]) == 901


def test_gate_prepare_respects_custom_gate_description_max_chars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(gate_description_max_chars=100)
    date = _today()

    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        json.dump([{"id": "job1", "description": "z" * 500}], f)

    _gate_prepare(cfg, date)

    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "_input_0.json") as f:
        gate_input = json.load(f)
    assert len(gate_input[0]["description"]) == 101
