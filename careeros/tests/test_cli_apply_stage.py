"""Tests for careeros/cli.py's `apply --prepare/--finalize` batch stage
(P2.10): for every Apply-tier job, classify its application form's fetch
outcome into one of the specific STATUS_* codes (generated / login_required
/ playwright_missing / closed / no_essay_questions / network_error /
manual_required — the fallback for any unclassified failure), then validate
whatever answers.md the agent wrote. The background form-fetch itself
(`careeros.apply.browser.fetch_visible_text`) is mocked throughout -- see
test_apply_browser.py for that module's own unit tests of the actual
login-wall/closed-posting/JS-shell detection logic (invisible to these
tests, since they mock the boundary function directly), and
test_provider_fantastic_jobs.py-style direct-function-call conventions
(no CliRunner/subprocess) for why these call `_apply_prepare`/
`_apply_finalize` directly."""

from __future__ import annotations

import json

import pytest
import typer

from careeros import runmeta
from careeros.apply import browser as apply_browser
from careeros.cli import (
    STATUS_BOT_CHECK,
    STATUS_CLOSED,
    STATUS_GENERATED,
    STATUS_LOGIN_REQUIRED,
    STATUS_MANUAL_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_NO_ESSAY_QUESTIONS,
    STATUS_PLAYWRIGHT_MISSING,
    _apply_finalize,
    _apply_prepare,
    _load_apply_status,
)
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
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4, "seniority_fit": 4, "skills_match": 4, "domain": 4, "logistics": 4},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h",
    }


def _seed_selected(cfg, date, jobs):
    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "selected.json", "w") as f:
        f.write(dumps([_minimal_eval(j.id) for j in jobs]))
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))


# ── --prepare ──────────────────────────────────────────────────────────

def test_prepare_marks_manual_required_when_reason_unclassified(tmp_path, monkeypatch):
    """An empty fetch with no specific REASON_* attached falls back to the
    generic manual_required status -- the pre-existing catch-all."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text", lambda url, **kw: (None, "none", None)
    )
    _apply_prepare(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_MANUAL_REQUIRED}
    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    assert not (artifacts / "_apply_input.json").exists()


@pytest.mark.parametrize(
    "reason, expected_status",
    [
        (apply_browser.REASON_LOGIN_WALL, STATUS_LOGIN_REQUIRED),
        (apply_browser.REASON_CLOSED_POSTING, STATUS_CLOSED),
        (apply_browser.REASON_PLAYWRIGHT_MISSING, STATUS_PLAYWRIGHT_MISSING),
        (apply_browser.REASON_NETWORK_ERROR, STATUS_NETWORK_ERROR),
        (apply_browser.REASON_BOT_CHECK, STATUS_BOT_CHECK),
    ],
)
def test_prepare_maps_each_fetch_reason_to_its_specific_status(
    tmp_path, monkeypatch, reason, expected_status
):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text", lambda url, **kw: (None, "none", reason)
    )
    _apply_prepare(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": expected_status}


def test_prepare_uses_reason_even_when_form_text_is_truthy(tmp_path, monkeypatch):
    """Regression: browser.py's login-wall/closed-posting/bot-check
    detection returns the ACTUAL fetched text alongside the reason (it's
    real, substantial text -- just the wrong page, not an empty fetch; see
    fetch_visible_text's docstring). `_apply_prepare` must check `reason`
    BEFORE `form_text` truthiness, or a LinkedIn login-wall page (which has
    plenty of real boilerplate text) would silently fall through to the
    agent as if it were a genuine readable form instead of being
    short-circuited to login_required."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: (
            "Sign in to see who you already know. " * 20,
            "http",
            apply_browser.REASON_LOGIN_WALL,
        ),
    )
    _apply_prepare(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_LOGIN_REQUIRED}
    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    assert not (artifacts / "_apply_input.json").exists()  # never sent to the agent


def test_prepare_writes_input_for_readable_form(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1", apply_url="https://example.com/apply/1")
    _seed_selected(cfg, "2026-07-10", [job])

    form_text = "Why do you want to work here?\nWhat is your notice period?"
    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text", lambda url, **kw: (form_text, "http", None)
    )
    _apply_prepare(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {}  # not yet resolved -- pending agent draft
    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    input_path = artifacts / "_apply_input.json"
    assert input_path.exists()
    with open(input_path) as f:
        payload = json.load(f)
    assert payload["form_text"] == form_text
    assert payload["fetch_method"] == "http"
    assert payload["id"] == "job-1"


def test_prepare_skips_refetch_when_answers_already_exist(tmp_path, monkeypatch):
    """Idempotent resume: a job whose answers.md already exists (e.g. a
    resumed run) must not be refetched or re-drafted."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_selected(cfg, "2026-07-10", [job])
    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    (artifacts / "answers.md").write_text("# Application Answers\n\n## Q\nA\n")

    calls = []
    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: calls.append(url) or (None, "none", None),
    )
    _apply_prepare(cfg, "2026-07-10")
    assert calls == []  # never called

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_GENERATED}


def test_prepare_handles_multiple_jobs_mixed_outcomes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    readable = make_job(id="job-readable", apply_url="https://example.com/a")
    closed = make_job(id="job-closed", apply_url="https://example.com/b")
    _seed_selected(cfg, "2026-07-10", [readable, closed])

    def fake_fetch(url, **kw):
        if url.endswith("/a"):
            return "Real question text here, plenty of it.", "http", None
        return None, "http", apply_browser.REASON_CLOSED_POSTING

    monkeypatch.setattr("careeros.cli.apply_browser.fetch_visible_text", fake_fetch)
    _apply_prepare(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-closed": STATUS_CLOSED}
    readable_artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-readable")
    assert (readable_artifacts / "_apply_input.json").exists()


# ── --finalize ────────────────────────────────────────────────────────

def test_finalize_marks_generated_when_answers_written_and_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1", apply_url="https://example.com/apply/1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: ("Why do you want to work here?", "http", None),
    )
    _apply_prepare(cfg, "2026-07-10")

    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    (artifacts / "answers.md").write_text(
        "# Application Answers\n\n## Why do you want to work here?\nBecause it is a great fit.\n"
    )

    _apply_finalize(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_GENERATED}


def test_finalize_marks_no_essay_questions_when_agent_skipped_a_real_form(tmp_path, monkeypatch):
    """prepare fetched a genuinely real, usable form (reason=None) but the
    agent legitimately chose not to write answers.md because it found no
    actual free-text essay questions in it -- finalize must mark that
    specific outcome, not the generic manual_required, since the form WAS
    readable; there just weren't any questions to answer."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1", apply_url="https://example.com/apply/1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: ("First Name\nLast Name\nResume\nSubmit Application", "http", None),
    )
    _apply_prepare(cfg, "2026-07-10")
    # agent never wrote answers.md for job-1 (no essay questions found)

    _apply_finalize(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_NO_ESSAY_QUESTIONS}


def test_finalize_raises_on_voice_dna_issues_and_does_not_save_bad_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1", apply_url="https://example.com/apply/1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: ("Why do you want to work here?", "http", None),
    )
    _apply_prepare(cfg, "2026-07-10")

    artifacts = runmeta.artifacts_dir(cfg.runs_dir, "2026-07-10", "job-1")
    (artifacts / "answers.md").write_text(
        "## Why do you want to work here?\nWe will leverage synergy for a paradigm shift.\n"
    )

    with pytest.raises(typer.Exit):
        _apply_finalize(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {}  # unresolved -- errors block saving a status for this job


def test_finalize_leaves_already_resolved_jobs_untouched(tmp_path, monkeypatch):
    """A job prepare already marked with a terminal status (login required,
    closed, playwright missing, network error, or the generic fallback)
    must not be re-examined by finalize (no answers.md is expected for
    it)."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    job = make_job(id="job-1")
    _seed_selected(cfg, "2026-07-10", [job])

    monkeypatch.setattr(
        "careeros.cli.apply_browser.fetch_visible_text",
        lambda url, **kw: (None, "none", apply_browser.REASON_LOGIN_WALL),
    )
    _apply_prepare(cfg, "2026-07-10")
    _apply_finalize(cfg, "2026-07-10")

    status = _load_apply_status(cfg, "2026-07-10")
    assert status == {"job-1": STATUS_LOGIN_REQUIRED}
