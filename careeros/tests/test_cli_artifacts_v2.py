"""Tests for the v1.4.0 artifacts pipeline wiring in careeros/cli.py:
`_artifacts_finalize` now validates resume.json against schemas/resume.schema.json,
runs verify_resume_facts (fact-preservation + company-name-leak), and renders
resume.pdf locally via careeros/typst_render.py, gated on the ATS one-page
requirement. Real typst is exercised (cheap, deterministic) — skipped
entirely if the optional `[resume]` extra isn't installed locally."""

from __future__ import annotations

import json

import pytest
import typer

pytest.importorskip("typst", reason="requires the optional [resume] extra (typst)")

from careeros import runmeta
from careeros.cli import _artifacts_finalize
from careeros.config import Config

_PROFILE_YAML = """\
version: 1
candidate:
  full_name: "Test Candidate"
  email: "test@example.com"
  phone: "+1-555-0100"
  location: "Testville, TC"
  linkedin: "https://www.linkedin.com/in/testcandidate/"
headline: "Product Manager"
tagline: "Product | Growth | AI"
targets: [product-manager]
summary_variants:
  - id: default
    text: "Product Manager who ships end to end."
    jd_tags: []
experience:
  - company: "Acme Corp"
    role: "Product Manager"
    location: "Remote"
    dates: { start: "2022-01", end: "present" }
    bullets:
      - text: "Shipped widget X, growing revenue 40%."
        tags: [product]
        visibility: headline
      - text: "Led a team of 5 engineers."
        tags: [leadership]
        visibility: supporting
projects: []
skills:
  - { name: "SQL", category: data, tags: [], visibility: headline }
education:
  - { degree: "B.Sc", institution: "Test University", score: "3.8 GPA", dates: { start: "2018", end: "2022" } }
"""


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={"resume": "v2", "cover": "v1"},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _setup_run(tmp_path, monkeypatch, date="2026-07-13", job_id="job-1", score=4.2):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_PROFILE_YAML)
    cfg = _cfg()

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "selected.json", "w") as f:
        json.dump([{
            "id": job_id, "score": score, "confidence": 0.9, "recommendation": "apply",
            "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
            "company_summary": "s", "fit_paragraph": "f",
            "rubric": {"role_fit": 4.5, "seniority_fit": 4.5, "skills_match": 4.5, "domain": 4.0, "logistics": 4.0},
            "prompt_version": "v2", "profile_version": 1, "job_hash": "hash-1",
        }], f)

    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        json.dump([{
            "id": job_id, "source": "test", "title": "Product Manager", "company": "Acme Corp",
            "location": "Remote", "remote": True, "description": "A great JD.",
            "apply_url": "https://example.com/apply",
        }], f)

    artifacts_dir = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
    (artifacts_dir / "cover.md").write_text("Dear Hiring Team,\n\nI am excited to apply.\n\nBest,\nTest Candidate")
    return cfg, date, artifacts_dir


def _write_resume_json(artifacts_dir, **overrides):
    resume_json = {
        "tagline": "Product | Growth | AI",
        "summary": "A concise, honest summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    }
    resume_json.update(overrides)
    (artifacts_dir / "resume.json").write_text(json.dumps(resume_json))
    return resume_json


def test_finalize_happy_path_renders_one_page_pdf_and_caches(tmp_path, monkeypatch):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir)

    _artifacts_finalize(cfg, date)

    pdf_path = artifacts_dir / "resume.pdf"
    assert pdf_path.exists()
    assert pdf_path.read_bytes()[:5] == b"%PDF-"

    # Confirm something got cached under the "resume" stage at all.
    import os
    assert any(os.scandir(cfg.cache_dir / "resume"))


def test_finalize_rejects_schema_invalid_resume_json(tmp_path, monkeypatch, capsys):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    (artifacts_dir / "resume.json").write_text(json.dumps({"tagline": "x"}))  # missing required fields

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "schema" in err
    assert not (artifacts_dir / "resume.pdf").exists()


def test_finalize_rejects_invented_metric(tmp_path, monkeypatch, capsys):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir, experience=[
        {"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 90%."]},  # 90% not in profile
    ])

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "truthfulness" in err
    assert not (artifacts_dir / "resume.pdf").exists()


def test_finalize_rejects_target_company_name_leak(tmp_path, monkeypatch, capsys):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir, experience=[
        {"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%, excited to bring this to Acme Corp."]},
    ])

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "truthfulness" in err


def test_finalize_rejects_a_resume_that_overflows_one_page(tmp_path, monkeypatch, capsys):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    # Force overflow: a huge summary, a maximally-padded bullet (schema caps
    # bullets at 4 per company, so bulk comes from length/skills, not count),
    # and many long skill categories.
    huge_summary = " ".join(
        ["This is a very long summary sentence padded out with extra words to take up space."] * 40
    )
    huge_bullet = "Shipped widget X, growing revenue 40%. " + (
        "Extra padding detail repeated to make this one bullet very long indeed. " * 8
    )
    huge_skills = [
        {"category": f"Category {i}", "items": [f"Skill Item Number {i}-{j} With Extra Words" for j in range(12)]}
        for i in range(20)
    ]
    _write_resume_json(
        artifacts_dir, summary=huge_summary, skills=huge_skills,
        experience=[{"company": "Acme Corp", "bullets": [huge_bullet]}],
    )

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "pages" in err
    assert not (artifacts_dir / "resume.pdf").exists()


def test_finalize_renders_pdf_unconditionally_even_on_cache_hit(tmp_path, monkeypatch):
    """The PDF isn't itself cached — it must be re-rendered locally every
    finalize run, even for a resume.json whose voice/fact check was already
    cached-clean on a prior run (verify/lint themselves still re-run every
    time — see the next test — only the cache.put marker is skipped)."""
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir)

    _artifacts_finalize(cfg, date)
    assert (artifacts_dir / "resume.pdf").exists()

    (artifacts_dir / "resume.pdf").unlink()
    assert not (artifacts_dir / "resume.pdf").exists()

    _artifacts_finalize(cfg, date)  # resume.json content unchanged -> cache hit this time
    assert (artifacts_dir / "resume.pdf").exists()


def test_finalize_still_catches_a_bad_edit_after_a_cache_hit(tmp_path, monkeypatch, capsys):
    """verify_resume_facts/lint must run every finalize, not just on a cache
    miss. Reproduces the exact staleness bug: same job_hash/score/prompt_version
    (so the SAME cache key) across both runs, only the resume.json file content
    changes in between — a cache-gated check would treat the second run as a
    hit and skip re-verification entirely, letting the invented metric ship."""
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir)

    _artifacts_finalize(cfg, date)  # first pass: clean, gets cached
    assert (artifacts_dir / "resume.pdf").exists()

    # Hand-edit in place, introducing a violation, WITHOUT touching anything
    # that's part of the cache key (job_hash/score/prompt_version unchanged).
    _write_resume_json(artifacts_dir, experience=[
        {"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 90%."]},  # 90% not in profile
    ])

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "truthfulness" in err


def test_finalize_rejects_a_companies_entry_not_in_profile(tmp_path, monkeypatch, capsys):
    """Integration-level check that the F1 companies/projects name validation
    (careeros/lint.py's verify_resume_facts) is actually wired into finalize,
    not just correct in isolation — a typo'd `companies` entry passes schema
    validation (it's just a string) but must still be rejected here."""
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir, companies=["Acme Corp Typo"])

    with pytest.raises(typer.Exit):
        _artifacts_finalize(cfg, date)

    err = capsys.readouterr().err
    assert "truthfulness" in err
    assert "Acme Corp Typo" in err


def test_finalize_also_renders_cover_pdf(tmp_path, monkeypatch):
    cfg, date, artifacts_dir = _setup_run(tmp_path, monkeypatch)
    _write_resume_json(artifacts_dir)

    _artifacts_finalize(cfg, date)

    cover_pdf = artifacts_dir / "cover.pdf"
    assert cover_pdf.exists()
    assert cover_pdf.read_bytes()[:5] == b"%PDF-"
