"""Query-tuning guardrails (v2.0). Pure logic — no file I/O, no network.

Ships as part of v2.0 but the tuner it backs is DORMANT: `careeros tune`
(careeros/cli/tune.py) refuses to act on any tier that fails
`careeros/pipeline/ledger.py`'s `compute_arming` (roughly 4 weeks / 400
records / 8 apply-or-consider events, per tier — see that module for the
sample-size arithmetic). This module is the safety envelope a proposal must
clear BEFORE it's allowed to reach the config overlay
(`careeros/config.py`'s `TUNING_OVERLAY_ALLOWLIST` merge) — it never talks
to the overlay file directly; the CLI layer owns that.

Every guardrail below traces to a specific failure mode found while
designing this release:
  - one change per cycle, across ALL tiers: three simultaneous changes
    destroy attribution — you can no longer tell which change did what.
  - a record floor per tier: below it, a tier can never accumulate enough
    sample to earn its own budget back (the death-spiral case).
  - exclusion-list decay: each `title_exclusion_search` entry is
    individually defensible ("exclude 'Product Marketing Manager'") and
    collectively catastrophic (exclude enough terms and you've excluded
    half of "Product Manager").
  - the control tier is fixed by a human, never chosen by the tuner itself
    — a tuner that could pick its own control would pick the one that
    flatters it.
  - revert is diff-in-differences against that control: a
    07-30-vs-07-31-magnitude swing (a bad week, a model change) must not
    be blamed on the one tier that happened to get changed that cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from careeros.pipeline.ledger import TierLedgerEntry

VALID_FIELDS = ("title_search", "title_exclusion_search", "tier_limits")


def is_check_due(last_checked: Optional[str], today: str, *, cadence_days: int = 7) -> bool:
    """Weekly check-in cadence (v2.0.1) — deliberately separate from
    `cadence_days` in careeros/config.py's `tuning:` block, which is how
    often an actual CHANGE may be applied (30 days). Looking at the ledger
    is free and safe every week; touching the search is what needs the
    longer, statistically-defensible wait — see careeros/pipeline/ledger.py
    for the sample-size arithmetic behind that split.

    `last_checked=None` (never checked before) is always due. An
    unparseable date is treated as due too — safer to check an extra time
    than to silently never check again because of a corrupt state file."""
    if last_checked is None:
        return True
    from datetime import date as _date
    try:
        last = _date.fromisoformat(last_checked)
        current = _date.fromisoformat(today)
    except ValueError:
        return True
    return (current - last).days >= cadence_days


@dataclass(frozen=True)
class TuningFlag:
    """One armed tier with a pattern worth putting in front of the
    candidate — the output of the weekly check-due pass. Deliberately
    thin: the actual proposed VALUE (which title to exclude, etc.) is still
    a judgment call made by reading the real rejected postings behind
    `top_reason` (see prompts/tune_v1.md) — this just says WHERE to look
    and how much evidence backs it, in plain terms, not what to do about it."""

    tier: str
    top_reason: str
    reason_count: int
    total_records: int
    label: str  # a ready-to-read plain-language sentence, no jargon

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "top_reason": self.top_reason, "reason_count": self.reason_count,
                "total_records": self.total_records, "label": self.label}


def find_tuning_flags(
    summary: dict[str, dict[str, Any]],
    arming: dict[str, dict[str, Any]],
    *, control_tier: str, min_reason_count: int = 8,
) -> list[TuningFlag]:
    """From the pooled ledger summary, which ARMED, non-control tiers have a
    reject reason common enough to be worth asking the candidate about.
    `min_reason_count` mirrors the arming floor's spirit — a reason that
    only explains a couple of rejections isn't a pattern, it's noise."""
    flags: list[TuningFlag] = []
    for tier, s in summary.items():
        if tier == control_tier or not arming.get(tier, {}).get("armed"):
            continue
        top = s.get("top_reject_reasons") or []
        if not top:
            continue
        reason, count = top[0]
        if count < min_reason_count:
            continue
        label = (
            f"Over the last {len(s['dates'])} runs, {count} jobs in your "
            f"'{tier}' search were rejected for the same reason ('{reason}'), "
            f"out of {s['total_records']} records found there."
        )
        flags.append(TuningFlag(tier=tier, top_reason=reason, reason_count=count,
                                total_records=s["total_records"], label=label))
    return flags


@dataclass(frozen=True)
class Proposal:
    tier: str
    field: str  # one of VALID_FIELDS
    value: Any  # the single entry to add (title_search/title_exclusion_search) or the new limit (tier_limits)
    evidence: str  # free text or comma-joined job ids — MUST be non-empty
    sunset: Optional[str] = None  # required for title_exclusion_search, an ISO date

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "field": self.field, "value": self.value,
                "evidence": self.evidence, "sunset": self.sunset}


def record_floor(num_tiers: int, daily_total: int) -> int:
    """A tier's `tier_limits` may never be pushed below this. Below ~15
    records/tier/day, a tier's weekly sample can never reach the arming
    minimum (careeros/pipeline/ledger.py's compute_arming), so it could
    never earn its budget back once cut — the death-spiral guard."""
    equal_share = daily_total / max(1, num_tiers)
    return max(15, round(0.5 * equal_share))


def validate_proposal(
    proposal: Proposal,
    *,
    arming: dict[str, dict[str, Any]],
    control_tier: str,
    current_overlay_api: dict[str, Any],
    changes_this_cycle: int,
    num_tiers: int,
    daily_total: int,
    max_exclusion_entries: int = 8,
    tier_limit_max_delta_pct: float = 0.25,
) -> list[str]:
    """Returns a list of violation reasons — empty means the proposal may
    proceed to `careeros tune --apply`. Never mutates anything; the CLI
    layer applies a proposal that passes this cleanly."""
    violations: list[str] = []

    if proposal.field not in VALID_FIELDS:
        violations.append(f"'{proposal.field}' is not a tunable field (must be one of {VALID_FIELDS})")
        return violations  # nothing else below is meaningful for a bad field

    if proposal.tier == control_tier:
        violations.append(f"'{proposal.tier}' is the fixed control tier — it may never be changed by the tuner")

    tier_arming = arming.get(proposal.tier, {"armed": False, "reason": "no ledger data for this tier"})
    if not tier_arming.get("armed"):
        violations.append(f"tier '{proposal.tier}' is not armed yet: {tier_arming.get('reason')}")

    if changes_this_cycle >= 1:
        violations.append("a change has already been made this cycle — max one change per cycle, across ALL tiers")

    if not proposal.evidence or not proposal.evidence.strip():
        violations.append("proposal cites no evidence — every change must name what motivates it")

    if proposal.field == "title_exclusion_search":
        current = list(current_overlay_api.get("title_exclusion_search", []))
        if proposal.value in current:
            violations.append(f"'{proposal.value}' is already in title_exclusion_search")
        elif len(current) >= max_exclusion_entries:
            violations.append(
                f"title_exclusion_search already has {len(current)} entries "
                f"(cap {max_exclusion_entries}) — remove or let one expire first"
            )
        if not proposal.sunset:
            violations.append("title_exclusion_search additions require a sunset date — nobody removes these by hand")

    elif proposal.field == "title_search":
        current = list(current_overlay_api.get("title_search", []))
        if proposal.value in current:
            violations.append(f"'{proposal.value}' is already in title_search")

    elif proposal.field == "tier_limits":
        current_limits = dict(current_overlay_api.get("tier_limits", {}))
        before = current_limits.get(proposal.tier)
        after = proposal.value
        if before is not None and before > 0:
            delta_pct = abs(after - before) / before
            if delta_pct > tier_limit_max_delta_pct:
                violations.append(
                    f"tier_limits change for '{proposal.tier}' is {delta_pct:.0%}, "
                    f"exceeding the {tier_limit_max_delta_pct:.0%} per-cycle cap"
                )
        floor = record_floor(num_tiers, daily_total)
        if after < floor:
            violations.append(f"tier_limits value {after} is below the record floor {floor} for this profile")

    return violations


def apply_proposal_to_overlay(overlay_api: dict[str, Any], proposal: Proposal) -> dict[str, Any]:
    """Pure: returns a NEW overlay `api` dict with the proposal applied.
    Caller (careeros/cli/tune.py) is responsible for writing this to
    overlay.yaml and appending the change to changes.jsonl. Must only be
    called after `validate_proposal` returns no violations."""
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
           for k, v in overlay_api.items()}
    if proposal.field in ("title_search", "title_exclusion_search"):
        out.setdefault(proposal.field, [])
        out[proposal.field] = out[proposal.field] + [proposal.value]
    elif proposal.field == "tier_limits":
        out.setdefault("tier_limits", {})
        out["tier_limits"] = {**out["tier_limits"], proposal.tier: proposal.value}
    return out


def sunset_expired_exclusions(overlay_api: dict[str, Any], *, as_of: str, sunsets: dict[str, str]) -> dict[str, Any]:
    """Drops any `title_exclusion_search` entry whose recorded sunset date
    (from changes.jsonl, keyed by the excluded term) has passed. Nobody
    ever removes an exclusion term by hand, so this is the mechanism that
    keeps the list from only ever growing."""
    from datetime import date as _date

    try:
        today = _date.fromisoformat(as_of)
    except ValueError:
        return overlay_api  # can't determine expiry -- leave untouched, conservative

    current = list(overlay_api.get("title_exclusion_search", []))
    kept = []
    for term in current:
        sunset = sunsets.get(term)
        if sunset:
            try:
                if _date.fromisoformat(sunset) < today:
                    continue  # expired
            except ValueError:
                pass
        kept.append(term)
    out = dict(overlay_api)
    out["title_exclusion_search"] = kept
    return out


@dataclass(frozen=True)
class RevertDecision:
    should_revert: bool
    reason: str
    confounded: bool = False  # True if a trigger fired but the control moved similarly too


def _pooled(entries: list[TierLedgerEntry], field_path: tuple[str, str]) -> int:
    section, key = field_path
    return sum(getattr(e, section)[key] for e in entries if getattr(e, section).get(key) is not None)


def check_revert(
    changed_window: list[TierLedgerEntry],
    changed_baseline: list[TierLedgerEntry],
    control_window: list[TierLedgerEntry],
    control_baseline: list[TierLedgerEntry],
    *,
    min_eligible_per_run: int = 60,
    min_evaluated_per_run: int = 12,
    precision_drop_floor: float = 0.10,
) -> RevertDecision:
    """R1 (volume, absolute) / R2 (precision, diff-in-diff vs control) / R3
    (killed-the-best, absolute). All three are evaluated; the first that
    fires wins, in that order — R1 needs no comparison at all, so it's
    checked first. R2's diff-in-differences is what protects against a
    07-30-vs-07-31-magnitude swing being blamed on the wrong cause: if the
    untouched control moved similarly in the same window, this returns
    `confounded=True` and does NOT revert."""
    if not changed_window:
        return RevertDecision(False, "no data in the revert window yet")

    # R1 — absolute volume floors, median-based, no comparison needed.
    records = sorted(e.decision["records"] for e in changed_window)
    evaluated = sorted(e.descriptive["evaluated"] for e in changed_window)
    median_records = records[len(records) // 2]
    median_evaluated = evaluated[len(evaluated) // 2]
    if median_records < min_eligible_per_run or median_evaluated < min_evaluated_per_run:
        return RevertDecision(
            True,
            f"R1 volume floor breached: median records={median_records} "
            f"(floor {min_eligible_per_run}), median evaluated={median_evaluated} "
            f"(floor {min_evaluated_per_run})",
        )

    # R3 — killed the best jobs. Absolute and deliberately extreme (zero),
    # so it only fires on a genuine kill, not ordinary variance.
    window_events = sum(e.descriptive["apply"] + e.descriptive["consider"] for e in changed_window)
    baseline_events = sum(e.descriptive["apply"] + e.descriptive["consider"] for e in changed_baseline)
    if window_events == 0 and baseline_events >= 3:
        return RevertDecision(
            True,
            f"R3 killed-the-best: 0 apply/consider events over the revert window, "
            f"vs {baseline_events} in the prior baseline",
        )

    # R2 — precision, diff-in-differences against the fixed control tier.
    def _gate_keep_rate(entries: list[TierLedgerEntry]) -> Optional[float]:
        dropped = sum(e.decision["gate_dropped"] for e in entries)
        kept = sum(e.descriptive["evaluated"] for e in entries)
        pool = dropped + kept
        return kept / pool if pool else None

    changed_before = _gate_keep_rate(changed_baseline)
    changed_after = _gate_keep_rate(changed_window)
    control_before = _gate_keep_rate(control_baseline)
    control_after = _gate_keep_rate(control_window)

    if None in (changed_before, changed_after):
        return RevertDecision(False, "insufficient gate-keep-rate data to evaluate R2")

    changed_delta = changed_after - changed_before
    if changed_delta >= -precision_drop_floor:
        return RevertDecision(False, "no trigger fired — change looks neutral or positive")

    control_delta = (control_after - control_before) if control_before is not None and control_after is not None else 0.0
    # Difference-in-differences: how much WORSE the changed tier did than
    # the control over the same window.
    relative_drop = changed_delta - control_delta
    if relative_drop <= -precision_drop_floor:
        return RevertDecision(
            True,
            f"R2 precision drop: changed tier's gate-keep rate fell {abs(changed_delta):.1%} "
            f"vs control's {control_delta:+.1%} over the same window (relative drop {abs(relative_drop):.1%})",
        )

    return RevertDecision(
        False,
        f"changed tier's gate-keep rate fell {abs(changed_delta):.1%}, but the control moved "
        f"similarly ({control_delta:+.1%}) — likely an environmental shift, not this change",
        confounded=True,
    )
