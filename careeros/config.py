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
    "threshold": 4.0,
    "gate_batch_size": 50,
    "description_max_chars": 4000,
    "prompts": {
        "gate": "v1",
        "eval": "v2",
        "resume": "v1",
        "cover": "v1",
        "deep_report": "v1",
        "apply": "v1",
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
        # providers/fantastic_jobs.py's _iter_tokens().
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
        # "active-ats" (career sites/ATS — actor-parity scope) or
        # "active-jb" (+ LinkedIn/YC/Wellfound — future multi-source work,
        # not exercised by P2.7).
        "endpoint": "active-ats",
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
        # The Drive folder date-folders are created directly inside (i.e.
        # this IS your "CareerOS/" root — point it at a folder you already
        # created/shared, no extra nesting is added).
        "root_folder_id": None,
    },
}


@dataclass
class Config:
    provider: str
    threshold: float
    gate_batch_size: int
    description_max_chars: int
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
        gate_batch_size=merged["gate_batch_size"],
        description_max_chars=merged["description_max_chars"],
        prompts=merged["prompts"],
        sheets=merged["sheets"],
        apify=merged["apify"],
        api=merged["api"],
        fx_rates=merged["fx_rates"],
        drive=merged["drive"],
    )
