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
    only cli/pipeline.py's explicit append_seen_ids call after a real run
    does that."""
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


# ── processed.jsonl (v2.0): TTL-aware history, generic terminal records ──

def test_load_processed_ids_reads_legacy_first_seen_key(tmp_path):
    """A pre-v2.0 seen.jsonl (id + first_seen, no date/reason/score) must
    still be readable with zero migration step."""
    from careeros.pipeline.dedupe import append_seen_ids, load_processed_ids
    path = tmp_path / "seen.jsonl"
    append_seen_ids(path, [make_job(id="legacy-a")], "2026-07-01")
    assert load_processed_ids(path) == {"legacy-a"}


def test_load_processed_ids_no_ttl_never_expires(tmp_path):
    from careeros.pipeline.dedupe import append_processed, load_processed_ids
    path = tmp_path / "processed.jsonl"
    append_processed(path, [{"id": "old-job", "date": "2020-01-01", "terminal_stage": "gate", "reason": "role-mismatch", "score": None}])
    assert load_processed_ids(path) == {"old-job"}
    assert load_processed_ids(path, ttl_days=30, as_of=None) == {"old-job"}  # as_of missing -> no TTL applied


def test_load_processed_ids_ttl_expires_old_entries(tmp_path):
    from careeros.pipeline.dedupe import append_processed, load_processed_ids
    path = tmp_path / "processed.jsonl"
    append_processed(path, [
        {"id": "stale", "date": "2026-06-01", "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "fresh", "date": "2026-07-25", "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
    ])
    ids = load_processed_ids(path, ttl_days=30, as_of="2026-07-31")
    assert ids == {"fresh"}, "an entry older than the TTL must be allowed to resurface"


def test_load_processed_ids_keeps_unparseable_dates_forever(tmp_path):
    """A qa-* run label isn't an ISO date -- conservatively never expire it
    rather than guessing."""
    from careeros.pipeline.dedupe import append_processed, load_processed_ids
    path = tmp_path / "processed.jsonl"
    append_processed(path, [{"id": "qa-job", "date": "qa-hardening-02", "terminal_stage": "gate", "reason": None, "score": None}])
    ids = load_processed_ids(path, ttl_days=30, as_of="2099-01-01")
    assert ids == {"qa-job"}


def test_dedupe_against_history_respects_ttl(tmp_path):
    from careeros.pipeline.dedupe import append_processed, dedupe_against_history
    path = tmp_path / "processed.jsonl"
    append_processed(path, [{"id": "stale", "date": "2026-06-01", "terminal_stage": "evaluate", "reason": None, "score": 2.0}])
    jobs = [make_job(id="stale"), make_job(id="new")]
    unique, dropped = dedupe_against_history(jobs, path, ttl_days=30, as_of="2026-07-31")
    assert [j.id for j in unique] == ["stale", "new"]
    assert dropped == []


def test_append_processed_records_are_additive(tmp_path):
    from careeros.pipeline.dedupe import append_processed, load_processed_ids
    path = tmp_path / "processed.jsonl"
    append_processed(path, [{"id": "a", "date": "2026-07-30", "terminal_stage": "constraints", "reason": "onsite", "score": None}])
    append_processed(path, [{"id": "b", "date": "2026-07-31", "terminal_stage": "select", "reason": "apply", "score": 4.3}])
    assert load_processed_ids(path) == {"a", "b"}


# ── tier provenance union on collapse (v2.0) ─────────────────────────────

def test_dedupe_in_run_unions_tiers_across_collapsed_duplicate():
    from careeros.models import Job
    a1 = Job(id="a", source="fantastic-jobs", title="PM", company="Acme",
             apply_url="https://x", tiers=["global_remote"])
    a2 = Job(id="a", source="fantastic-jobs", title="PM", company="Acme",
             apply_url="https://x", tiers=["india_remote"])
    unique, dropped = dedupe_in_run([a1, a2])
    assert len(unique) == 1
    assert unique[0].tiers == ["global_remote", "india_remote"]


def test_dedupe_cross_location_unions_tiers_across_collapsed_duplicate():
    from careeros.models import Job
    desc = "Same role description text repeated for length. " * 5
    a = Job(id="a-poland", source="fantastic-jobs", title="Senior PM", company="Appfire",
            apply_url="https://x", location="Poland", description=desc, tiers=["global_remote"])
    b = Job(id="a-bulgaria", source="fantastic-jobs", title="Senior PM", company="Appfire",
            apply_url="https://y", location="Bulgaria", description=desc, tiers=["india_remote"])
    unique, dropped = dedupe_cross_location([a, b])
    assert len(unique) == 1
    assert unique[0].tiers == ["global_remote", "india_remote"]


def test_dedupe_cross_location_on_duplicate_fires_with_survivor_and_dropped():
    """The seam `careeros/cli/pipeline.py`'s `dedupe` command uses to record
    which SOURCE survived a cross-location collapse (for `pipeline/ledger.py`'s
    cross-layer duplicate attribution) -- fires exactly once per collapse,
    with (survivor, dropped) in that order, never on a non-duplicate."""
    from careeros.models import Job
    desc = "Same role description text repeated for length. " * 5
    layer1 = Job(id="l1", source="ats-dataset", title="Product Manager, Growth",
                 company="Sarvam AI", apply_url="https://x", description=desc)
    layer2a = Job(id="l2a", source="ats-watchlist", title="Product Manager, Growth",
                  company="Sarvam AI", apply_url="https://y", description=desc)
    unrelated = Job(id="u", source="ats-dataset", title="Data Scientist",
                     company="Sarvam AI", apply_url="https://z", description="Different role entirely.")

    calls: list[tuple[str, str]] = []
    unique, dropped = dedupe_cross_location(
        [layer1, layer2a, unrelated],
        on_duplicate=lambda survivor, dup: calls.append((survivor.id, dup.id)),
    )

    assert calls == [("l1", "l2a")]  # fired once, survivor first
    assert [j.id for j in unique] == ["l1", "u"]
    assert [j.id for j in dropped] == ["l2a"]


def test_dedupe_cross_location_on_duplicate_none_is_backward_compatible():
    """Every existing caller/test omits on_duplicate entirely -- confirms
    the default keeps dedupe_cross_location's behavior byte-for-byte
    unchanged from before the callback existed."""
    from careeros.models import Job
    desc = "Same role description text repeated for length. " * 5
    a = Job(id="a", source="fantastic-jobs", title="PM", company="Acme", apply_url="https://x", description=desc)
    b = Job(id="b", source="fantastic-jobs", title="PM", company="Acme", apply_url="https://y", description=desc)
    unique, dropped = dedupe_cross_location([a, b])  # no on_duplicate passed
    assert [j.id for j in unique] == ["a"]
    assert [j.id for j in dropped] == ["b"]


# ── prefer_key (v2.2): survivor choice can override first-occurrence ─────
# Real bug this fixes: 2 `India`-located postings collapsed into a
# `Sydney, Australia` twin under first-occurrence-wins alone, because
# ats-dataset has no india_remote tier to guarantee India-first ordering.

def test_dedupe_cross_location_prefer_key_none_keeps_first_occurrence():
    """Default (prefer_key omitted) is unchanged: first occurrence wins even
    when a later, differently-scored duplicate exists."""
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="sydney", company="Acme", title="PM", location="Sydney, Australia", description=desc),
        make_job(id="india", company="Acme", title="PM", location="India", description=desc),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert [j.id for j in unique] == ["sydney"]


def test_dedupe_cross_location_prefer_key_promotes_strictly_higher_scored_copy():
    """The actual fix: an India-preference prefer_key makes the LATER
    (India) copy survive over the FIRST (Sydney) one."""
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="sydney", company="Acme", title="PM", location="Sydney, Australia", description=desc),
        make_job(id="india", company="Acme", title="PM", location="India", description=desc),
    ]
    prefer = lambda j: 1 if "india" in (j.location or "").lower() else 0
    unique, dropped = dedupe_cross_location(jobs, prefer_key=prefer)
    assert [j.id for j in unique] == ["india"]
    assert [j.id for j in dropped] == ["sydney"]


def test_dedupe_cross_location_prefer_key_tie_keeps_first_occurrence():
    """A tie (both score 0, or both score equally) must NOT flip the
    survivor -- only a STRICTLY higher prefer_key promotes the later copy."""
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="first", company="Acme", title="PM", location="Germany", description=desc),
        make_job(id="second", company="Acme", title="PM", location="France", description=desc),
    ]
    prefer = lambda j: 1 if "india" in (j.location or "").lower() else 0  # neither matches
    unique, dropped = dedupe_cross_location(jobs, prefer_key=prefer)
    assert [j.id for j in unique] == ["first"]


def test_dedupe_cross_location_prefer_key_on_duplicate_fires_survivor_first():
    """on_duplicate must still fire with (survivor, dropped) in that order
    even when prefer_key promotes the SECOND job to survivor."""
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="sydney", company="Acme", title="PM", location="Sydney, Australia", description=desc),
        make_job(id="india", company="Acme", title="PM", location="India", description=desc),
    ]
    prefer = lambda j: 1 if "india" in (j.location or "").lower() else 0
    calls = []
    dedupe_cross_location(jobs, on_duplicate=lambda s, d: calls.append((s.id, d.id)), prefer_key=prefer)
    assert calls == [("india", "sydney")]


def test_dedupe_cross_location_prefer_key_unions_tiers_regardless_of_survivor():
    desc = "Same role description text repeated for length. " * 5
    jobs = [
        make_job(id="sydney", company="Acme", title="PM", location="Sydney, Australia",
                  description=desc, tiers=["global_remote"]),
        make_job(id="india", company="Acme", title="PM", location="India",
                  description=desc, tiers=["default"]),
    ]
    prefer = lambda j: 1 if "india" in (j.location or "").lower() else 0
    unique, _ = dedupe_cross_location(jobs, prefer_key=prefer)
    assert unique[0].id == "india"
    assert unique[0].tiers == ["default", "global_remote"]


# ── HTML/entity normalization before the cross-location key (v2.2) ───────
# Real bug this fixes: the same Airwallex role from ats-dataset (markdown)
# and ats-watchlist (HTML) never collapsed -- their raw first-300-char
# prefixes differed even though the underlying prose was identical.

def test_dedupe_cross_location_collapses_html_vs_markdown_duplicate():
    html_desc = "<p>Airwallex is the only unified payments platform for global businesses.</p>" * 4
    md_desc = "Airwallex is the only unified payments platform for global businesses. " * 4
    jobs = [
        make_job(id="from-dataset", source="ats-dataset", company="airwallex",
                  title="Senior Product Manager, Transaction Monitoring", description=md_desc),
        make_job(id="from-watchlist", source="ats-watchlist", company="airwallex",
                  title="Senior Product Manager, Transaction Monitoring", description=html_desc),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 1
    assert len(dropped) == 1


def test_dedupe_cross_location_html_normalization_unescapes_entities():
    a = "Sales &amp; Marketing team &mdash; we&#39;re growing fast. " * 4
    b = "Sales & Marketing team — we're growing fast. " * 4
    jobs = [
        make_job(id="a", company="Acme", title="PM", description=a),
        make_job(id="b", company="Acme", title="PM", description=b),
    ]
    unique, dropped = dedupe_cross_location(jobs)
    assert len(unique) == 1


def test_dedupe_in_run_tiers_none_when_neither_job_has_tiers():
    from careeros.models import Job
    a1 = Job(id="a", source="fantastic-jobs", title="PM", company="Acme", apply_url="https://x")
    a2 = Job(id="a", source="fantastic-jobs", title="PM", company="Acme", apply_url="https://x")
    unique, _ = dedupe_in_run([a1, a2])
    assert unique[0].tiers is None
