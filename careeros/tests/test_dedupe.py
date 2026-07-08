"""Tests for careeros/pipeline/dedupe.py — in-run, cross-location, vs-history,
and vs-sheet dedup passes."""

from __future__ import annotations

from careeros.pipeline.dedupe import (
    append_seen_ids, dedupe_against_history, dedupe_against_sheet_ids,
    dedupe_cross_location, dedupe_in_run, load_seen_ids,
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


# ── cross-location (P2.6): same role, multiple countries -> keep one ────

def test_dedupe_cross_location_collapses_same_role_multi_country():
    """The real pattern found in the P2.6 benchmark: Appfire posted the same
    'Senior Product Manager - AI Governance' role separately for Poland,
    Bulgaria, and Spain — same company/title/description, different
    locations/ids. Only the first (highest work-mode-tier) survives."""
    desc = "At Appfire, we believe great work happens when people choose how they work. " * 3
    jobs = [
        make_job(id="a-poland", company="Appfire", title="Senior PM - AI Governance",
                  location="Poland", description=desc),
        make_job(id="a-bulgaria", company="Appfire", title="Senior PM - AI Governance",
                  location="Bulgaria", description=desc),
        make_job(id="a-spain", company="Appfire", title="Senior PM - AI Governance",
                  location="Spain", description=desc),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert [j.id for j in unique] == ["a-poland"]
    assert [j.id for j in dropped] == ["a-bulgaria", "a-spain"]


def test_dedupe_cross_location_keeps_first_in_list_order():
    """Discovery appends items in profile.work_mode_priority query order, so
    'first in list' already means 'from the highest-priority tier' — no
    separate ranking logic needed here."""
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="high-priority-tier", company="Acme", title="PM", location="India", description=desc),
        make_job(id="low-priority-tier", company="Acme", title="PM", location="Germany", description=desc),
    ]
    unique, _ = dedupe_cross_location(jobs)
    assert unique[0].id == "high-priority-tier"


def test_dedupe_cross_location_keeps_different_titles_at_same_company():
    jobs = [make_job(id="a", company="Acme", title="Product Manager"),
            make_job(id="b", company="Acme", title="Growth Product Manager")]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 2
    assert dropped == []


def test_dedupe_cross_location_keeps_same_title_different_company():
    jobs = [make_job(id="a", company="Acme", title="Product Manager"),
            make_job(id="b", company="Globex", title="Product Manager")]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 2
    assert dropped == []


def test_dedupe_cross_location_keeps_genuinely_different_reqs_same_title():
    """Two DIFFERENT simultaneous openings at the same company sharing a title
    (e.g. two 'Software Engineer' reqs) must survive if their actual JD
    content differs — description-prefix similarity, not title alone, decides."""
    jobs = [
        make_job(id="a", company="Acme", title="Software Engineer",
                  description="Backend team, owns the payments service."),
        make_job(id="b", company="Acme", title="Software Engineer",
                  description="Frontend team, owns the checkout UI."),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 2
    assert dropped == []


def test_dedupe_cross_location_is_case_and_whitespace_insensitive():
    jobs = [
        make_job(id="a", company="Appfire", title="Senior PM", description="Great role here."),
        make_job(id="b", company="APPFIRE", title="senior pm", description="Great   role  here."),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 1
    assert len(dropped) == 1


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
