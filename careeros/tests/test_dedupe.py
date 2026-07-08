"""Tests for careeros/pipeline/dedupe.py — in-run, vs-history, and vs-sheet
dedup passes."""

from __future__ import annotations

from careeros.pipeline.dedupe import (
    append_seen_ids, dedupe_against_history, dedupe_against_sheet_ids,
    dedupe_in_run, load_seen_ids,
)
from careeros.tests.conftest import make_job


def test_dedupe_in_run_drops_repeated_ids():
    jobs = [make_job(id="a"), make_job(id="b"), make_job(id="a")]
    unique, dropped = dedupe_in_run(jobs)
    assert [j.id for j in unique] == ["a", "b"]
    assert [j.id for j in dropped] == ["a"]


def test_dedupe_in_run_no_duplicates_returns_all():
    jobs = [make_job(id="a"), make_job(id="b")]
    unique, dropped = dedupe_in_run(jobs)
    assert len(unique) == 2
    assert dropped == []


def test_load_seen_ids_missing_file_returns_empty_set(tmp_path):
    assert load_seen_ids(tmp_path / "seen.jsonl") == set()


def test_append_then_load_seen_ids_roundtrips(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    jobs = [make_job(id="a"), make_job(id="b")]
    append_seen_ids(seen_path, jobs, "2026-07-08")
    assert load_seen_ids(seen_path) == {"a", "b"}


def test_append_seen_ids_is_additive_across_calls(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    append_seen_ids(seen_path, [make_job(id="a")], "2026-07-08")
    append_seen_ids(seen_path, [make_job(id="b")], "2026-07-09")
    assert load_seen_ids(seen_path) == {"a", "b"}


def test_dedupe_against_history_drops_previously_seen(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    append_seen_ids(seen_path, [make_job(id="a")], "2026-07-07")
    jobs = [make_job(id="a"), make_job(id="b")]
    unique, dropped = dedupe_against_history(jobs, seen_path)
    assert [j.id for j in unique] == ["b"]
    assert [j.id for j in dropped] == ["a"]


def test_dedupe_against_history_is_read_only(tmp_path):
    """Calling it twice (e.g. a dry-run) must not mutate seen.jsonl itself —
    only cli.py's explicit append_seen_ids call after a real run does that."""
    seen_path = tmp_path / "seen.jsonl"
    append_seen_ids(seen_path, [make_job(id="a")], "2026-07-07")
    dedupe_against_history([make_job(id="b")], seen_path)
    dedupe_against_history([make_job(id="b")], seen_path)
    assert load_seen_ids(seen_path) == {"a"}


def test_dedupe_against_sheet_ids_drops_matching_ids():
    jobs = [make_job(id="a"), make_job(id="b")]
    unique, dropped = dedupe_against_sheet_ids(jobs, {"a"})
    assert [j.id for j in unique] == ["b"]
    assert [j.id for j in dropped] == ["a"]


def test_dedupe_against_sheet_ids_empty_set_keeps_all():
    jobs = [make_job(id="a"), make_job(id="b")]
    unique, dropped = dedupe_against_sheet_ids(jobs, set())
    assert len(unique) == 2
    assert dropped == []
