"""Tests for careeros/runmeta.py — run.json's manifest bookkeeping, including
the estimated_tokens accounting added to make the "least AI cost" KPI
directionally visible (it was previously completely unmeasured)."""

from __future__ import annotations

from careeros import runmeta


def test_estimate_tokens_from_file_sizes(tmp_path):
    f = tmp_path / "input.json"
    f.write_text("x" * 400)  # 400 bytes -> ~100 tokens at 4 chars/token
    assert runmeta.estimate_tokens(f) == 100


def test_estimate_tokens_sums_multiple_files(tmp_path):
    f1 = tmp_path / "a.json"
    f2 = tmp_path / "b.json"
    f1.write_text("x" * 400)
    f2.write_text("y" * 400)
    assert runmeta.estimate_tokens(f1, f2) == 200


def test_estimate_tokens_ignores_missing_files(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert runmeta.estimate_tokens(missing) == 0


def test_record_stage_writes_estimated_tokens(tmp_path):
    runmeta.record_stage(tmp_path, "2026-07-08", "gate",
                          count_in=10, count_out=5, seconds=1.0, estimated_tokens=250)
    manifest = runmeta.load_manifest(tmp_path, "2026-07-08")
    assert manifest["stages"]["gate"]["estimated_tokens"] == 250


def test_record_stage_totals_sum_estimated_tokens_across_stages(tmp_path):
    runmeta.record_stage(tmp_path, "2026-07-08", "gate",
                          count_in=10, count_out=5, seconds=1.0, estimated_tokens=100)
    runmeta.record_stage(tmp_path, "2026-07-08", "evaluate",
                          count_in=5, count_out=5, seconds=2.0, estimated_tokens=300)
    manifest = runmeta.load_manifest(tmp_path, "2026-07-08")
    assert manifest["totals"]["estimated_tokens_total"] == 400


def test_record_stage_totals_funnel_tracks_count_out_per_stage(tmp_path):
    runmeta.record_stage(tmp_path, "2026-07-08", "discover", count_in=0, count_out=40, seconds=1.0)
    runmeta.record_stage(tmp_path, "2026-07-08", "constraints", count_in=40, count_out=4, seconds=0.1)
    manifest = runmeta.load_manifest(tmp_path, "2026-07-08")
    assert manifest["totals"]["discovered"] == 40
    assert manifest["totals"]["eligible"] == 4


def test_load_manifest_missing_file_returns_default_shape(tmp_path):
    manifest = runmeta.load_manifest(tmp_path, "2026-07-08")
    assert manifest == {"date": "2026-07-08", "stages": {}, "prompt_versions": {}, "cache": {}}


def test_validate_stage_catches_schema_violation():
    errors = runmeta.validate_stage("eval", [{"id": "job-1"}])  # missing required fields
    assert errors  # at least one error for the missing required properties


def test_validate_stage_passes_valid_record():
    valid = {
        "id": "job-1", "score": 4.2, "confidence": 0.8, "recommendation": "apply",
        "strengths": ["a", "b", "c"], "weaknesses": ["x", "y"], "ats_keywords": [],
        "company_summary": "s", "fit_paragraph": "f",
        "rubric": {"role_fit": 4, "seniority_fit": 4, "skills_match": 4, "domain": 4, "logistics": 4},
        "prompt_version": "v2", "profile_version": 1, "job_hash": "h",
    }
    assert runmeta.validate_stage("eval", [valid]) == []
