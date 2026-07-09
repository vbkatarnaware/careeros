"""Regression test for a real production bug found 2026-07-10: the eval
cache key is content-based (job_hash excludes `source`), so a cache hit can
carry a STALE `id` from whenever that content was first evaluated under a
different Job.id (e.g. before the actor->REST provider migration, since
`source` feeds Job.id but not content_hash). `_evaluate_prepare` must
overwrite the cached `id` with TODAY's job_id, or every downstream stage
(threshold/artifacts/drive/sheets/summary) fails to find the matching Job —
which is exactly what happened live (a Motive PM cache hit silently
displaced that day's own fresh evaluation of the same job)."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.cache import Cache, eval_cache_key
from careeros.cli import _evaluate_prepare
from careeros.config import Config
from careeros.models import Job, dumps

_VALID_PROFILE = (
    "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
)


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={"eval": "v2"},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _minimal_eval(eval_id: str, job_hash: str) -> dict:
    return {
        "id": eval_id, "score": 4.3, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4, "seniority_fit": 4, "skills_match": 4, "domain": 4, "logistics": 4},
        "prompt_version": "v2", "profile_version": 1, "job_hash": job_hash,
    }


def test_cache_hit_id_is_overwritten_to_todays_job_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()

    today_job = dict(
        id="today-id-123", source="fantastic-jobs", title="Product Manager",
        company="Motive", apply_url="https://example.com/motive/pm",
        location="India", remote=True, seniority=None, employment_type="full_time",
        description="Fleet compliance product manager role.",
    )
    job_hash = Job.from_dict(today_job).content_hash()

    # A stale cache entry from a PRIOR run, under a DIFFERENT id (e.g. the
    # legacy actor provider), for the SAME content -> same job_hash.
    stale_eval = _minimal_eval("stale-id-from-old-run", job_hash)
    cache = Cache(cfg.cache_dir)
    key = eval_cache_key(job_hash, 1, "v2")
    cache.put("evaluate", key, stale_eval)

    date = "2026-07-10"
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([{"id": "today-id-123", "keep": True, "reason": "role-match", "confidence": 0.8}]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        f.write(dumps([today_job]))

    _evaluate_prepare(cfg, date)

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "today-id-123.json"
    assert out_path.exists()
    with open(out_path) as f:
        written = json.load(f)
    assert written["id"] == "today-id-123"
    assert written["score"] == 4.3  # cached scoring content is still reused
    assert written["job_hash"] == job_hash


def test_cache_miss_still_goes_to_input_json(tmp_path, monkeypatch):
    """Sanity check the fix doesn't affect the no-cache-hit path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "profile.yaml").write_text(_VALID_PROFILE)
    cfg = _cfg()

    job = dict(
        id="fresh-id-456", source="fantastic-jobs", title="Product Manager",
        company="NewCo", apply_url="https://example.com/newco/pm",
        location="India", remote=True, seniority=None, employment_type="full_time",
        description="Brand new posting never evaluated before.",
    )
    date = "2026-07-10"
    gate_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    with open(gate_dir / "gated.json", "w") as f:
        f.write(dumps([{"id": "fresh-id-456", "keep": True, "reason": "role-match", "confidence": 0.8}]))
    constraints_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(constraints_dir / "eligible.json", "w") as f:
        f.write(dumps([job]))

    _evaluate_prepare(cfg, date)

    input_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / "_input.json"
    assert input_path.exists()
    with open(input_path) as f:
        to_evaluate = json.load(f)
    assert len(to_evaluate) == 1
    assert to_evaluate[0]["job"]["id"] == "fresh-id-456"
