"""Discovery quota guard (P2.8).

CareerOS RECOMMENDS a daily discovery limit and WARNS/PREVENTS before you
exhaust your Fantastic.jobs weekly record quota — but it NEVER silently
overrides your configured `api.limit`. You always own the final number.

The binding constraint on the free tier is records/week (500), not requests,
so the guard reasons in RECORDS. It spreads your weekly quota across the days
you actually run discovery and prints the recommendation WITH its arithmetic,
so you can see exactly why a number was chosen. Consumption is tracked in
`.careeros/discovery_budget.json` — a plain rolling-week counter, not a
database — so a run can stop BEFORE a mid-week hard 429 rather than after.

v1.7: `api.limit` is a DAILY TOTAL across every search, not a per-search
count. `discover` divides it across however many query tiers the candidate's
profile generates. The old per-search reading meant a 3-tier profile fetched
3x whatever number the user typed — see `Recommendation.configured_per_search`.

Design contract: this module is advisory + protective. `recommend()` and the
warnings are pure/printable; `check_before_run()` can ask the caller to stop,
but nothing here edits the user's config or changes what gets fetched beyond
honoring an already-exhausted weekly budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

# Only the free tier's weekly record quota is publicly known/verified. Other
# EXPLICITLY-set plans are None on purpose: the guard stays purely
# informational for those until the user sets `api.weekly_record_quota`
# themselves, rather than inventing an unverified paid-tier number. An UNSET
# plan is different (P2.9.1): most new OSS users are on the free tier, so
# `weekly_quota()` assumes "free" rather than silently falling back to a
# hardcoded 100-record default that risks an unexpected mid-week 429 for
# anyone who never touched api.plan.
PLAN_WEEKLY_RECORD_QUOTA: dict[str, Optional[int]] = {
    "free": 500,
    "rapidapi": None,
    "paid": None,
    "enterprise": None,
}

DEFAULT_LIMIT = 100  # mirrors the `discover --limit` default (a daily total)
BUDGET_FILENAME = "discovery_budget.json"


def plan_is_assumed(api_cfg: dict[str, Any]) -> bool:
    """True when the user never configured api.plan or api.weekly_record_quota
    and "free" is being assumed as the safe OSS default, rather than a
    deliberate choice. Callers use this to show a one-time disclosure."""
    explicit_quota = api_cfg.get("weekly_record_quota")
    has_explicit_quota = isinstance(explicit_quota, int) and explicit_quota > 0
    return not api_cfg.get("plan") and not has_explicit_quota


def weekly_quota(api_cfg: dict[str, Any]) -> Optional[int]:
    """The records/week ceiling: an explicit `api.weekly_record_quota` wins;
    otherwise it's derived from `api.plan`, defaulting to "free" (500) when
    plan is unset — see the module-level note on PLAN_WEEKLY_RECORD_QUOTA.
    Still None for an EXPLICITLY-set plan with no verified number (paid/
    rapidapi/enterprise) — that stays a deliberate, informational-only case."""
    explicit = api_cfg.get("weekly_record_quota")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    plan = api_cfg.get("plan") or "free"
    return PLAN_WEEKLY_RECORD_QUOTA.get(plan)


def effective_limit(api_cfg: dict[str, Any], cli_default: int = DEFAULT_LIMIT) -> int:
    """The DAILY job total the user actually gets: their configured
    `api.limit` if set, else the CLI default. `discover` divides this across
    the candidate's search tiers. The guard reads this; it never rewrites it."""
    v = api_cfg.get("limit")
    return v if isinstance(v, int) and v > 0 else cli_default


def _active_days(api_cfg: dict[str, Any]) -> int:
    v = api_cfg.get("active_days_per_week")
    return v if isinstance(v, int) and 1 <= v <= 7 else 7


@dataclass
class Recommendation:
    plan: Optional[str]
    plan_is_assumed: bool
    quota: Optional[int]
    active_days: int
    requests_per_run: int
    goal_interviews_per_week: Optional[int]
    configured_limit: int
    configured_records_per_day: int
    configured_weekly_records: int
    recommended_per_request: Optional[int]
    recommended_records_per_day: Optional[int]
    over_quota: bool

    @property
    def configured_per_search(self) -> int:
        """How `configured_limit` (a DAILY total) is actually spent per search
        once `discover` divides it across this candidate's tiers. Floored at 1
        — asking for fewer jobs/day than you have searches still fetches at
        least one per search, which `discover` warns about."""
        return max(1, self.configured_limit // max(1, self.requests_per_run))

    @property
    def recommended_daily_total(self) -> Optional[int]:
        """The number onboarding/`doctor` quote: jobs per DAY, all searches
        combined. Same value as `recommended_records_per_day`, named for the
        question a person actually gets asked."""
        return self.recommended_records_per_day

    def lines(self) -> list[str]:
        """A short, plain-language explanation block for `discover`/`config`.
        Leads with the one-time assumed-plan disclosure when applicable —
        see `plan_is_assumed`. Quotes DAILY TOTALS first (the unit `api.limit`
        is in since v1.7), with the per-search split as a parenthetical."""
        out: list[str] = []
        if self.plan_is_assumed:
            out.append(
                "api.plan is not configured. Assuming the Free plan. Run "
                "`careeros start` or edit config.yaml if you're on a different plan."
            )
        plan_txt = self.plan or "unset"
        quota_txt = f"{self.quota} records/week" if self.quota else "unknown quota"
        goal_txt = (
            f", goal: {self.goal_interviews_per_week} interviews/week"
            if self.goal_interviews_per_week
            else ""
        )
        out.append(f"Quota guard — plan: {plan_txt} ({quota_txt}){goal_txt}")
        out.append(
            f"  Configured: {self.configured_records_per_day} jobs/day"
            f" ({self.configured_per_search} per search × {self.requests_per_run} search(es))"
            + (
                f", ~{self.configured_weekly_records}/week"
                f" ({round(100 * self.configured_weekly_records / self.quota)}% of quota)"
                if self.quota
                else ""
            )
        )
        if self.recommended_daily_total is not None:
            out.append(
                f"  Recommended: {self.recommended_daily_total} jobs/day"
                f" ({self.recommended_per_request} per search) to spread"
                f" {self.quota} across {self.active_days} active day(s)."
                " Set api.limit to change; CareerOS never changes it for you."
            )
        if self.over_quota:
            out.append(
                "  ⚠ Your configured limit is on track to exceed your weekly"
                " quota — you may hit the cap mid-week. Lower api.limit,"
                " lower api.active_days_per_week, or raise your plan."
            )
        if self.quota is None:
            out.append(
                "  (No weekly quota known for this plan — set api.plan or"
                " api.weekly_record_quota to enable quota warnings.)"
            )
        return out


def recommend(
    api_cfg: dict[str, Any],
    goals: dict[str, Any],
    requests_per_run: int,
    cli_default_limit: int = DEFAULT_LIMIT,
    limit_is_explicit: Optional[bool] = None,
) -> Recommendation:
    """Pure: compute the recommendation + whether the configured limit is on
    track to blow the weekly quota. No I/O, no mutation.

    `limit_is_explicit` says whether the user actually CHOSE a number (via
    `api.limit` or `discover --limit`). Left None it's derived from `api.limit`
    alone. It matters because when nothing is set, `discover` fetches the
    RECOMMENDATION, not `cli_default_limit` — so reporting the latter as
    "configured" would describe a limit that is not in effect, and could warn
    about blowing a quota the user was never going to blow."""
    requests_per_run = max(1, requests_per_run)
    quota = weekly_quota(api_cfg)
    active_days = _active_days(api_cfg)
    limit = effective_limit(api_cfg, cli_default_limit)
    if limit_is_explicit is None:
        cfg_limit = api_cfg.get("limit")
        limit_is_explicit = isinstance(cfg_limit, int) and cfg_limit > 0

    rec_per_request = rec_per_day = None
    if quota:
        rec_per_day = max(1, quota // active_days)
        rec_per_request = max(1, rec_per_day // requests_per_run)

    # v1.7: `limit` is a DAILY TOTAL across all searches, not a per-search
    # count — `discover` divides it across this candidate's tiers. It used to
    # be multiplied by requests_per_run here, which meant a 3-tier profile
    # quietly fetched 3x whatever number the user typed.
    configured_per_day = limit if limit_is_explicit or rec_per_day is None else rec_per_day
    configured_weekly = configured_per_day * active_days

    over = bool(quota) and configured_weekly > quota
    goal = (goals or {}).get("interviews_per_week")
    return Recommendation(
        plan=api_cfg.get("plan") or ("free" if quota else None),
        plan_is_assumed=plan_is_assumed(api_cfg),
        quota=quota,
        active_days=active_days,
        requests_per_run=requests_per_run,
        goal_interviews_per_week=goal if isinstance(goal, int) and goal > 0 else None,
        configured_limit=configured_per_day,
        configured_records_per_day=configured_per_day,
        configured_weekly_records=configured_weekly,
        recommended_per_request=rec_per_request,
        recommended_records_per_day=rec_per_day,
        over_quota=over,
    )


# ── rolling-week consumption tracking (the "prevent" half) ──────────────────

def week_start(today_iso: str) -> str:
    """ISO date of the Monday that begins `today_iso`'s week.

    Every OTHER pipeline command (normalize, dedupe, constraints, ...) treats
    `--date` as an opaque run-folder label, not necessarily a real calendar
    date (e.g. this repo's own QA runs are labeled "qa-p27-actor",
    "qa-hardening-01"). The guard is advisory/protective and must never crash
    `discover` just because a non-ISO label was used — so an unparseable
    `today_iso` falls back to the REAL current date for the week bucket
    (quota tracking still works; it just isn't backdated to a fictional date)."""
    try:
        d = date.fromisoformat(today_iso)
    except (ValueError, TypeError):
        d = date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def _budget_path(careeros_dir: Path) -> Path:
    return Path(careeros_dir) / BUDGET_FILENAME


def load_state(careeros_dir: Path, today_iso: str) -> dict[str, Any]:
    """Return this week's {week_start, records, requests}. If the stored week
    differs from `today_iso`'s week (or nothing is stored), start fresh at 0 —
    the counter rolls over automatically each Monday."""
    ws = week_start(today_iso)
    path = _budget_path(careeros_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
        if state.get("week_start") == ws:
            state.setdefault("records", 0)
            state.setdefault("requests", 0)
            return state
    return {"week_start": ws, "records": 0, "requests": 0}


def save_state(careeros_dir: Path, state: dict[str, Any]) -> None:
    _budget_path(careeros_dir).write_text(json.dumps(state))


def record_consumption(state: dict[str, Any], records: int, requests: int = 1) -> dict[str, Any]:
    state["records"] = int(state.get("records", 0)) + max(0, records)
    state["requests"] = int(state.get("requests", 0)) + max(0, requests)
    return state


# ── last-error diagnostics (P2.9) — local state only, no live API call ──────
# A separate file from BUDGET_FILENAME's rolling-week counter so its schema
# (asserted exactly in test_budget.py) never has to change.

LAST_ERROR_FILENAME = "last_discovery_error.json"


def _last_error_path(careeros_dir: Path) -> Path:
    return Path(careeros_dir) / LAST_ERROR_FILENAME


def record_last_error(careeros_dir: Path, date: str, message: str) -> None:
    """Persist the most recent `discover` failure so `careeros doctor` can
    show a plain-English diagnosis without making a live API call (which
    would spend quota on every doctor run). Overwritten on every failed
    attempt; cleared by `clear_last_error` on the next success."""
    Path(careeros_dir).mkdir(parents=True, exist_ok=True)
    _last_error_path(careeros_dir).write_text(json.dumps({"date": date, "message": message}))


def load_last_error(careeros_dir: Path) -> Optional[dict[str, Any]]:
    path = _last_error_path(careeros_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_last_error(careeros_dir: Path) -> None:
    path = _last_error_path(careeros_dir)
    if path.exists():
        path.unlink()


def guard_for(provider_config: dict[str, Any]) -> str:
    """Which budget/quota CAPABILITY a provider's own resolved config block
    declares — uniform, name-free enforcement (`discover`'s loop never
    branches on a provider's identity). Detected purely from which keys are
    PRESENT in the dict (not their values), because each provider config
    shape is structurally distinct by design:

    - "weekly": the block has a "plan" key — this is Fantastic Jobs' own
      `api:` block (the only config shape with that key), so this is its
      records/week quota guard.
    - "monthly": the block has a "max_monthly_budget_usd" key (even if its
      value is null) — a paid, metered source declaring a rolling-month spend
      ceiling in its own `providers.<name>` block. No provider ships with
      this today (v1.7 removed them all); the capability stays so a future
      paid source is spend-capped without touching `discover`.
    - "none": neither key present — an unmetered source, no guard to apply.
    """
    if "plan" in provider_config:
        return "weekly"
    if "max_monthly_budget_usd" in provider_config:
        return "monthly"
    return "none"


# ── rolling-month spend tracking (the monthly-budget capability) ────────────
# Mirrors the rolling-week functions above exactly, one level up (month
# instead of week) — a separate file/schema so neither counter's tests ever
# have to change for the other's sake. The `apify_*` names and filename are
# kept as-is: they hold real accumulated user state on disk, and renaming
# them would orphan it for no functional gain.

APIFY_BUDGET_FILENAME = "apify_budget.json"


def month_start(today_iso: str) -> str:
    """ISO date of the 1st of `today_iso`'s month — same non-ISO-label
    fallback as `week_start` (this repo's own QA runs use non-calendar date
    labels; never let that crash the guard)."""
    try:
        d = date.fromisoformat(today_iso)
    except (ValueError, TypeError):
        d = date.today()
    return d.replace(day=1).isoformat()


def _apify_budget_path(careeros_dir: Path) -> Path:
    return Path(careeros_dir) / APIFY_BUDGET_FILENAME


def load_apify_state(careeros_dir: Path, today_iso: str) -> dict[str, Any]:
    """Return this month's {month_start, spend_usd}. Rolls over automatically
    at the start of a new calendar month, same pattern as `load_state`."""
    ms = month_start(today_iso)
    path = _apify_budget_path(careeros_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
        if state.get("month_start") == ms:
            state.setdefault("spend_usd", 0.0)
            return state
    return {"month_start": ms, "spend_usd": 0.0}


def save_apify_state(careeros_dir: Path, state: dict[str, Any]) -> None:
    _apify_budget_path(careeros_dir).write_text(json.dumps(state))


def record_apify_spend(state: dict[str, Any], cost_usd: float) -> dict[str, Any]:
    state["spend_usd"] = round(float(state.get("spend_usd", 0.0)) + max(0.0, cost_usd), 6)
    return state


def check_apify_budget(
    state: dict[str, Any], max_monthly_budget_usd: Optional[float]
) -> tuple[bool, Optional[str]]:
    """Best-effort SOFT guard — decide whether a metered paid provider should
    run this call. Only PREVENTS when a budget is configured AND the
    recorded estimate has already reached it.

    HONEST LIMITATION (documented, not hidden): this can only be as accurate
    as the paid source's own reported per-run cost, which is typically a
    LOWER BOUND — real settled cost can be higher (charges often settle
    asynchronously). A paid provider should therefore ALSO pass a hard
    per-call cap enforced server-side as the reliable half of this guard —
    this function is the advisory, rolling-month half on top of that."""
    if not max_monthly_budget_usd:
        return True, None
    spent = float(state.get("spend_usd", 0.0))
    if spent >= max_monthly_budget_usd:
        return False, (
            f"Monthly spend budget reached (estimated): ${spent:.4f}/${max_monthly_budget_usd:.2f}"
            f" used since {state.get('month_start')}. This is a best-effort estimate (a paid "
            "source's reported usage can undercount real settled cost — also check its console). "
            "Raise max_monthly_budget_usd to continue, or wait for next month's reset."
            " (Override: run `discover` with --ignore-budget.)"
        )
    remaining = max_monthly_budget_usd - spent
    return True, (
        f"Monthly spend (estimated): ${spent:.4f}/${max_monthly_budget_usd:.2f} used this month"
        f" (${remaining:.4f} remaining)."
    )


def check_before_run(
    state: dict[str, Any], quota: Optional[int]
) -> tuple[bool, Optional[str]]:
    """Estimate whether this week's LOCAL counter looks exhausted. Returns
    (ok, message) — but `ok=False` is advisory only, never a hard skip: the
    caller (`discover.py`) prints the message as an early warning and still
    proceeds, because the live API's own response is the authoritative check
    (a real incident: a rotated-in fresh key was reported "exhausted" by this
    local, Monday-reset counter alone, which knows nothing about which key is
    configured). `--ignore-budget` does NOT apply to this weekly guard — it
    already never hard-skips — only to the separate monthly-spend guard
    (`check_apify_budget`), which does."""
    if not quota:
        return True, None
    used = int(state.get("records", 0))
    if used >= quota:
        return False, (
            f"Weekly discovery quota reached (local estimate): {used}/{quota}"
            f" records used since {state.get('week_start')}. Not skipping —"
            " verifying against the live API instead. Resets Monday, or raise"
            " api.weekly_record_quota / your plan if this keeps happening."
        )
    remaining = quota - used
    return True, (
        f"Weekly budget: {used}/{quota} records used this week"
        f" ({remaining} remaining before Monday reset)."
    )
