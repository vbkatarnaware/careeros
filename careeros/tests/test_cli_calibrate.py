"""CLI-level tests for `careeros calibrate` (careeros/cli/calibrate.py) —
a small SYNTHETIC golden set, not the real 18-entry one seeded in
.careeros/golden/, so these stay hermetic and fast."""

from __future__ import annotations

import json
import sys

import pytest
import typer

import careeros.cli  # noqa: F401 -- ensures careeros.cli.calibrate is imported
from careeros import runmeta
from careeros.config import Config
from careeros.models import Job, dumps

# `careeros/cli/__init__.py` re-exports `calibrate` (the typer COMMAND
# function) as its own package attribute for backward compatibility, which
# shadows the submodule of the same name on that attribute — so
# `from careeros.cli import calibrate` (and even `import careeros.cli.calibrate
# as x`, which resolves via the same attribute chain) both hand back the
# function, not the module. Pull the real module straight out of sys.modules
# instead, same trick the package's own docstring describes for other
# re-exported names.
calibrate_mod = sys.modules["careeros.cli.calibrate"]


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={"eval": "v2"},
        sheets={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _job(job_id: str, title="Product Manager", company="Acme") -> dict:
    return dict(
        id=job_id, source="fantastic-jobs", title=title, company=company,
        apply_url=f"https://example.com/{job_id}", location="India", remote=True,
        seniority=None, employment_type="full_time", description="A real job description.",
    )


def _seal_golden_set(cfg: Config, entries_and_jobs: list[tuple[dict, dict]]) -> None:
    golden_dir = cfg.careeros_dir / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    jobs = [j for _, j in entries_and_jobs]
    entries = [e for e, _ in entries_and_jobs]
    with open(golden_dir / "jobs.json", "w") as f:
        f.write(dumps(jobs))
    golden_set = {
        "version": 1, "profile_version": 1, "prompt_version": "v2",
        "sealed_at": "2026-08-01", "sealed_by": "human", "entries": entries,
    }
    with open(golden_dir / "golden_set.json", "w") as f:
        f.write(dumps(golden_set))


def _entry(job: dict, band: str, score: float, rubric: dict, holdout=False) -> dict:
    job_hash = Job.from_dict(job).content_hash()
    return {
        "job_hash": job_hash, "job_ref": "jobs.json#0", "label": f"{job['company']} — {job['title']}",
        "expected": {"score": score, "rubric": rubric}, "band": band, "holdout": holdout,
    }


_RUBRIC = {"role_fit": 4.0, "seniority_fit": 4.0, "skills_match": 4.0, "domain": 4.0, "logistics": 4.0}


def test_check_reports_clean_when_todays_eval_matches_golden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    cfg = _cfg()
    date = "2026-08-01"

    job = _job("golden-job-1")
    entry = _entry(job, "apply", 4.0, _RUBRIC)
    _seal_golden_set(cfg, [(entry, job)])

    # today's eval output happens to include the golden job, matching exactly
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    record = {
        "id": "golden-job-1", "score": 4.0, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f", "rubric": _RUBRIC,
        "prompt_version": "v2", "profile_version": 1, "job_hash": entry["job_hash"],
    }
    with open(stage_dir / "golden-job-1.json", "w") as f:
        f.write(dumps(record))

    calibrate_mod._calibrate_check(cfg, date)  # should not raise


def test_probe_samples_and_writes_input_excluding_holdout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    cfg = _cfg()
    date = "2026-08-01"

    job1 = _job("g1", company="Co1")
    job2 = _job("g2", company="Co2")
    entry1 = _entry(job1, "apply", 4.5, _RUBRIC)
    entry2 = _entry(job2, "omit", 2.0, _RUBRIC, holdout=True)
    _seal_golden_set(cfg, [(entry1, job1), (entry2, job2)])

    calibrate_mod._calibrate_probe(cfg, date, n=5)

    probe_input = json.loads((cfg.careeros_dir / "golden" / "_probe_input.json").read_text())
    ids = {e["job"]["id"] for e in probe_input}
    assert ids == {"g1"}, "holdout entries must never be sampled into a probe"


def test_score_blocks_on_signed_inflation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    cfg = _cfg()
    date = "2026-08-01"

    job = _job("g1")
    entry = _entry(job, "omit", 2.0, _RUBRIC)
    _seal_golden_set(cfg, [(entry, job)])

    output_dir = cfg.careeros_dir / "golden" / "_probe_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    inflated = {
        "id": "g1", "score": 4.6, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f", "rubric": _RUBRIC,
        "prompt_version": "v2", "profile_version": 1, "job_hash": entry["job_hash"],
    }
    with open(output_dir / f"{entry['job_hash']}.json", "w") as f:
        f.write(dumps(inflated))

    with pytest.raises(typer.Exit):
        calibrate_mod._calibrate_score(cfg, date)


def test_approve_refuses_unsealed_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    cfg = _cfg()
    golden_dir = cfg.careeros_dir / "golden"
    golden_dir.mkdir(parents=True)
    draft = {
        "version": 1, "profile_version": 1, "prompt_version": "v2",
        "sealed_at": "2026-08-01", "sealed_by": "agent",  # not "human"
        "entries": [],
    }
    with open(golden_dir / "golden_set.draft.json", "w") as f:
        json.dump(draft, f)

    with pytest.raises(typer.Exit):
        calibrate_mod._calibrate_approve(cfg)
    assert not (golden_dir / "golden_set.json").exists()


def test_approve_seals_a_reviewed_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    cfg = _cfg()
    golden_dir = cfg.careeros_dir / "golden"
    golden_dir.mkdir(parents=True)
    job = _job("g1")
    entry = _entry(job, "apply", 4.0, _RUBRIC)
    draft = {
        "version": 1, "profile_version": 1, "prompt_version": "v2",
        "sealed_at": "2026-08-01", "sealed_by": "human", "entries": [entry],
    }
    with open(golden_dir / "golden_set.draft.json", "w") as f:
        f.write(dumps(draft))

    calibrate_mod._calibrate_approve(cfg)

    assert (golden_dir / "golden_set.json").exists()
    sealed = json.loads((golden_dir / "golden_set.json").read_text())
    assert len(sealed["entries"]) == 1
