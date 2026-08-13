"""Regression tests for the 2026-08-12 token-optimization pass (see
docs audit): flat Evaluate batching, no nested sub-agent invitation,
batch retry/checkpoint, deterministic HTML-stripping, and the structured-
location-field pre-filter. Every test here proves the change did NOT touch
the rubric/grounding/schema Evaluate writes -- only the dispatch mechanics
around it."""

from __future__ import annotations

import json
from pathlib import Path

from careeros import runmeta
from careeros.cli import _evaluate_prepare
from careeros.cli.gate_evaluate import _evaluate_input_job, strip_html
from careeros.config import Config
from careeros.models import Job, dumps
from careeros.pipeline.constraints import _region_restricted_remote, evaluate_constraints
from careeros.tests.conftest import FX_RATES, make_job, make_profile

_VALID_PROFILE = (
    "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
)


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        eval_batch_size=50,
        goals={}, prompts={"eval": "v2"},
        sheets={}, api={}, fx_rates={}, drive={"enabled": False},
        calibration={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed(cfg, date, job_ids: list[str], *, descriptions: dict[str, str] | None = None):
    descriptions = descriptions or {}
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([{"id": jid, "keep": True, "reason": "role-match", "confidence": 0.8} for jid in job_ids]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    jobs = [
        {
            "id": jid, "source": "fantastic-jobs", "title": "Product Manager", "company": "Acme",
            "apply_url": f"https://x/{jid}", "location": "India", "remote": True,
            "description": descriptions.get(jid, f"description for {jid}"),
        }
        for jid in job_ids
    ]
    with open(constraints_dir / "eligible.json", "w") as f:
        f.write(dumps(jobs))


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)


# ── 1. Flat batching ─────────────────────────────────────────────────────

def test_evaluate_prepare_writes_batched_input_files(tmp_path, monkeypatch):
    """326-style volume: eval_batch_size=2 forces 3 batches for 5 jobs,
    proving _input_N.json is written per-batch (like Gate), never as one
    unbatched _input.json."""
    _setup(tmp_path, monkeypatch)
    cfg = _cfg(eval_batch_size=2)
    date = "2026-08-12"
    job_ids = [f"job-{i}" for i in range(5)]
    _seed(cfg, date, job_ids)

    _evaluate_prepare(cfg, date)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    assert not (stage_dir / "_input.json").exists(), "old unbatched shape must be gone"
    batch_files = sorted(stage_dir.glob("_input_*.json"))
    assert [p.name for p in batch_files] == ["_input_0.json", "_input_1.json", "_input_2.json"]

    total = 0
    for p in batch_files:
        with open(p) as f:
            batch = json.load(f)
        assert len(batch) <= 2
        total += len(batch)
    assert total == 5


def test_evaluate_prepare_single_batch_when_under_batch_size(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cfg = _cfg(eval_batch_size=50)
    date = "2026-08-12"
    _seed(cfg, date, ["only-job"])

    _evaluate_prepare(cfg, date)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    assert (stage_dir / "_input_0.json").exists()
    assert not (stage_dir / "_input_1.json").exists()


# ── 2. No nested agent spawning (instruction-level) ─────────────────────

def test_eval_prompt_no_longer_invites_sub_agent_nesting():
    """The exact line that caused the real 2-level nested fanout ('split
    across sub-agents instead if needed') must be gone from eval_v2.md,
    and the prompt must explicitly say not to spawn further sub-agents."""
    text = Path("prompts/eval_v2.md").read_text()
    assert "split across sub-agents instead if needed" not in text
    assert "do not spawn further sub-agents" in text.lower() or "not spawn further sub-agents" in text.lower()


def test_evaluate_prepare_instructions_tell_agent_to_handle_batch_directly(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    cfg = _cfg()
    date = "2026-08-12"
    _seed(cfg, date, ["job-1"])

    _evaluate_prepare(cfg, date)

    out = capsys.readouterr().out
    assert "_input_N.json" in out
    assert "do not spawn further sub-agents" in out.lower()


# ── 3. Batch retry / checkpoint behavior ─────────────────────────────────

def test_prepare_rerun_skips_jobs_that_already_have_output(tmp_path, monkeypatch):
    """Simulates a batch that already wrote its output (a completed agent
    call, or a survivor from an earlier partial attempt this date) --
    re-running --prepare must never re-send it."""
    _setup(tmp_path, monkeypatch)
    cfg = _cfg()
    date = "2026-08-12"
    _seed(cfg, date, ["done-1", "missing-1"])

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    stage_dir.mkdir(parents=True, exist_ok=True)
    # done-1 already has output on disk, as if a prior --prepare/agent pass
    # this same date already finished it.
    with open(stage_dir / "done-1.json", "w") as f:
        f.write(dumps({
            "id": "done-1", "score": 3.0, "confidence": 0.8, "recommendation": "skip",
            "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
            "company_summary": "s", "fit_paragraph": "f",
            "rubric": {"role_fit": 3, "seniority_fit": 3, "skills_match": 3, "domain": 3, "logistics": 3},
            "prompt_version": "v2", "profile_version": 1, "job_hash": "irrelevant",
        }))

    _evaluate_prepare(cfg, date)

    batch_files = sorted(stage_dir.glob("_input_*.json"))
    assert len(batch_files) == 1
    with open(batch_files[0]) as f:
        batch = json.load(f)
    ids_in_batch = {e["job"]["id"] for e in batch}
    assert ids_in_batch == {"missing-1"}, "already-done job must not be re-batched"


def test_prepare_rerun_clears_stale_batch_files(tmp_path, monkeypatch):
    """A leftover _input_N.json from an earlier --prepare call this date
    (e.g. batch 1 of 2, now stale since batch 0 already finished and only
    a smaller retry set remains) must not accumulate across --prepare
    calls -- it would re-list already-done ids in --finalize's
    expected_ids for no reason."""
    _setup(tmp_path, monkeypatch)
    cfg = _cfg(eval_batch_size=1)
    date = "2026-08-12"
    _seed(cfg, date, ["a", "b"])

    _evaluate_prepare(cfg, date)  # first pass: writes _input_0.json (a), _input_1.json (b)
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    assert len(list(stage_dir.glob("_input_*.json"))) == 2

    # Simulate job "a" finishing, job "b" never getting an agent call.
    with open(stage_dir / "a.json", "w") as f:
        f.write(dumps({
            "id": "a", "score": 3.0, "confidence": 0.8, "recommendation": "skip",
            "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
            "company_summary": "s", "fit_paragraph": "f",
            "rubric": {"role_fit": 3, "seniority_fit": 3, "skills_match": 3, "domain": 3, "logistics": 3},
            "prompt_version": "v2", "profile_version": 1, "job_hash": "h",
        }))

    _evaluate_prepare(cfg, date)  # retry pass

    batch_files = sorted(stage_dir.glob("_input_*.json"))
    assert len(batch_files) == 1, "stale leftover batch file(s) must be cleared, not accumulated"
    with open(batch_files[0]) as f:
        batch = json.load(f)
    assert {e["job"]["id"] for e in batch} == {"b"}


# ── 4. HTML stripping ─────────────────────────────────────────────────────

def test_strip_html_removes_tags_and_unescapes_entities():
    raw = "<p>Own the <b>roadmap</b> &amp; ship &nbsp;fast.</p>"
    assert strip_html(raw) == "Own the roadmap & ship fast."


def test_strip_html_noop_on_plain_text():
    assert strip_html("Just plain text, no markup.") == "Just plain text, no markup."


def test_strip_html_empty_and_none_safe():
    assert strip_html("") == ""
    assert strip_html(None) is None


def test_evaluate_input_job_strips_description_only():
    job = {"id": "j1", "title": "PM", "description": "<p>Ship <b>fast</b></p>"}
    out = _evaluate_input_job(job)
    assert out["description"] == "Ship fast"
    assert out["id"] == "j1" and out["title"] == "PM"
    # Original dict is untouched (non-mutating copy, same convention as
    # gate's own _gate_input_job).
    assert job["description"] == "<p>Ship <b>fast</b></p>"


def test_evaluate_prepare_batch_file_has_stripped_description_cache_key_unaffected(tmp_path, monkeypatch):
    """The AGENT sees stripped text; the cache key (job_hash) must still be
    computed from the ORIGINAL (HTML-including) description, so this change
    can never bust the eval cache."""
    _setup(tmp_path, monkeypatch)
    cfg = _cfg()
    date = "2026-08-12"
    raw_description = "<p>Own the <b>roadmap</b>.</p>"
    _seed(cfg, date, ["job-1"], descriptions={"job-1": raw_description})

    _evaluate_prepare(cfg, date)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    with open(stage_dir / "_input_0.json") as f:
        batch = json.load(f)
    entry = batch[0]
    assert entry["job"]["description"] == "Own the roadmap ."
    expected_hash = Job.from_dict({
        "id": "job-1", "source": "fantastic-jobs", "title": "Product Manager", "company": "Acme",
        "apply_url": "https://x/job-1", "location": "India", "remote": True,
        "description": raw_description,
    }).content_hash()
    assert entry["job_hash"] == expected_hash


# ── 5. Structured-location pre-filter ────────────────────────────────────

def _sponsorship_profile(**overrides):
    location = {"remote": "preferred", "onsite_ok": ["Mumbai"], "visa_sponsorship_required": True}
    location.update(overrides)
    return make_profile(location=location)


def test_region_restricted_remote_dash_form_rejects():
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Remote-EMEA", description="")
    result = evaluate_constraints(job, profile, FX_RATES)
    assert not result.passed
    assert "region" in result.reasons[0]


def test_region_restricted_remote_suffix_form_rejects():
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Latin America - Remote", description="")
    assert not evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_colon_form_rejects():
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Remote: United States", description="")
    assert not evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_india_mentioned_never_rejects():
    """India named anywhere in the location string means the restriction
    doesn't exclude this candidate -- must never reject."""
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Remote within India, Canada or US", description="")
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_worldwide_never_rejects():
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Remote - Worldwide", description="")
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_plain_remote_never_rejects():
    profile = _sponsorship_profile()
    job = make_job(remote=True, location="Remote", description="")
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_does_not_fire_without_sponsorship_requirement():
    """Same exclusionary location string, but the candidate doesn't need
    sponsorship -- must never reject, same gating as the existing
    work-authorization rule."""
    profile = make_profile(location={"remote": "preferred", "onsite_ok": ["Mumbai"]})
    job = make_job(remote=True, location="Remote-EMEA", description="")
    assert evaluate_constraints(job, profile, FX_RATES).passed


def test_region_restricted_remote_never_reads_jd_body():
    """Deliberately narrow per the audit: a region restriction stated only
    in the JD BODY (not the structured location field) must NOT be caught
    here -- that class of case is reserved for the AI Gate/Evaluate
    reasoning stages, not a regex."""
    profile = _sponsorship_profile()
    job = make_job(
        remote=True, location="Remote",
        description="This role is only open to candidates based in the EMEA region.",
    )
    assert evaluate_constraints(job, profile, FX_RATES).passed
    assert _region_restricted_remote(job) is None


def test_region_restricted_remote_onsite_job_unaffected():
    profile = _sponsorship_profile()
    job = make_job(remote=False, location="Mumbai, India", description="")
    assert evaluate_constraints(job, profile, FX_RATES).passed


# ── 6. Evaluate schema/rubric untouched ──────────────────────────────────

def test_eval_schema_required_fields_and_rubric_shape_unchanged():
    """Trip-wire: this token-optimization pass must not have touched
    schemas/eval.schema.json's contract. Snapshotted from the file as it
    existed before this pass."""
    with open("schemas/eval.schema.json") as f:
        schema = json.load(f)
    assert schema["required"] == [
        "id", "score", "confidence", "recommendation",
        "strengths", "weaknesses", "ats_keywords",
        "company_summary", "fit_paragraph", "rubric",
        "prompt_version", "profile_version", "job_hash",
    ]
    assert schema["properties"]["strengths"]["minItems"] == 3
    assert schema["properties"]["strengths"]["maxItems"] == 3
    assert schema["properties"]["weaknesses"]["minItems"] == 2
    assert schema["properties"]["weaknesses"]["maxItems"] == 2
    assert schema["properties"]["recommendation"]["enum"] == ["apply", "skip"]


def test_eval_prompt_rubric_weights_unchanged():
    """The rubric table's weights (role_fit .30, seniority_fit .20,
    skills_match .25, domain .15, logistics .10) must still be present
    verbatim -- this pass only touched the input/dispatch instructions at
    the top of the file, never the rubric itself."""
    text = Path("prompts/eval_v2.md").read_text()
    for weight_line in ("| 0.30 |", "| 0.20 |", "| 0.25 |", "| 0.15 |", "| 0.10 |"):
        assert weight_line in text
    assert "company_summary" in text and "fit_paragraph" in text
    assert "exactly 3" in text.lower() or "**exactly 3**" in text
    assert "exactly 2" in text.lower() or "**exactly 2**" in text
