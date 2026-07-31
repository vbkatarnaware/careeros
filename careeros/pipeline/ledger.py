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
