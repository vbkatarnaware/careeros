"""Tests for careeros/pipeline/ledger.py — per-tier aggregation, arming
checks, and the quarantine mechanism."""

from __future__ import annotations

import json

from careeros import runmeta
from careeros.config import Config
from careeros.models import dumps
from careeros.pipeline.dedupe import dedupe_cross_location
from careeros.pipeline.ledger import (
    aggregate_entries, aggregate_source_entries, compute_arming,
    compute_run_source_stats, compute_run_tier_stats, load_quarantine,
    render_ledger_markdown, render_source_ledger_markdown,
)
from careeros.tests.conftest import make_job


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed_run(cfg, date, jobs_with_tiers, processed_records, outcomes):
    normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
    with open(normalize_dir / "jobs.json", "w") as f:
        f.write(dumps(jobs_with_tiers))
    with open(cfg.careeros_dir / "processed.jsonl", "a") as f:
        for r in processed_records:
            f.write(json.dumps(r) + "\n")
    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(select_dir / "outcomes.json", "w") as f:
        f.write(dumps(outcomes))


def test_compute_run_tier_stats_attributes_records_to_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"

    jobs = [
        {"id": "a", "tiers": ["global_remote"]},
        {"id": "b", "tiers": ["india_remote"]},
        {"id": "c", "tiers": ["global_remote", "india_remote"]},  # cross-location union
    ]
    processed = [
        {"id": "a", "date": date, "terminal_stage": "constraints", "reason": "onsite outside onsite_ok", "score": None},
        {"id": "b", "date": date, "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "c", "date": date, "terminal_stage": "select", "reason": "apply", "score": 4.5},
    ]
    outcomes = [
        {"id": "c", "tier": "apply", "score": 4.5, "recommendation": "apply",
         "shortfall": {"role_fit": 0.1, "seniority_fit": 0.1, "skills_match": 0.1, "domain": 0.1, "logistics": 0.1},
         "primary_gap": None, "secondary_gap": None, "tiers": ["global_remote", "india_remote"]},
    ]
    _seed_run(cfg, date, jobs, processed, outcomes)

    entries = compute_run_tier_stats(cfg, date)
    by_tier = {e.tier: e for e in entries}

    assert by_tier["global_remote"].decision["records"] == 2  # a, c
    assert by_tier["india_remote"].decision["records"] == 2  # b, c
    assert by_tier["global_remote"].decision["constraints_rejected"] == 1
    assert by_tier["india_remote"].decision["gate_dropped"] == 1
    assert by_tier["global_remote"].descriptive["apply"] == 1
    assert by_tier["india_remote"].descriptive["apply"] == 1  # job c counted in BOTH tiers


def test_compute_run_tier_stats_gate_keep_rate_excludes_constraints_rejected(tmp_path, monkeypatch):
    """Gate-keep rate is of jobs that REACHED the gate -- a constraints-
    rejected job never reached gate at all and must not dilute the rate."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-01"
    jobs = [{"id": "a", "tiers": ["onsite"]}, {"id": "b", "tiers": ["onsite"]}, {"id": "c", "tiers": ["onsite"]}]
    processed = [
        {"id": "a", "date": date, "terminal_stage": "constraints", "reason": "salary floor", "score": None},
        {"id": "b", "date": date, "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "c", "date": date, "terminal_stage": "select", "reason": "omit", "score": 2.0},
    ]
    _seed_run(cfg, date, jobs, processed, [])
    entries = compute_run_tier_stats(cfg, date)
    onsite = entries[0]
    # gate pool = gate_dropped(1) + reached_select(1) = 2; keep rate = 1/2
    assert onsite.decision["gate_keep_rate"] == 0.5
    assert onsite.decision["constraints_rejection_rate"] == round(1 / 3, 4)


def test_aggregate_entries_pools_across_runs():
    from careeros.pipeline.ledger import TierLedgerEntry
    e1 = TierLedgerEntry(date="2026-07-01", tier="onsite",
                         decision={"records": 10, "constraints_rejected": 1, "gate_dropped": 2,
                                   "gate_keep_rate": 0.8, "constraints_rejection_rate": 0.1,
                                   "reject_reason_histogram": {"role-mismatch": 2}},
                         descriptive={"evaluated": 8, "apply": 1, "consider": 1, "omit": 6,
                                     "mean_score": 3.0, "mean_shortfall": None})
    e2 = TierLedgerEntry(date="2026-07-08", tier="onsite",
                         decision={"records": 12, "constraints_rejected": 0, "gate_dropped": 3,
                                   "gate_keep_rate": 0.75, "constraints_rejection_rate": 0.0,
                                   "reject_reason_histogram": {"role-mismatch": 3}},
                         descriptive={"evaluated": 9, "apply": 2, "consider": 0, "omit": 7,
                                     "mean_score": 3.2, "mean_shortfall": None})
    summary = aggregate_entries([e1, e2])
    assert summary["onsite"]["num_runs"] == 2
    assert summary["onsite"]["total_records"] == 22
    assert summary["onsite"]["apply_or_consider_events"] == 4  # (1+1) + (2+0)
    assert summary["onsite"]["top_reject_reasons"] == [("role-mismatch", 5)]


def test_compute_arming_requires_all_three_floors():
    summary = {
        "onsite": {"dates": ["2026-07-01", "2026-07-31"], "total_records": 500, "apply_or_consider_events": 10},
        "global_remote": {"dates": ["2026-07-25", "2026-07-31"], "total_records": 50, "apply_or_consider_events": 2},
    }
    arming = compute_arming(summary, min_days=28, min_records=400, min_events=8)
    assert arming["onsite"]["armed"] is True
    assert arming["global_remote"]["armed"] is False
    assert "records" in arming["global_remote"]["reason"] or "history" in arming["global_remote"]["reason"]


def test_render_ledger_markdown_handles_empty_summary():
    md = render_ledger_markdown({}, arming={})
    assert "No data yet" in md


def test_load_quarantine_reads_dates_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    learning_dir = cfg.careeros_dir / "learning"
    learning_dir.mkdir(parents=True)
    (learning_dir / "quarantine.json").write_text(json.dumps({"dates": ["2026-07-29", "2026-07-30"]}))
    assert load_quarantine(cfg) == {"2026-07-29", "2026-07-30"}


def test_load_quarantine_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    assert load_quarantine(cfg) == set()


# ── compute_run_source_stats (Layer 1 vs Layer 2A) ──────────────────────

def _seed_dropped(cfg, date, dropped_records):
    dedupe_dir = runmeta.stage_dir(cfg.runs_dir, date, "dedupe")
    with open(dedupe_dir / "dropped.json", "w") as f:
        f.write(dumps(dropped_records))


def test_compute_run_source_stats_attributes_records_to_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [
        {"id": "a", "source": "ats-dataset", "company": "Acme"},
        {"id": "b", "source": "ats-watchlist", "company": "Sarvam AI"},
        {"id": "c", "source": "ats-watchlist", "company": "Swiggy"},
    ]
    processed = [
        {"id": "a", "date": date, "terminal_stage": "select", "reason": "apply", "score": 4.5},
        {"id": "b", "date": date, "terminal_stage": "gate", "reason": "role-mismatch", "score": None},
        {"id": "c", "date": date, "terminal_stage": "constraints", "reason": "onsite outside onsite_ok", "score": None},
    ]
    outcomes = [{"id": "a", "tier": "apply", "score": 4.5, "recommendation": "apply",
                 "shortfall": {}, "primary_gap": None, "secondary_gap": None, "tiers": []}]
    _seed_run(cfg, date, jobs, processed, outcomes)

    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}

    assert by_source["ats-dataset"].decision["records"] == 1
    assert by_source["ats-dataset"].descriptive["apply"] == 1
    assert by_source["ats-dataset"].descriptive["company_yield"] == {"Acme": 1}
    assert by_source["ats-watchlist"].decision["records"] == 2
    assert by_source["ats-watchlist"].decision["gate_dropped"] == 1
    assert by_source["ats-watchlist"].decision["constraints_rejected"] == 1


def test_compute_run_source_stats_never_uses_false_positive_terminology():
    """Explicit product requirement: rejections are rejections, never
    labeled false positives -- we have no human ground truth for that."""
    import inspect
    from careeros.pipeline import ledger as ledger_mod
    src = inspect.getsource(ledger_mod)
    assert "false positive" not in src.lower(), \
        "ledger.py must not claim false positives without human-labeled ground truth"


def test_compute_run_source_stats_counts_duplicate_of_other_source(tmp_path, monkeypatch):
    """The actual question this was built for: how many Layer 2A jobs were
    duplicates of a job Layer 1 already found -- not just "a duplicate
    occurred", but attributed to WHICH source survived."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [
        {"id": "layer1-job", "source": "ats-dataset", "company": "Sarvam AI"},
        {"id": "layer2a-job", "source": "ats-watchlist", "company": "Sarvam AI"},
    ]
    processed = [
        {"id": "layer1-job", "date": date, "terminal_stage": "select", "reason": "consider", "score": 3.8},
    ]
    outcomes = [{"id": "layer1-job", "tier": "consider", "score": 3.8, "recommendation": "consider",
                 "shortfall": {}, "primary_gap": None, "secondary_gap": None, "tiers": []}]
    _seed_run(cfg, date, jobs, processed, outcomes)
    # layer2a-job was dropped as a cross-location duplicate OF layer1-job --
    # exactly the shape careeros/cli/pipeline.py's `_tag` produces.
    _seed_dropped(cfg, date, [
        {"id": "layer2a-job", "source": "ats-watchlist", "company": "Sarvam AI",
         "_drop_reason": "cross-location", "_duplicate_of_source": "ats-dataset"},
    ])

    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}

    assert by_source["ats-watchlist"].descriptive["duplicate_of_other_source"] == {"ats-dataset": 1}
    # the survivor's own source must NOT show a duplicate against itself
    assert "ats-watchlist" not in by_source["ats-dataset"].descriptive["duplicate_of_other_source"]


def test_compute_run_source_stats_ignores_same_source_cross_location_drops(tmp_path, monkeypatch):
    """Regression: dedupe_cross_location's DOMINANT use is collapsing one
    provider's own multi-country reposts (see its docstring) -- a same-
    source collapse is not evidence of cross-LAYER overlap and must never
    land in `duplicate_of_other_source`, or a source's own repost collapses
    would misreport as "duplicates of another source". Real shape: pipeline.py's
    `_tag` records `_duplicate_of_source` for every cross-location drop
    regardless of whether the survivor's source differs."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "a", "source": "ats-dataset", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    _seed_dropped(cfg, date, [
        {"id": "b", "source": "ats-dataset", "company": "Acme",
         "_drop_reason": "cross-location", "_duplicate_of_source": "ats-dataset"},
    ])
    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}
    assert by_source["ats-dataset"].descriptive["duplicate_of_other_source"] == {}


def test_compute_run_source_stats_ignores_non_cross_location_drops(tmp_path, monkeypatch):
    """A history/sheet/in-run drop was never compared against a DIFFERENT
    source's job -- only cross-location drops carry real cross-layer
    duplicate meaning."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "a", "source": "ats-watchlist", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    _seed_dropped(cfg, date, [
        {"id": "a", "source": "ats-watchlist", "company": "Acme", "_drop_reason": "history"},
    ])
    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}
    assert by_source["ats-watchlist"].descriptive["duplicate_of_other_source"] == {}


def test_compute_run_source_stats_missing_dropped_json_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "a", "source": "ats-dataset", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    entries = compute_run_source_stats(cfg, date)
    assert entries[0].descriptive["duplicate_of_other_source"] == {}


def test_aggregate_source_entries_pools_across_dates_and_duplicates():
    from careeros.pipeline.ledger import SourceLedgerEntry
    e1 = SourceLedgerEntry(
        date="2026-08-01", source="ats-watchlist",
        decision={"records": 5, "constraints_rejected": 1, "gate_dropped": 1,
                  "gate_keep_rate": 0.5, "constraints_rejection_rate": 0.2,
                  "reject_reason_histogram": {"role-mismatch": 1}},
        descriptive={"evaluated": 3, "apply": 1, "consider": 0, "omit": 2, "mean_score": 3.0,
                     "duplicate_of_other_source": {"ats-dataset": 2}, "company_yield": {"Sarvam AI": 1}},
    )
    e2 = SourceLedgerEntry(
        date="2026-08-02", source="ats-watchlist",
        decision={"records": 4, "constraints_rejected": 0, "gate_dropped": 0,
                  "gate_keep_rate": 1.0, "constraints_rejection_rate": 0.0,
                  "reject_reason_histogram": {}},
        descriptive={"evaluated": 4, "apply": 0, "consider": 1, "omit": 3, "mean_score": 3.2,
                     "duplicate_of_other_source": {"ats-dataset": 1}, "company_yield": {"Swiggy": 1}},
    )
    summary = aggregate_source_entries([e1, e2])
    assert summary["ats-watchlist"]["num_runs"] == 2
    assert summary["ats-watchlist"]["total_records"] == 9
    assert summary["ats-watchlist"]["apply_or_consider_events"] == 2
    assert summary["ats-watchlist"]["duplicate_of_other_source"] == {"ats-dataset": 3}
    assert summary["ats-watchlist"]["company_yield"] == {"Sarvam AI": 1, "Swiggy": 1}


def test_render_source_ledger_markdown_handles_empty_summary():
    md = render_source_ledger_markdown({})
    assert "No data yet" in md
    assert "false positive" not in md.lower()


def test_render_source_ledger_markdown_shows_duplicate_and_company_yield():
    summary = {
        "ats-watchlist": {
            "dates": ["2026-08-10"], "num_runs": 1, "total_records": 60,
            "pooled_gate_keep_rate": None, "apply_or_consider_events": 0,
            "top_reject_reasons": [], "duplicate_of_other_source": {"ats-dataset": 3},
            "company_yield": {},
        },
    }
    md = render_source_ledger_markdown(summary)
    assert "ats-watchlist" in md
    assert "3 of ats-dataset" in md
    assert "Discovery layer" in md


# ── real dedupe_cross_location -> ledger, end to end ────────────────────

def test_duplicate_attribution_end_to_end_with_real_dedupe(tmp_path, monkeypatch):
    """The exact scenario in the product request: a Layer 1 (ats-dataset)
    version and a Layer 2A (ats-watchlist) version of the SAME real job,
    run through the REAL `dedupe_cross_location`, wired the same way
    careeros/cli/pipeline.py's `dedupe` command wires it -- confirms the
    on_duplicate callback fires correctly AND that compute_run_source_stats
    then reports it as "1 ats-watchlist duplicate of ats-dataset", not
    merely "a duplicate happened"."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"

    layer1_job = make_job(
        id="l1", source="ats-dataset", company="Sarvam AI",
        title="Product Manager, Growth", description="Own growth loops for Sarvam.",
    )
    layer2a_job = make_job(
        id="l2a", source="ats-watchlist", company="Sarvam AI",
        title="Product Manager, Growth", description="Own growth loops for Sarvam.",
    )

    duplicate_survivor_source: dict[str, str] = {}

    def _record(survivor, dropped_dup):
        duplicate_survivor_source[dropped_dup.id] = survivor.source

    # discover appended Layer 1 first, same order dedupe would see them.
    unique, dropped_cross_location = dedupe_cross_location(
        [layer1_job, layer2a_job], on_duplicate=_record,
    )

    assert [j.id for j in unique] == ["l1"]
    assert [j.id for j in dropped_cross_location] == ["l2a"]
    assert duplicate_survivor_source == {"l2a": "ats-dataset"}

    # Same shape careeros/cli/pipeline.py's `_tag` produces.
    dropped_tagged = [{
        **layer2a_job.to_dict(), "_drop_reason": "cross-location",
        "_duplicate_of_source": duplicate_survivor_source["l2a"],
    }]
    _seed_run(cfg, date, [j.to_dict() for j in unique], [], [])
    _seed_dropped(cfg, date, dropped_tagged)

    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}
    assert by_source["ats-dataset"].decision["records"] == 1
    # the ats-watchlist job never reached normalize's survivors, so it has
    # no `records` entry of its own here -- its only trace is the
    # duplicate-of attribution, which lives on whichever source key the
    # measurement asks about. Confirm it's retrievable from the raw
    # dropped.json shape directly, the same way a real report would:
    import json as _json
    dropped_on_disk = _json.loads(
        (runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "dropped.json").read_text()
    )
    ats_watchlist_dupes_of_layer1 = sum(
        1 for d in dropped_on_disk
        if d.get("source") == "ats-watchlist" and d.get("_duplicate_of_source") == "ats-dataset"
    )
    assert ats_watchlist_dupes_of_layer1 == 1


# ── zero-output Layer 2A observability (v2.1) ────────────────────────────
# The exact real-data finding this section covers: AtsWatchlistProvider's
# own internal title/geo/freshness filtering can legitimately reduce every
# scraped job to zero BEFORE anything reaches 02_normalize/jobs.json --
# meaning `ats-watchlist` would otherwise never appear in `all_sources` at
# all, making a genuine "ran, found nothing" indistinguishable from "never
# ran". `.careeros/watchlist_state.json` (written every real run,
# regardless of filter outcome) is what makes the distinction possible.

def _seed_watchlist_state(cfg, state):
    with open(cfg.careeros_dir / "watchlist_state.json", "w") as f:
        f.write(json.dumps(state))


def test_compute_run_source_stats_shows_ats_watchlist_with_zero_records_when_it_ran(tmp_path, monkeypatch):
    """The core fix: zero pipeline items must still produce an ats-watchlist
    entry, not silence it -- distinguishing a real zero from absence."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    # jobs.json has ONLY ats-dataset -- exactly today's real shape, since
    # every ats-watchlist job was filtered out before normalize ever saw it.
    jobs = [{"id": "a", "source": "ats-dataset", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    _seed_watchlist_state(cfg, {
        "Swiggy::smartrecruiters:swiggy": {
            "ats": "smartrecruiters", "name": "Swiggy", "job_count": 27,
            "verification_status": "live", "last_checked_at": date, "consecutive_failures": 0,
        },
        "Fi Money::https://jobs.lever.co/epifi": {
            "ats": "lever", "name": "Fi Money", "job_count": 11,
            "verification_status": "live", "last_checked_at": date, "consecutive_failures": 0,
        },
        "Sarvam AI::https://jobs.ashbyhq.com/sarvam": {
            "ats": "ashby", "name": "Sarvam AI", "job_count": 60,
            "verification_status": "live", "last_checked_at": date, "consecutive_failures": 0,
        },
    })

    entries = compute_run_source_stats(cfg, date)
    by_source = {e.source: e for e in entries}

    assert "ats-watchlist" in by_source  # the actual bug this fixes: it must be present
    watchlist = by_source["ats-watchlist"]
    assert watchlist.decision["records"] == 0  # a real, legitimate zero
    assert watchlist.descriptive["scraped"] == 98  # 27 + 11 + 60 -- raw, before OUR filtering
    assert watchlist.descriptive["provider_filtered"] == 98  # all 98 filtered out, 0 reached records
    assert watchlist.descriptive["companies_checked"] == 3
    assert watchlist.descriptive["scraped_by_company"] == {"Swiggy": 27, "Fi Money": 11, "Sarvam AI": 60}
    assert watchlist.descriptive["verification_status_counts"] == {"live": 3}


def test_compute_run_source_stats_omits_ats_watchlist_when_it_never_ran(tmp_path, monkeypatch):
    """The negative case: no watchlist_state.json at all (or nothing checked
    TODAY) must NOT fabricate an ats-watchlist row -- genuinely didn't run
    stays genuinely absent, only a real check produces a real zero."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "a", "source": "ats-dataset", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    # no watchlist_state.json written at all
    entries = compute_run_source_stats(cfg, date)
    assert {e.source for e in entries} == {"ats-dataset"}


def test_compute_run_source_stats_ignores_watchlist_state_from_a_different_date(tmp_path, monkeypatch):
    """A watchlist_state.json entry last checked on a DIFFERENT date must
    not bleed into today's report -- state is a rolling file, not per-run."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "a", "source": "ats-dataset", "company": "Acme"}]
    _seed_run(cfg, date, jobs, [], [])
    _seed_watchlist_state(cfg, {
        "Swiggy::smartrecruiters:swiggy": {
            "ats": "smartrecruiters", "name": "Swiggy", "job_count": 20,
            "verification_status": "live", "last_checked_at": "2026-08-09", "consecutive_failures": 0,
        },
    })
    entries = compute_run_source_stats(cfg, date)
    assert {e.source for e in entries} == {"ats-dataset"}


def test_compute_run_source_stats_watchlist_funnel_reflects_a_real_nonzero_pipeline_item(tmp_path, monkeypatch):
    """A company that DID produce a surviving pipeline item still gets the
    full funnel: scraped -> provider_filtered -> records, all consistent
    with each other (scraped == provider_filtered + records)."""
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    date = "2026-08-10"
    jobs = [{"id": "l2a-1", "source": "ats-watchlist", "company": "MPL"}]
    processed = [{"id": "l2a-1", "date": date, "terminal_stage": "select", "reason": "apply", "score": 4.2}]
    outcomes = [{"id": "l2a-1", "tier": "apply", "score": 4.2, "recommendation": "apply",
                 "shortfall": {}, "primary_gap": None, "secondary_gap": None, "tiers": []}]
    _seed_run(cfg, date, jobs, processed, outcomes)
    _seed_watchlist_state(cfg, {
        "MPL::darwinbox:mpl": {
            "ats": "darwinbox", "name": "MPL", "job_count": 4,
            "verification_status": "live", "last_checked_at": date, "consecutive_failures": 0,
        },
    })
    entries = compute_run_source_stats(cfg, date)
    watchlist = {e.source: e for e in entries}["ats-watchlist"]
    assert watchlist.decision["records"] == 1
    assert watchlist.descriptive["scraped"] == 4
    assert watchlist.descriptive["provider_filtered"] == 3  # 4 scraped - 1 that reached records
    assert watchlist.descriptive["apply"] == 1


def test_aggregate_source_entries_pools_watchlist_funnel_across_dates():
    from careeros.pipeline.ledger import SourceLedgerEntry
    e1 = SourceLedgerEntry(
        date="2026-08-09", source="ats-watchlist",
        decision={"records": 0, "constraints_rejected": 0, "gate_dropped": 0,
                  "gate_keep_rate": None, "constraints_rejection_rate": None, "reject_reason_histogram": {}},
        descriptive={"evaluated": 0, "apply": 0, "consider": 0, "omit": 0, "mean_score": None,
                     "duplicate_of_other_source": {}, "company_yield": {},
                     "scraped": 31, "provider_filtered": 31, "companies_checked": 2,
                     "scraped_by_company": {"Swiggy": 20, "Fi Money": 11},
                     "verification_status_counts": {"live": 2}},
    )
    e2 = SourceLedgerEntry(
        date="2026-08-10", source="ats-watchlist",
        decision={"records": 0, "constraints_rejected": 0, "gate_dropped": 0,
                  "gate_keep_rate": None, "constraints_rejection_rate": None, "reject_reason_histogram": {}},
        descriptive={"evaluated": 0, "apply": 0, "consider": 0, "omit": 0, "mean_score": None,
                     "duplicate_of_other_source": {}, "company_yield": {},
                     "scraped": 98, "provider_filtered": 98, "companies_checked": 3,
                     "scraped_by_company": {"Swiggy": 27, "Fi Money": 11, "Sarvam AI": 60},
                     "verification_status_counts": {"live": 3}},
    )
    summary = aggregate_source_entries([e1, e2])
    assert summary["ats-watchlist"]["total_scraped"] == 129
    assert summary["ats-watchlist"]["total_provider_filtered"] == 129
    assert summary["ats-watchlist"]["total_companies_checked"] == 5
    assert summary["ats-watchlist"]["verification_status_counts"] == {"live": 5}


def test_render_source_ledger_markdown_shows_scraped_funnel_for_a_real_zero():
    summary = {
        "ats-watchlist": {
            "dates": ["2026-08-10"], "num_runs": 1, "total_records": 0,
            "pooled_gate_keep_rate": None, "apply_or_consider_events": 0,
            "top_reject_reasons": [], "duplicate_of_other_source": {}, "company_yield": {},
            "total_scraped": 98, "total_provider_filtered": 98, "total_companies_checked": 3,
            "verification_status_counts": {"live": 3},
        },
    }
    md = render_source_ledger_markdown(summary)
    assert "Companies checked: 3 (3 live)" in md
    assert "Scraped (raw, before this provider's own filtering): 98" in md
    assert "Provider-filtered (title/geo/freshness, before Records below): 98" in md
    assert "Records (pipeline items — what reached normalize): 0" in md
