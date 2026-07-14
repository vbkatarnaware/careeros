"""Tests for careeros/cli/'s `summary` command's v1.6.0 local-first digest:
alongside the existing internal `runs/<date>/summary.md`, it now also writes
a stable `.careeros/results/<date>/summary.md` + a `latest` pointer, with
relative links to each Apply job's rendered resume/cover PDF — the one
place a local-only candidate (no Sheets/Drive) is told to look."""

from __future__ import annotations

from unittest.mock import patch

from careeros import runmeta
from careeros.cli import summary
from careeros.config import Config
from careeros.models import dumps
from careeros.tests.conftest import make_job


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _minimal_eval(job_id: str, score: float = 4.3) -> dict:
    return {
        "id": job_id, "score": score, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["great fit"], "weaknesses": [], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4, "seniority_fit": 4, "skills_match": 4, "domain": 4, "logistics": 4},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h",
    }


def test_summary_writes_stable_results_dir_and_latest_pointer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-07-14"
    job = make_job(id="job-1")

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "selected.json", "w") as f:
        f.write(dumps([_minimal_eval("job-1")]))
    with open(select_dir / "consider.json", "w") as f:
        f.write(dumps([]))
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([job.to_dict()]))

    artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, "job-1")
    (artifacts_path / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    (artifacts_path / "cover.pdf").write_bytes(b"%PDF-1.4 fake")

    with patch("careeros.cli.reports._config", return_value=cfg):
        summary(date=date)

    results_path = tmp_path / ".careeros" / "results" / date / "summary.md"
    assert results_path.exists()
    content = results_path.read_text()
    assert "[resume](" in content
    assert "[cover](" in content

    latest = tmp_path / ".careeros" / "results" / "latest"
    assert latest.is_symlink()
    assert latest.resolve() == results_path.parent.resolve()

    internal_path = runmeta.run_dir(cfg.runs_dir, date) / "summary.md"
    assert internal_path.exists()
    assert internal_path.read_text() == content


def test_summary_apply_job_without_pdfs_has_no_broken_links(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-07-14"
    job = make_job(id="job-1")

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "selected.json", "w") as f:
        f.write(dumps([_minimal_eval("job-1")]))
    with open(select_dir / "consider.json", "w") as f:
        f.write(dumps([]))
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([job.to_dict()]))

    with patch("careeros.cli.reports._config", return_value=cfg):
        summary(date=date)

    results_path = tmp_path / ".careeros" / "results" / date / "summary.md"
    content = results_path.read_text()
    assert "[resume](" not in content
    assert "[cover](" not in content
