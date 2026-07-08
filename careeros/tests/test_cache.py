"""Tests for careeros/cache.py — the content-addressed cache keys are what
make "re-running daily costs zero AI calls" actually true. If a key stopped
being sensitive to one of its inputs, stale content would silently get
served forever."""

from __future__ import annotations

from careeros.cache import Cache, artifact_cache_key, eval_cache_key


def test_eval_cache_key_deterministic():
    a = eval_cache_key("hash1", 1, "v2")
    b = eval_cache_key("hash1", 1, "v2")
    assert a == b


def test_eval_cache_key_sensitive_to_job_hash():
    a = eval_cache_key("hash1", 1, "v2")
    b = eval_cache_key("hash2", 1, "v2")
    assert a != b


def test_eval_cache_key_sensitive_to_profile_version():
    a = eval_cache_key("hash1", 1, "v2")
    b = eval_cache_key("hash1", 2, "v2")
    assert a != b


def test_eval_cache_key_sensitive_to_prompt_version():
    """This is the mechanism that makes bumping eval_v1 -> eval_v2 auto-
    invalidate the cache with no extra bookkeeping."""
    a = eval_cache_key("hash1", 1, "v1")
    b = eval_cache_key("hash1", 1, "v2")
    assert a != b


def test_artifact_cache_key_sensitive_to_eval_score():
    """Resumes/covers must regenerate if the job's score moves (e.g. after a
    rubric change) since they were tailored to the old score's strengths."""
    a = artifact_cache_key("hash1", 1, 4.0, "v1")
    b = artifact_cache_key("hash1", 1, 4.5, "v1")
    assert a != b


def test_artifact_cache_key_rounds_score_to_one_decimal():
    """4.01 and 4.04 both round-format to '4.0' in the key — this is
    intentional (avoids float-precision cache misses), not a bug."""
    a = artifact_cache_key("hash1", 1, 4.01, "v1")
    b = artifact_cache_key("hash1", 1, 4.04, "v1")
    assert a == b


def test_cache_get_returns_none_for_missing_key(tmp_path):
    cache = Cache(tmp_path)
    assert cache.get("evaluate", "nonexistent") is None


def test_cache_put_then_get_roundtrips(tmp_path):
    cache = Cache(tmp_path)
    value = {"score": 4.2, "recommendation": "apply"}
    cache.put("evaluate", "key1", value)
    assert cache.get("evaluate", "key1") == value


def test_cache_stats_counts_entries_per_stage(tmp_path):
    cache = Cache(tmp_path)
    cache.put("evaluate", "k1", {"a": 1})
    cache.put("evaluate", "k2", {"a": 2})
    cache.put("resume", "k3", {"content": "..."})
    stats = cache.stats()
    assert stats["evaluate"] == 2
    assert stats["resume"] == 1


def test_cache_stats_empty_dir_returns_empty_dict(tmp_path):
    cache = Cache(tmp_path / "does-not-exist")
    assert cache.stats() == {}
