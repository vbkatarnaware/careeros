"""Load and resolve .careeros/config.yaml.

Config is intentionally small: provider selection, active prompt versions
(the mechanism that makes prompt experimentation and cache invalidation the
same thing — see cache.py), the score threshold, and Sheets/Apify credentials
pointers. Nothing here should need a code change to tweak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    # "fantastic-jobs" (REST, default, maintained) or "fantastic-jobs-actor"
    # (legacy Apify actor — reference/no-code backend, see
    # providers/legacy/fantastic_jobs_actor.py). See `providers` command.
    "provider": "fantastic-jobs",
    # Two-tier selection (P2.8). APPLY: score >= threshold -> full pipeline
    # (resume + cover + report + Drive + Sheet). CONSIDER: consider_threshold
    # <= score < threshold -> Sheet row only (score + reasons, NO AI artifacts,
    # NO Drive) so near-misses stay visible at zero extra AI cost. Below
    # consider_threshold -> omitted from the Sheet. Both are configurable.
    "threshold": 4.0,
    "consider_threshold": 3.5,
    # Your job-search targets. `interviews_per_week` is used only as CONTEXT
    # in the discovery quota-guard's recommendation (careeros/budget.py); it
    # never changes scoring. Left null until you set it (or `careeros start`
    # captures it). Real goal-vs-outcome calibration is P3, not v1.0.
    "goals": {
        "interviews_per_week": None,
    },
    "gate_batch_size": 50,
    "description_max_chars": 4000,
    # Only stages actually read via cfg.prompts.get()/cfg.prompt_path() belong
    # here (gate, eval, resume, cover) — deep_report and apply are invoked by
    # skills/prep.md and skills/apply.md, which read their prompt files
    # directly by a hardcoded path, not through this config.
    "prompts": {
        "gate": "v1",
        "eval": "v2",
        "resume": "v1",
        "cover": "v1",
    },
    # Approximate FX to INR for salary constraint checks. A margin is applied
    # in constraints.py so a borderline conversion never wrongly hard-rejects.
    "fx_rates": {
        "INR": 1.0,
        "USD": 83.0,
        "EUR": 90.0,
        "GBP": 105.0,
    },
    "sheets": {
        "spreadsheet_id": None,
        "credentials_path": None,
        "worksheet": "Jobs",
    },
    "apify": {
        # Single-token var (back-compat) — checked only if `tokens_env` (a
        # comma-separated rotation pool) isn't set or is exhausted.
        "token_env": "APIFY_TOKEN",
        # Comma-separated list of tokens (one per account) for automatic
        # rotation when a token's monthly budget is exhausted — see
        # providers/legacy/fantastic_jobs_actor.py's _iter_tokens().
        "tokens_env": "APIFY_TOKENS",
        "actor": "fantastic-jobs/career-site-job-listing-api",
        "time_range": "7d",
        # "profile" (default): derive one segmented query per
        # profile.work_mode_priority tier (see pipeline/queryplan.py) instead
        # of a single broad fetch — the discovery-benchmark-backed fix for a
        # single query yielding ~1 apply-worthy job per 40 fetched. "single"
        # restores the old one-query behavior driven by title_search/
        # location_search below (also the automatic fallback when a profile
        # has no work_mode_priority/role_priorities to derive queries from).
        "discovery_mode": "profile",
        "title_search": [],
        "location_search": [],
        "title_exclusion_search": [],
        "location_exclusion_search": [],
        # Actor's aiWorkArrangementFilter: any of "On-site"|"Hybrid"|
        # "Remote OK"|"Remote Solely". [] = no filter (fetch everything).
        "work_arrangement": [],
        # Actor's removeAgency — drop recruiting-agency postings server-side.
        "remove_agency": True,
        # Actor's hasSalary — True/False to filter, null to not filter.
        "has_salary": None,
        # Per-call Apify spend cap (USD). Caps a single `discover` call's
        # cost so one run can't silently exhaust a token's monthly budget —
        # the real failure mode hit during QA. None = no cap.
        "max_cost_usd": 1.0,
        # Optional per-work-mode-tier limit override, keyed by the same tier
        # strings as profile.work_mode_priority (e.g. {"global_remote": 15}).
        # Falls back to `discover --limit` for any tier not listed here.
        # Deliberately NOT pre-tuned with opinionated defaults (e.g. "lower
        # global_remote") — which tiers convert well is candidate-specific
        # (a different profile/role could see the opposite pattern), so
        # tuning this is left to each user's own observed run.json history,
        # not baked into the shared engine.
        "tier_limits": {},
    },
    # P2.7: the default `fantastic-jobs` provider's config (careeros/
    # providers/fantastic_jobs.py) — the official Fantastic Jobs REST API,
    # NOT the legacy Apify actor above (that block, `apify:`, is only read
    # by `provider: fantastic-jobs-actor`).
    "api": {
        # "direct" (developer.fantastic.jobs) or "rapidapi" (RapidAPI's
        # "Active Jobs DB"). NO DEFAULT — deliberately unset. Which transport
        # is cheaper/has a usable free tier is a config/commercial decision,
        # not an architectural one (see the P2.6/P2.7 architecture review);
        # `fetch()` fails fast with a clear message until you choose one.
        "transport": None,
        # transport: direct
        "base_url": "https://data.fantastic.jobs",
        "api_key_env": "FANTASTIC_API_KEY",
        # transport: rapidapi — verify the exact host/path against your own
        # RapidAPI dashboard; not live-verified during P2.7.
        "rapidapi_base_url": None,
        "rapidapi_host": "active-jobs-db.p.rapidapi.com",
        "rapidapi_key_env": "RAPIDAPI_KEY",
        # "both" (DEFAULT, P2.8-frozen): queries active-ats (career sites/
        # ATS) AND active-jb (+LinkedIn/YC/Wellfound) every run, merged —
        # the Final Discovery Acceptance Audit found the two sources score a
        # statistically identical ~8% >=4.0 rate but are 92% disjoint, so
        # "both" roughly doubles interview-worthy jobs per run at the same
        # quality (see .careeros/qa/acceptance_audit_report.md). Discovery
        # is frozen on this default; "active-ats" or "active-jb" alone
        # remain selectable (e.g. to source from one board) but are no longer
        # the recommended default.
        "endpoint": "both",
        # Per-endpoint split of each tier's record allocation when endpoint is
        # "both". Default (null) = EQUAL 50/50 (the frozen v1.0 default — "both"
        # shares the weekly quota, doesn't double it). Override with weights,
        # e.g. {"active-ats": 0.3, "active-jb": 0.7}, on a paid plan.
        "endpoint_allocation": None,
        # Everything below mirrors `apify:`'s search-filter keys exactly —
        # pipeline/queryplan.py's segmented-discovery specs use these same
        # neutral key names regardless of which provider is active, so this
        # provider's config block has to match them for the query-plan
        # overlay in fetch()'s `_merge_query` to keep working unchanged.
        "discovery_mode": "profile",
        "time_range": "7d",                    # -> time_frame: 1h | 24h | 7d | 6m
        "title_search": [],
        "location_search": [],
        "title_exclusion_search": [],
        "location_exclusion_search": [],
        "work_arrangement": [],
        "remove_agency": True,
        "has_salary": None,
        "tier_limits": {},
        # ── Quota guard (P2.8). CareerOS RECOMMENDS a daily discovery limit
        # and WARNS before you exhaust your provider quota, but never silently
        # overrides your choice — see careeros/budget.py. ──
        # Your Fantastic.jobs plan. Picks a default weekly record quota when
        # `weekly_record_quota` is left null: free -> 500, others -> unknown
        # (guard stays informational until you set the number yourself).
        "plan": None,                 # free | rapidapi | paid | enterprise | null
        # Records/week your plan allows. null -> derived from `plan`. Set this
        # explicitly for a paid/enterprise plan so the guard knows your ceiling.
        "weekly_record_quota": None,
        # Days/week you actually run discovery — the weekly quota is spread
        # across these when recommending a daily limit.
        "active_days_per_week": 7,
        # YOUR chosen per-request record limit. null -> falls back to the
        # `discover --limit` default (100). The guard reads this and warns if
        # it will blow the weekly quota; it never rewrites it.
        "limit": None,
    },
    # Optional Google Drive artifact backup (P2.6). ADDITIVE only — local
    # Markdown under .careeros/runs/ stays the source of truth end to end;
    # Drive is never read back by any pipeline stage. Uses an OAuth DESKTOP
    # client (not a service account), so uploads land in the configured
    # user's own personal Drive quota — appropriate for a personal daily-use
    # CLI. Default OFF so a fresh OSS clone never needs Drive to work.
    "drive": {
        "enabled": False,
        # Path to an OAuth 2.0 "Desktop app" client secret JSON (from Google
        # Cloud Console). NOT a service-account key.
        "client_secret_path": None,
        # Where the one-time browser-consent refresh token is cached after
        # the first successful auth — reused silently on every later run.
        "token_path": ".careeros/drive_token.json",
        # Flat layout (Phase 3, locked default): every Apply-tier job's
        # files (Resume.pdf, Cover Letter.pdf, Evaluation.md, Deep Report.md
        # if it exists) land directly in this ONE folder, named
        # "Company - Role - <Artifact>.<ext>" — no per-company, no per-job
        # subfolders. Point this at a folder you already created/shared.
        "root_folder_id": None,
        # Optional: group each day's uploads under a YYYY-MM-DD subfolder
        # inside root_folder_id instead of the flat root. Off by default.
        "date_subfolder": False,
    },
}


@dataclass
class Config:
    provider: str
    threshold: float
    consider_threshold: float
    gate_batch_size: int
    description_max_chars: int
    goals: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    sheets: dict[str, Any] = field(default_factory=dict)
    apify: dict[str, Any] = field(default_factory=dict)
    api: dict[str, Any] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)
    drive: dict[str, Any] = field(default_factory=dict)

    @property
    def careeros_dir(self) -> Path:
        return Path(".careeros")

    @property
    def runs_dir(self) -> Path:
        return self.careeros_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.careeros_dir / "cache"

    @property
    def profile_path(self) -> Path:
        return self.careeros_dir / "profile.yaml"

    def prompt_path(self, stage: str) -> Path:
        version = self.prompts.get(stage, "v1")
        return Path("prompts") / f"{stage}_{version}.md"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | str = ".careeros/config.yaml") -> Config:
    path = Path(path)
    merged = dict(DEFAULT_CONFIG)
    if path.exists():
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, user_cfg)
    return Config(
        provider=merged["provider"],
        threshold=merged["threshold"],
        consider_threshold=merged.get("consider_threshold", 3.5),
        gate_batch_size=merged["gate_batch_size"],
        description_max_chars=merged["description_max_chars"],
        goals=merged.get("goals", {}),
        prompts=merged["prompts"],
        sheets=merged["sheets"],
        apify=merged["apify"],
        api=merged["api"],
        fx_rates=merged["fx_rates"],
        drive=merged["drive"],
    )
