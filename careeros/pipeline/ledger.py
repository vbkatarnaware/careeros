"""Stage: ledger (v2.0). Deterministic. Zero AI, zero tokens.

Aggregates one run's already-written stage output into per-DISCOVERY-TIER
statistics — the input the (currently dormant, see careeros/cli/tune.py once
it ships) query-tuning loop reads, and the human-readable record of "is this
query tier working" the candidate can read directly.

Everything here is DERIVED from files the deterministic pipeline already
writes (`02_normalize/jobs.json` for tier attribution via `Job.tiers`,
`.careeros/processed.jsonl` for each job's terminal fate, `07_select/
outcomes.json` for the jobs that reached evaluate) — nothing here re-reads a
job description or calls any AI. That separation is load-bearing, not
incidental: the ledger's DECISION metrics (gate-keep rate, constraints-
rejection rate) come from deterministic stages and the gate agent, never
from the evaluate agent's own score, specifically so a future tuner reading
this ledger cannot improve its apparent standing by influencing evaluation.
Eval scores appear here too, but only as DESCRIPTIVE fields — reported for a
human to read, never a tuning objective. See the v2.0 plan's Goodhart-guard
section for why that split lives in the data, not just in a docstring.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from careeros import runmeta
from careeros.config import Config

UNATTRIBUTED_TIER = "unattributed"  # a job with no Job.tiers set (e.g. pre-v2.0 data, or a provider with no segmented plan whose "default" tag wasn't set for some reason)


@dataclass
class TierLedgerEntry:
    date: str
    tier: str
    decision: dict[str, Any] = field(default_factory=dict)
    descriptive: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "tier": self.tier, "decision": self.decision, "descriptive": self.descriptive}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def compute_run_tier_stats(cfg: Config, date: str) -> list[TierLedgerEntry]:
    """Builds one TierLedgerEntry per discovery tier that appeared in this
    run. A job whose `tiers` spans more than one tier (dedupe's cross-
    location union) contributes to EVERY tier it lists — it was genuinely
    returned by each of them, so each tier's "records" count should reflect
    that, even though the job's single fate is only counted once per tier
    it's attributed to."""
    normalize_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not normalize_path.exists():
        return []
    with open(normalize_path) as f:
        jobs = json.load(f)
    tiers_by_id: dict[str, list[str]] = {
        j["id"]: (j.get("tiers") or [UNATTRIBUTED_TIER]) for j in jobs
    }

    processed_path = cfg.careeros_dir / "processed.jsonl"
    processed_today = [r for r in _load_jsonl(processed_path) if r.get("date") == date]
    processed_by_id = {r["id"]: r for r in processed_today}

    outcomes_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "outcomes.json"
    outcomes_by_id: dict[str, dict] = {}
    if outcomes_path.exists():
        with open(outcomes_path) as f:
            outcomes_by_id = {o["id"]: o for o in json.load(f)}

    # Per-tier accumulators.
    records: Counter = Counter()
    constraints_rejected: Counter = Counter()
    gate_dropped: Counter = Counter()
    reached_select: Counter = Counter()  # denominator complement for gate-keep-rate
    apply_count: Counter = Counter()
    consider_count: Counter = Counter()
    omit_count: Counter = Counter()
    reject_reasons: dict[str, Counter] = {}
    scores_by_tier: dict[str, list[float]] = {}
    shortfall_sums: dict[str, dict[str, float]] = {}
    shortfall_counts: Counter = Counter()

    for job_id, tiers in tiers_by_id.items():
        proc = processed_by_id.get(job_id)
        outcome = outcomes_by_id.get(job_id)
        for tier in tiers:
            records[tier] += 1
            if proc is None:
                continue  # deduped out before reaching any terminal stage this run
            stage = proc.get("terminal_stage")
            reason = proc.get("reason")
            if stage == "constraints":
                constraints_rejected[tier] += 1
                reject_reasons.setdefault(tier, Counter())[reason or "unspecified"] += 1
            elif stage == "gate":
                gate_dropped[tier] += 1
                reject_reasons.setdefault(tier, Counter())[reason or "unspecified"] += 1
            elif stage == "select":
                reached_select[tier] += 1
                if reason == "apply":
                    apply_count[tier] += 1
                elif reason == "consider":
                    consider_count[tier] += 1
                else:
                    omit_count[tier] += 1
                if outcome is not None:
                    scores_by_tier.setdefault(tier, []).append(outcome["score"])
                    if outcome.get("shortfall"):
                        sums = shortfall_sums.setdefault(tier, {})
                        for dim, val in outcome["shortfall"].items():
                            sums[dim] = sums.get(dim, 0.0) + val
                        shortfall_counts[tier] += 1

    entries: list[TierLedgerEntry] = []
    all_tiers = set(records) | set(constraints_rejected) | set(gate_dropped) | set(reached_select)
    for tier in sorted(all_tiers):
        gate_pool = gate_dropped[tier] + reached_select[tier]
        gate_keep_rate = round(reached_select[tier] / gate_pool, 4) if gate_pool else None
        constraints_pool = records[tier]
        constraints_rejection_rate = (
            round(constraints_rejected[tier] / constraints_pool, 4) if constraints_pool else None
        )
        mean_shortfall = None
        if shortfall_counts[tier]:
            mean_shortfall = {
                dim: round(total / shortfall_counts[tier], 4)
                for dim, total in shortfall_sums.get(tier, {}).items()
            }
        scores = scores_by_tier.get(tier, [])
        entries.append(TierLedgerEntry(
            date=date, tier=tier,
            decision={
                "records": records[tier],
                "constraints_rejected": constraints_rejected[tier],
                "gate_dropped": gate_dropped[tier],
                "gate_keep_rate": gate_keep_rate,
                "constraints_rejection_rate": constraints_rejection_rate,
                "reject_reason_histogram": dict(reject_reasons.get(tier, Counter())),
            },
            descriptive={
                "evaluated": reached_select[tier],
                "apply": apply_count[tier],
                "consider": consider_count[tier],
                "omit": omit_count[tier],
                "mean_score": round(sum(scores) / len(scores), 3) if scores else None,
                "mean_shortfall": mean_shortfall,
            },
        ))
    return entries


def aggregate_entries(entries: list[TierLedgerEntry]) -> dict[str, dict[str, Any]]:
    """Rolls up a list of per-date TierLedgerEntry (already filtered by the
    caller to exclude quarantined dates) into one summary per tier — the
    shape a tuner's arming check reads: records total, dates spanned,
    apply-or-consider event count, and pooled gate-keep rate."""
    by_tier: dict[str, list[TierLedgerEntry]] = {}
    for e in entries:
        by_tier.setdefault(e.tier, []).append(e)

    summary: dict[str, dict[str, Any]] = {}
    for tier, tier_entries in by_tier.items():
        total_records = sum(e.decision["records"] for e in tier_entries)
        total_gate_dropped = sum(e.decision["gate_dropped"] for e in tier_entries)
        total_reached_select = sum(e.descriptive["evaluated"] for e in tier_entries)
        gate_pool = total_gate_dropped + total_reached_select
        pooled_gate_keep_rate = round(total_reached_select / gate_pool, 4) if gate_pool else None
        apply_or_consider = sum(e.descriptive["apply"] + e.descriptive["consider"] for e in tier_entries)
        reasons: Counter = Counter()
        for e in tier_entries:
            reasons.update(e.decision["reject_reason_histogram"])
        summary[tier] = {
            "dates": sorted({e.date for e in tier_entries}),
            "num_runs": len({e.date for e in tier_entries}),
            "total_records": total_records,
            "pooled_gate_keep_rate": pooled_gate_keep_rate,
            "apply_or_consider_events": apply_or_consider,
            "top_reject_reasons": reasons.most_common(5),
        }
    return summary


@dataclass
class SourceLedgerEntry:
    """Same shape as `TierLedgerEntry`, grouped by `Job.source` (discovery
    LAYER/provider — 'ats-dataset' = Layer 1, 'ats-watchlist' = Layer 2A)
    instead of `Job.tiers` (discovery QUERY). Answers a different question:
    not "is this query tier working" but "is this discovery layer/company
    adding anything Layer 1 didn't already have" — see `docs/ats-registry.md`
    and the Layer 2A measurement proposal this implements."""
    date: str
    source: str
    decision: dict[str, Any] = field(default_factory=dict)
    descriptive: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "source": self.source, "decision": self.decision, "descriptive": self.descriptive}


_WATCHLIST_SOURCE = "ats-watchlist"
_WATCHLIST_STATE_FILENAME = "watchlist_state.json"


def _watchlist_scraped_summary(cfg: Config, date: str) -> Optional[dict[str, Any]]:
    """Reads `.careeros/watchlist_state.json` — written by `AtsWatchlistProvider.
    fetch()` every real `discover` run, for every entry, REGARDLESS of whether
    any of its jobs survive that provider's own internal title/geo/freshness
    filtering (see `providers/ats_watchlist.py`). This is the ONLY place that
    filtering's raw input ('scraped') is recorded — `01_discover/raw.json`
    and `02_normalize/jobs.json` only ever see what's LEFT after it, so a
    company checked today that filtered to zero is otherwise indistinguishable
    from Layer 2A not running at all.

    Returns None when nothing was checked today (empty watchlist, or this
    date predates Layer 2A) — the correct signal for "genuinely didn't run",
    as opposed to a present-but-zero-scraped summary, which would claim a
    check happened when it didn't.
    """
    state_path = cfg.careeros_dir / _WATCHLIST_STATE_FILENAME
    if not state_path.exists():
        return None
    with open(state_path) as f:
        state = json.load(f)
    checked_today = {k: v for k, v in state.items() if v.get("last_checked_at") == date}
    if not checked_today:
        return None
    scraped_by_company = {v.get("name", k): (v.get("job_count") or 0) for k, v in checked_today.items()}
    return {
        "companies_checked": len(checked_today),
        "scraped": sum(scraped_by_company.values()),
        "scraped_by_company": scraped_by_company,
        "verification_status_counts": dict(Counter(v.get("verification_status") for v in checked_today.values())),
    }


def compute_run_source_stats(cfg: Config, date: str) -> list[SourceLedgerEntry]:
    """The Layer 2A companion to `compute_run_tier_stats` — same four
    already-written files, one more (`03_dedupe/dropped.json`) read ONLY
    here, for cross-layer duplicate attribution. Every number is derived
    after the fact from deterministic pipeline output; no AI, no new stage,
    no change to what Gate/Evaluate/constraints actually decide.

    `duplicate_of_other_source` answers the specific question this was
    built for — "how many of THIS source's jobs were duplicates of a job
    a DIFFERENT source already found" (e.g. `ats-watchlist`'s dict holding
    `{"ats-dataset": 3}` means 3 Layer 2A jobs this run were the same
    role Layer 1 had already surfaced) — not just "a duplicate occurred."
    Deliberately NOT called a false-positive rate anywhere in this module:
    a Gate/constraints rejection is a rejection, not a labeled false
    positive — we have no human ground truth to call it that."""
    normalize_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not normalize_path.exists():
        return []
    with open(normalize_path) as f:
        jobs = json.load(f)
    source_by_id: dict[str, str] = {j["id"]: (j.get("source") or UNATTRIBUTED_TIER) for j in jobs}
    company_by_id: dict[str, str] = {j["id"]: (j.get("company") or "unknown") for j in jobs}

    processed_path = cfg.careeros_dir / "processed.jsonl"
    processed_today = [r for r in _load_jsonl(processed_path) if r.get("date") == date]
    processed_by_id = {r["id"]: r for r in processed_today}

    outcomes_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "outcomes.json"
    outcomes_by_id: dict[str, dict] = {}
    if outcomes_path.exists():
        with open(outcomes_path) as f:
            outcomes_by_id = {o["id"]: o for o in json.load(f)}

    dropped_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "dropped.json"
    dropped_records: list[dict] = []
    if dropped_path.exists():
        with open(dropped_path) as f:
            dropped_records = json.load(f)

    # Per-source accumulators — mirrors compute_run_tier_stats exactly,
    # `source` is a single value per job (not a list like `tiers`), so
    # there's no "spans multiple groups" case to handle here.
    records: Counter = Counter()
    constraints_rejected: Counter = Counter()
    gate_dropped: Counter = Counter()
    reached_select: Counter = Counter()
    apply_count: Counter = Counter()
    consider_count: Counter = Counter()
    omit_count: Counter = Counter()
    reject_reasons: dict[str, Counter] = {}
    scores_by_source: dict[str, list[float]] = {}
    company_yield: dict[str, Counter] = {}  # source -> company -> apply+consider count

    for job_id, source in source_by_id.items():
        records[source] += 1
        proc = processed_by_id.get(job_id)
        if proc is None:
            continue  # deduped out before reaching any terminal stage this run
        stage = proc.get("terminal_stage")
        reason = proc.get("reason")
        if stage == "constraints":
            constraints_rejected[source] += 1
            reject_reasons.setdefault(source, Counter())[reason or "unspecified"] += 1
        elif stage == "gate":
            gate_dropped[source] += 1
            reject_reasons.setdefault(source, Counter())[reason or "unspecified"] += 1
        elif stage == "select":
            reached_select[source] += 1
            outcome = outcomes_by_id.get(job_id)
            if outcome is not None:
                scores_by_source.setdefault(source, []).append(outcome["score"])
            if reason == "apply":
                apply_count[source] += 1
                company_yield.setdefault(source, Counter())[company_by_id.get(job_id, "unknown")] += 1
            elif reason == "consider":
                consider_count[source] += 1
                company_yield.setdefault(source, Counter())[company_by_id.get(job_id, "unknown")] += 1
            else:
                omit_count[source] += 1

    # Cross-layer duplicate attribution: dropped_source -> {survivor_source: count}.
    duplicate_of_source: dict[str, Counter] = {}
    for d in dropped_records:
        if d.get("_drop_reason") != "cross-location":
            continue
        survivor_source = d.get("_duplicate_of_source")
        if not survivor_source:
            continue
        dropped_source = d.get("source") or UNATTRIBUTED_TIER
        duplicate_of_source.setdefault(dropped_source, Counter())[survivor_source] += 1

    # Force ats-watchlist into the report whenever it genuinely ran today,
    # even with zero pipeline items — otherwise a legitimate zero (every
    # scraped job filtered out) is indistinguishable from "didn't run".
    watchlist_summary = _watchlist_scraped_summary(cfg, date)
    entries: list[SourceLedgerEntry] = []
    all_sources = set(records) | set(constraints_rejected) | set(gate_dropped) | set(reached_select)
    if watchlist_summary is not None:
        all_sources.add(_WATCHLIST_SOURCE)
    for source in sorted(all_sources):
        gate_pool = gate_dropped[source] + reached_select[source]
        gate_keep_rate = round(reached_select[source] / gate_pool, 4) if gate_pool else None
        constraints_pool = records[source]
        constraints_rejection_rate = (
            round(constraints_rejected[source] / constraints_pool, 4) if constraints_pool else None
        )
        scores = scores_by_source.get(source, [])
        descriptive: dict[str, Any] = {
            "evaluated": reached_select[source],
            "apply": apply_count[source],
            "consider": consider_count[source],
            "omit": omit_count[source],
            "mean_score": round(sum(scores) / len(scores), 3) if scores else None,
            "duplicate_of_other_source": dict(duplicate_of_source.get(source, Counter())),
            "company_yield": dict(company_yield.get(source, Counter())),
        }
        if source == _WATCHLIST_SOURCE and watchlist_summary is not None:
            # The funnel step before `records` — scraped (raw, pre-filter)
            # -> provider_filtered (dropped inside AtsWatchlistProvider's own
            # title/geo/freshness chain, never reaching `records` at all).
            # `records` here already IS "pipeline items" (jobs.json), so
            # scraped -> provider_filtered -> records -> ... -> apply/consider
            # is the full chain the report renders, per company.
            descriptive["scraped"] = watchlist_summary["scraped"]
            descriptive["provider_filtered"] = watchlist_summary["scraped"] - records[source]
            descriptive["companies_checked"] = watchlist_summary["companies_checked"]
            descriptive["scraped_by_company"] = watchlist_summary["scraped_by_company"]
            descriptive["verification_status_counts"] = watchlist_summary["verification_status_counts"]
        entries.append(SourceLedgerEntry(
            date=date, source=source,
            decision={
                "records": records[source],
                "constraints_rejected": constraints_rejected[source],
                "gate_dropped": gate_dropped[source],
                "gate_keep_rate": gate_keep_rate,
                "constraints_rejection_rate": constraints_rejection_rate,
                "reject_reason_histogram": dict(reject_reasons.get(source, Counter())),
            },
            descriptive=descriptive,
        ))
    return entries


def aggregate_source_entries(entries: list[SourceLedgerEntry]) -> dict[str, dict[str, Any]]:
    """Rolls up per-date SourceLedgerEntry into one summary per source —
    same shape/spirit as `aggregate_entries`, plus the two fields that only
    make sense at the source level: pooled cross-layer duplicate counts and
    pooled per-company yield (the latter meaningless for a tier, since a
    tier isn't a set of companies)."""
    by_source: dict[str, list[SourceLedgerEntry]] = {}
    for e in entries:
        by_source.setdefault(e.source, []).append(e)

    summary: dict[str, dict[str, Any]] = {}
    for source, source_entries in by_source.items():
        total_records = sum(e.decision["records"] for e in source_entries)
        total_gate_dropped = sum(e.decision["gate_dropped"] for e in source_entries)
        total_reached_select = sum(e.descriptive["evaluated"] for e in source_entries)
        gate_pool = total_gate_dropped + total_reached_select
        pooled_gate_keep_rate = round(total_reached_select / gate_pool, 4) if gate_pool else None
        apply_or_consider = sum(e.descriptive["apply"] + e.descriptive["consider"] for e in source_entries)
        reasons: Counter = Counter()
        duplicate_of: Counter = Counter()
        company_totals: Counter = Counter()
        total_scraped = 0
        total_provider_filtered = 0
        total_companies_checked = 0
        verification_status_totals: Counter = Counter()
        has_watchlist_funnel = False
        for e in source_entries:
            reasons.update(e.decision["reject_reason_histogram"])
            duplicate_of.update(e.descriptive["duplicate_of_other_source"])
            company_totals.update(e.descriptive["company_yield"])
            if "scraped" in e.descriptive:
                has_watchlist_funnel = True
                total_scraped += e.descriptive["scraped"]
                total_provider_filtered += e.descriptive["provider_filtered"]
                total_companies_checked += e.descriptive["companies_checked"]
                verification_status_totals.update(e.descriptive["verification_status_counts"])
        summary[source] = {
            "dates": sorted({e.date for e in source_entries}),
            "num_runs": len({e.date for e in source_entries}),
            "total_records": total_records,
            "pooled_gate_keep_rate": pooled_gate_keep_rate,
            "apply_or_consider_events": apply_or_consider,
            "top_reject_reasons": reasons.most_common(5),
            "duplicate_of_other_source": dict(duplicate_of),
            "company_yield": dict(company_totals.most_common()),
        }
        if has_watchlist_funnel:
            summary[source]["total_scraped"] = total_scraped
            summary[source]["total_provider_filtered"] = total_provider_filtered
            summary[source]["total_companies_checked"] = total_companies_checked
            summary[source]["verification_status_counts"] = dict(verification_status_totals)
    return summary


def render_source_ledger_markdown(summary: dict[str, dict[str, Any]]) -> str:
    """Discovery-LAYER section — Layer 1 (`ats-dataset`) vs Layer 2A
    (`ats-watchlist`) vs any other registered source. Appended after the
    per-tier report by `careeros ledger`; same terse per-group style as
    `render_ledger_markdown`, no arming/tuner concept here (arming is
    specifically about the query tuner, which doesn't apply to discovery
    layer/company measurement)."""
    lines = ["## Discovery layer (Layer 1 vs Layer 2A)", ""]
    if not summary:
        lines.append("_No data yet — run `careeros ledger` after at least one `careeros daily` run._")
        return "\n".join(lines) + "\n"
    for source in sorted(summary):
        s = summary[source]
        lines.append(f"### {source}")
        lines.append(f"- Runs: {s['num_runs']} ({s['dates'][0]} to {s['dates'][-1]})" if s["dates"] else "- Runs: 0")
        if "total_scraped" in s:
            # The funnel step before "Records" — what AtsWatchlistProvider's
            # own internal filtering already dropped, which 02_normalize/
            # jobs.json ("Records") never sees. Rendered even when scraped
            # is 0 or provider_filtered == scraped (a real, legitimate zero
            # is still a checked run, not an absent one).
            statuses = s.get("verification_status_counts") or {}
            status_str = ", ".join(f"{n} {st}" for st, n in statuses.items()) or "n/a"
            lines.append(f"- Companies checked: {s['total_companies_checked']} ({status_str})")
            lines.append(f"- Scraped (raw, before this provider's own filtering): {s['total_scraped']}")
            lines.append(f"- Provider-filtered (title/geo/freshness, before Records below): {s['total_provider_filtered']}")
        lines.append(f"- Records (pipeline items — what reached normalize): {s['total_records']}")
        gkr = s["pooled_gate_keep_rate"]
        lines.append(f"- Gate-keep rate: {gkr:.1%}" if gkr is not None else "- Gate-keep rate: n/a")
        lines.append(f"- Apply/Consider events: {s['apply_or_consider_events']}")
        if s["top_reject_reasons"]:
            reasons_str = ", ".join(f"{r} ({n})" for r, n in s["top_reject_reasons"])
            lines.append(f"- Top rejection reasons: {reasons_str}")
        if s["duplicate_of_other_source"]:
            dup_str = ", ".join(f"{n} of {src}" for src, n in s["duplicate_of_other_source"].items())
            lines.append(f"- Duplicates of another source's job: {dup_str}")
        if s["company_yield"]:
            yield_str = ", ".join(f"{c} ({n})" for c, n in s["company_yield"].items())
            lines.append(f"- Apply/Consider by company: {yield_str}")
        lines.append("")
    return "\n".join(lines) + "\n"


def load_quarantine(cfg: Config) -> set[str]:
    """Dates (or run labels) excluded from ledger aggregation — the 07-29/
    07-30 inflated-scoring runs, any `qa-*` label, and 07-12's known
    dedupe-count corruption. See .careeros/learning/quarantine.json."""
    path = cfg.careeros_dir / "learning" / "quarantine.json"
    if not path.exists():
        return set()
    with open(path) as f:
        data = json.load(f)
    return set(data.get("dates", []))


def compute_arming(
    summary: dict[str, dict[str, Any]],
    *, min_days: int = 28, min_records: int = 400, min_events: int = 8,
) -> dict[str, dict[str, Any]]:
    """Per-tier arming check for the (currently dormant) query tuner —
    careeros/cli/tune.py refuses to propose a change for any tier that
    fails this. All three floors must hold, per the v2.0 plan: ~4 calendar
    weeks of accumulated data, AND >=400 records, AND >=8 apply-or-consider
    events for that specific tier. Below any one, the tier is statistically
    too thin to act on — see the plan's sample-size arithmetic (detecting a
    halving of apply-yield needs n~700/arm at this volume)."""
    from datetime import date as _date

    out: dict[str, dict[str, Any]] = {}
    for tier, s in summary.items():
        reasons = []
        if s["dates"]:
            try:
                span_days = (_date.fromisoformat(s["dates"][-1]) - _date.fromisoformat(s["dates"][0])).days
            except ValueError:
                span_days = 0
        else:
            span_days = 0
        if span_days < min_days:
            reasons.append(f"only {span_days}d of history (need {min_days}d)")
        if s["total_records"] < min_records:
            reasons.append(f"only {s['total_records']} records (need {min_records})")
        if s["apply_or_consider_events"] < min_events:
            reasons.append(f"only {s['apply_or_consider_events']} apply/consider events (need {min_events})")
        out[tier] = {"armed": not reasons, "reason": "; ".join(reasons)}
    return out


def render_ledger_markdown(summary: dict[str, dict[str, Any]], *, arming: dict[str, dict[str, Any]]) -> str:
    lines = ["# CareerOS Learning Ledger", "", "Aggregated per discovery tier, quarantined runs excluded.", ""]
    if not summary:
        lines.append("_No data yet — run `careeros ledger` after at least one `careeros daily` run._")
        return "\n".join(lines) + "\n"
    for tier in sorted(summary):
        s = summary[tier]
        a = arming.get(tier, {})
        lines.append(f"## {tier}")
        lines.append(f"- Runs: {s['num_runs']} ({s['dates'][0]} to {s['dates'][-1]})" if s["dates"] else "- Runs: 0")
        lines.append(f"- Records: {s['total_records']}")
        gkr = s["pooled_gate_keep_rate"]
        lines.append(f"- Gate-keep rate: {gkr:.1%}" if gkr is not None else "- Gate-keep rate: n/a")
        lines.append(f"- Apply/Consider events: {s['apply_or_consider_events']}")
        if s["top_reject_reasons"]:
            reasons_str = ", ".join(f"{r} ({n})" for r, n in s["top_reject_reasons"])
            lines.append(f"- Top reject reasons: {reasons_str}")
        armed = a.get("armed", False)
        status = "ARMED — tuner may propose changes" if armed else "dormant — " + a.get("reason", "insufficient data")
        lines.append(f"- Tuner status: {status}")
        lines.append("")
    return "\n".join(lines) + "\n"
