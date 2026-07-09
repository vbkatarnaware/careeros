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
# plans are None on purpose: the guard stays purely informational until the
# user sets `api.weekly_record_quota` themselves, rather than inventing an
# unverified paid-tier number.
PLAN_WEEKLY_RECORD_QUOTA: dict[str, Optional[int]] = {
    "free": 500,
    "rapidapi": None,
    "paid": None,
    "enterprise": None,
}

DEFAULT_LIMIT = 100  # mirrors the `discover --limit` default in cli.py
BUDGET_FILENAME = "discovery_budget.json"


def weekly_quota(api_cfg: dict[str, Any]) -> Optional[int]:
    """The records/week ceiling: an explicit `api.weekly_record_quota` wins;
    otherwise it's derived from `api.plan`; otherwise None (guard is
    informational, no hard ceiling)."""
    explicit = api_cfg.get("weekly_record_quota")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    plan = api_cfg.get("plan")
    return PLAN_WEEKLY_RECORD_QUOTA.get(plan) if plan else None


def effective_limit(api_cfg: dict[str, Any], cli_default: int = DEFAULT_LIMIT) -> int:
    """The per-request record limit the user actually gets: their configured
    `api.limit` if set, else the CLI default. The guard reads this; it never
    rewrites it."""
    v = api_cfg.get("limit")
    return v if isinstance(v, int) and v > 0 else cli_default


def _active_days(api_cfg: dict[str, Any]) -> int:
    v = api_cfg.get("active_days_per_week")
    return v if isinstance(v, int) and 1 <= v <= 7 else 7


@dataclass
class Recommendation:
    plan: Optional[str]
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

    def lines(self) -> list[str]:
        """A short, plain-language explanation block for `discover`/`config`."""
        plan_txt = self.plan or "unset"
        quota_txt = f"{self.quota} records/week" if self.quota else "unknown quota"
        goal_txt = (
            f", goal: {self.goal_interviews_per_week} interviews/week"
            if self.goal_interviews_per_week
            else ""
        )
        out = [f"Quota guard — plan: {plan_txt} ({quota_txt}){goal_txt}"]
        out.append(
            f"  {self.requests_per_run} request(s)/run × limit {self.configured_limit}"
            f" ≈ {self.configured_records_per_day} records/day"
            + (
                f", ~{self.configured_weekly_records}/week"
                f" ({round(100 * self.configured_weekly_records / self.quota)}% of quota)"
                if self.quota
                else ""
            )
        )
        if self.recommended_per_request is not None:
            out.append(
                f"  Recommended: limit {self.recommended_per_request}/request"
                f" (≈ {self.recommended_records_per_day} records/day) to spread"
                f" {self.quota} across {self.active_days} active day(s)."
                " Set api.limit to change; CareerOS never changes it for you."
            )
        if self.over_quota:
            out.append(
                "  ⚠ Your configured limit is on track to exceed your weekly"
                " quota — you may hit the cap mid-week. Lower api.limit or"
                " raise your plan."
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
) -> Recommendation:
    """Pure: compute the recommendation + whether the configured limit is on
    track to blow the weekly quota. No I/O, no mutation."""
    requests_per_run = max(1, requests_per_run)
    quota = weekly_quota(api_cfg)
    active_days = _active_days(api_cfg)
    limit = effective_limit(api_cfg, cli_default_limit)

    configured_per_day = limit * requests_per_run
    configured_weekly = configured_per_day * active_days

    rec_per_request = rec_per_day = None
    if quota:
        rec_per_day = max(1, quota // active_days)
        rec_per_request = max(1, rec_per_day // requests_per_run)

    over = bool(quota) and configured_weekly > quota
    goal = (goals or {}).get("interviews_per_week")
    return Recommendation(
        plan=api_cfg.get("plan"),
        quota=quota,
        active_days=active_days,
        requests_per_run=requests_per_run,
        goal_interviews_per_week=goal if isinstance(goal, int) and goal > 0 else None,
        configured_limit=limit,
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


def check_before_run(
    state: dict[str, Any], quota: Optional[int]
) -> tuple[bool, Optional[str]]:
    """Decide whether discovery should proceed. Returns (ok, message).
    Only PREVENTS when a hard quota is known AND already fully consumed this
    week — the whole point is to stop before a mid-week 429, never to silently
    swallow a run when the ceiling is unknown."""
    if not quota:
        return True, None
    used = int(state.get("records", 0))
    if used >= quota:
        return False, (
            f"Weekly discovery quota reached: {used}/{quota} records used since"
            f" {state.get('week_start')}. Skipping discovery to avoid a hard"
            " rate-limit. Resets Monday, or raise api.weekly_record_quota /"
            " your plan. (Override: run `discover` with --ignore-budget.)"
        )
    remaining = quota - used
    return True, (
        f"Weekly budget: {used}/{quota} records used this week"
        f" ({remaining} remaining before Monday reset)."
    )
