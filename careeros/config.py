"""Load and resolve .careeros/config.yaml.

Config is intentionally small: provider selection, active prompt versions
(the mechanism that makes prompt experimentation and cache invalidation the
same thing — see cache.py), the score threshold, and Sheets/Drive credentials
pointers. Nothing here should need a code change to tweak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    # THE discovery source model. Keys are provider ids (see
    # `providers/registry.py`); each value is that provider's own config
    # block — always at least `{"enabled": bool}`, plus whatever else that
    # provider declares it needs (a `limit`, a `max_monthly_budget_usd`,
    # etc.). `discover` runs every `enabled: true` entry, IN THIS ORDER
    # (Python/YAML preserve mapping order) — put your primary/most-trusted
    # source first, since `pipeline/dedupe.py` keeps the FIRST occurrence of
    # a duplicate role. `fantastic-jobs`'s own DETAILED config (transport,
    # endpoint, search filters, quota) intentionally stays in the separate
    # `api:` block below — `providers:` only controls which sources run.
    #
    # v1.7: one source. The seven others shipped through v1.6 were removed
    # on live evidence (see providers/README.md's "Evaluated and removed").
    # This dict is still the extension point: a new provider is one entry
    # here plus one line in the registry.
    "providers": {
        "fantastic-jobs": {"enabled": True},
    },
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
    # v1.3: how many enabled providers' fetch() calls `discover` runs
    # concurrently (each is a blocking network call, so this is real
    # wall-clock savings, not a correctness risk — budget/quota state is
    # always checked and recorded serially, only the network I/O itself
    # runs in parallel; merge order is always config order regardless of
    # completion order). Set to 1 to force the old fully-serial behavior
    # (useful for debugging a specific provider in isolation).
    "discovery_max_workers": 4,
    # Only stages actually read via cfg.prompts.get()/cfg.prompt_path() belong
    # here (gate, eval, resume, cover) — deep_report and apply are invoked by
    # skills/prep.md and skills/apply.md, which read their prompt files
    # directly by a hardcoded path, not through this config.
    "prompts": {
        "gate": "v1",
        "eval": "v2",
        "resume": "v4",
        "cover": "v2",
    },
    # Approximate FX to INR for salary constraint checks. A margin is applied
    # in constraints.py so a borderline conversion never wrongly hard-rejects.
    "fx_rates": {
        "INR": 1.0,
        "USD": 83.0,
        "EUR": 90.0,
        "GBP": 105.0,
    },
    # Optional Google Sheets tracker (P2.6). ADDITIVE only — local Markdown
    # under .careeros/runs/ and .careeros/results/ stays the source of truth
    # end to end; the Sheet is never read back by any pipeline stage. Default
    # OFF so a fresh OSS clone never needs a Google Cloud project to work —
    # `careeros init`/`start` offer Sheets+Drive or a local-only results
    # folder, see skills/start.md.
    "sheets": {
        "enabled": False,
        "spreadsheet_id": None,
        "credentials_path": None,
        "worksheet": "Jobs",
    },
    # The `fantastic-jobs` provider's config (careeros/providers/
    # fantastic_jobs.py) — the official Fantastic Jobs REST API.
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
        # pipeline/queryplan.py's segmented-discovery specs use these neutral
        # key names regardless of which provider is active, so a provider's
        # config block has to match them for the query-plan overlay in
        # fetch()'s `_merge_query` to keep working unchanged.
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
        # YOUR chosen daily job total from this source (v1.7 — this used to
        # mean records PER SEARCH, which reliably surprised people: a
        # 3-tier profile silently fetched 3x what they typed). `discover`
        # divides it evenly across however many search tiers this
        # candidate's profile generates. null -> the quota guard's
        # recommendation for the configured plan. The guard reads this and
        # warns if it will blow the weekly quota; it never rewrites it.
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
    threshold: float
    consider_threshold: float
    gate_batch_size: int
    description_max_chars: int
    discovery_max_workers: int = 4
    goals: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    sheets: dict[str, Any] = field(default_factory=dict)
    api: dict[str, Any] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)
    drive: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)

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


def _resolve_providers(user_providers: dict) -> dict:
    """v1.2 revision #2: the user's OWN `providers:` block is authoritative
    for both MEMBERSHIP and ORDER — it does NOT deep-merge against
    DEFAULT_CONFIG's `providers` dict at the top level. A plain `_deep_merge`
    would silently reintroduce every default provider the user didn't
    mention (since dict `out[k] = v` never removes an existing key) and
    would ALWAYS lose the user's ordering (Python dict key order is fixed at
    first insertion — DEFAULT_CONFIG's order wins regardless of what order
    the user's own keys appear in their file). Each individual provider's
    OWN sub-block still deep-merges against its DEFAULT_CONFIG counterpart
    (if one exists), so a provider listed with just `{enabled: true}` still
    picks up its shipped `limit`/budget defaults — only the overall
    set-and-order of WHICH providers are listed is the user's."""
    default_providers = DEFAULT_CONFIG.get("providers", {})
    resolved: dict = {}
    for name, block in user_providers.items():
        base_block = default_providers.get(name, {})
        resolved[name] = _deep_merge(base_block, block) if isinstance(block, dict) else block
    return resolved


def load_config(path: Path | str = ".careeros/config.yaml") -> Config:
    path = Path(path)
    merged = dict(DEFAULT_CONFIG)
    if path.exists():
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        if "providers" in user_cfg:
            resolved_providers = _resolve_providers(user_cfg["providers"])
            user_cfg_for_merge = {k: v for k, v in user_cfg.items() if k != "providers"}
            merged = _deep_merge(merged, user_cfg_for_merge)
            merged["providers"] = resolved_providers
        else:
            merged = _deep_merge(merged, user_cfg)
    return Config(
        threshold=merged["threshold"],
        consider_threshold=merged.get("consider_threshold", 3.5),
        gate_batch_size=merged["gate_batch_size"],
        description_max_chars=merged["description_max_chars"],
        discovery_max_workers=merged.get("discovery_max_workers", 4),
        goals=merged.get("goals", {}),
        prompts=merged["prompts"],
        sheets=merged["sheets"],
        api=merged["api"],
        fx_rates=merged["fx_rates"],
        drive=merged["drive"],
        providers=merged["providers"],
    )


def enabled_providers(cfg: Config) -> list[str]:
    """Provider ids to run, IN CONFIG ORDER (v1.2 revision #2 — Python/YAML
    dicts preserve insertion order, so this is exactly the order `providers:`
    lists them in config.yaml). Order matters: `pipeline/dedupe.py` keeps the
    FIRST occurrence of a duplicate role, so a source listed earlier wins."""
    return [name for name, block in (cfg.providers or {}).items() if (block or {}).get("enabled", False)]


def provider_config_block(cfg: Config, provider_name: str) -> dict[str, Any]:
    """The config dict a provider's OWN capability/limits are read from for
    `budget.guard_for` and the query-plan overlay. Guard-capability detection
    stays purely structural — it reads which KEYS a block declares, never the
    provider's name: Fantastic Jobs' block is `cfg.api` (has "plan" -> weekly
    quota guard); any other provider's is its own `cfg.providers[name]` entry
    (a "max_monthly_budget_usd" key there -> monthly spend guard; neither key
    -> no guard). A provider needing shared credentials reads them directly
    inside its own `fetch()`, not through this resolver; this function is for
    guard/limit purposes only."""
    if provider_name == "fantastic-jobs":
        return cfg.api
    return cfg.providers.get(provider_name, {}) or {}
