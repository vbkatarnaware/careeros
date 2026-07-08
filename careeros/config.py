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
    fx_rates: dict[str, float] = field(default_factory=dict)

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
        fx_rates=merged["fx_rates"],
    )
