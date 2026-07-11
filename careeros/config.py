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
    # DEPRECATED single-provider key, kept only as a migration INPUT (v1.2).
    # A config file that still sets `provider:` (and has no `providers:`
    # block) is auto-upgraded in memory by `_migrate_legacy_provider` below
    # to `providers: {<that provider>: {enabled: true}}` — same behavior,
    # one provider only, nothing new silently enabled. Run
    # `careeros migrate-config` to write that upgrade to disk permanently.
    # Scheduled for removal in v2.0 — `providers:` (below) is the ONE model
    # going forward; nothing in this codebase reads `provider` except that
    # migration shim.
    "provider": "fantastic-jobs",
    # THE discovery source model (v1.2). Keys are provider ids (see
    # `providers/registry.py`); each value is that provider's own config
    # block — always at least `{"enabled": bool}`, plus whatever else that
    # provider declares it needs (a `limit`, a `max_monthly_budget_usd`,
    # etc.). `discover` runs every `enabled: true` entry, IN THIS ORDER
    # (Python/YAML preserve mapping order) — put your primary/most-trusted
    # source first, since `pipeline/dedupe.py` keeps the FIRST occurrence of
    # a duplicate role. `fantastic-jobs`'s own DETAILED config (transport,
    # endpoint, search filters, quota) intentionally stays in the separate
    # `api:` block below, unmoved — `providers:` only controls which sources
    # run, `api:` is Fantastic Jobs' existing, frozen, tested configuration.
    "providers": {
        # Core — on by default, no signup required.
        "fantastic-jobs": {"enabled": True},
        "remoteok": {"enabled": True},
        "we-work-remotely": {"enabled": True},
        # Paid sources — off by default (a fresh clone has no credential
        # configured; these cost real money per job). `limit` caps records
        # per fetch; `max_monthly_budget_usd: null` means "use the shared
        # apify.max_monthly_budget_usd account default" below rather than
        # its own separate sub-cap. See providers/README.md's "Shipped
        # providers" for the evidence-backed category (Optional/
        # Experimental/Not Recommended) behind each of these.
        "naukri": {"enabled": False, "limit": 100, "max_monthly_budget_usd": None},        # Optional
        "glassdoor": {"enabled": False, "limit": 100, "max_monthly_budget_usd": None},     # Optional
        "ziprecruiter": {"enabled": False, "limit": 100, "max_monthly_budget_usd": None},  # Optional
        "indeed": {"enabled": False, "limit": 100, "max_monthly_budget_usd": None},        # Experimental
        "foundit": {"enabled": False, "limit": 100, "max_monthly_budget_usd": None},       # Not Recommended
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
        # v1.2: shared account-level rolling-month spend ceiling across every
        # Apify-actor-based provider (fantastic-jobs-actor, naukri, foundit,
        # indeed, glassdoor, ziprecruiter — they all bill against the SAME
        # Apify account balance). A provider's own `max_monthly_budget_usd`
        # (in its `providers:` block) overrides this if set; null there means
        # "use this shared default." A modest starting default — raise it in
        # your own config once you know your real usage. See budget.py's
        # `check_apify_budget` for the honest "best-effort, not a precise
        # ceiling" caveat, and set a matching hard limit in the Apify
        # console too.
        "max_monthly_budget_usd": 10,
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
    provider: str  # DEPRECATED — see DEFAULT_CONFIG's comment. Use `providers` (below).
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
    providers: dict[str, Any] = field(default_factory=dict)
    # True when `provider:` was auto-upgraded from the deprecated single-key
    # input this load (see `_migrate_legacy_provider`) — `doctor`/`config`
    # use this to print the one-time deprecation notice pointing at
    # `careeros migrate-config`, without re-deriving it themselves.
    provider_migrated: bool = False

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


LEGACY_PROVIDER_DEPRECATION_NOTICE = (
    "config.yaml still uses the deprecated `provider:` key. Auto-upgraded for "
    "this run to `providers: {{{name}: {{enabled: true}}}}` — same single "
    "source, nothing new enabled. Run `careeros migrate-config` to write this "
    "to config.yaml permanently (this fallback is removed in v2.0)."
)


def _migrate_legacy_provider(raw_user_cfg: dict, merged: dict) -> tuple[dict, bool]:
    """v1.2: ONE config model (`providers:`) — this is the temporary,
    isolated upgrade-on-read path off the deprecated `provider:` key, not a
    second live config system. Only the RAW file as the user wrote it is
    consulted (never the already-defaulted `merged` dict), so this only
    triggers for a config that genuinely still uses the old key:

    - `providers:` present in the raw file -> new model already in use,
      nothing to migrate (even if a stale `provider:` line is also present).
    - `providers:` absent AND `provider:` present -> the deprecated input:
      upgrade in memory to `providers: {<that provider>: {enabled: true}}`,
      REPLACING (not merging with) the default `providers` dict — this
      preserves exact single-provider behavior; it must never silently
      enable the new free sources for someone who never asked for them.
    - Neither present -> nothing user-authored to migrate; the default
      `providers` dict (new-model defaults) already applies.

    Returns (possibly-updated merged dict, whether a migration happened).
    """
    if "providers" in raw_user_cfg:
        return merged, False
    if "provider" not in raw_user_cfg:
        return merged, False
    legacy_name = raw_user_cfg["provider"]
    merged = dict(merged)
    merged["providers"] = {legacy_name: {"enabled": True}}
    return merged, True


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
    (if one exists), so `providers: {naukri: {enabled: true}}` still picks
    up naukri's shipped `limit`/`max_monthly_budget_usd` defaults — only the
    overall set-and-order of WHICH providers are listed is the user's."""
    default_providers = DEFAULT_CONFIG.get("providers", {})
    resolved: dict = {}
    for name, block in user_providers.items():
        base_block = default_providers.get(name, {})
        resolved[name] = _deep_merge(base_block, block) if isinstance(block, dict) else block
    return resolved


def load_config(path: Path | str = ".careeros/config.yaml") -> Config:
    path = Path(path)
    merged = dict(DEFAULT_CONFIG)
    migrated = False
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
        merged, migrated = _migrate_legacy_provider(user_cfg, merged)
        if migrated:
            print(LEGACY_PROVIDER_DEPRECATION_NOTICE.format(name=user_cfg["provider"]))
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
        providers=merged["providers"],
        provider_migrated=migrated,
    )


def enabled_providers(cfg: Config) -> list[str]:
    """Provider ids to run, IN CONFIG ORDER (v1.2 revision #2 — Python/YAML
    dicts preserve insertion order, so this is exactly the order `providers:`
    lists them in config.yaml). Order matters: `pipeline/dedupe.py` keeps the
    FIRST occurrence of a duplicate role, so a source listed earlier wins."""
    return [name for name, block in (cfg.providers or {}).items() if (block or {}).get("enabled", False)]


def provider_config_block(cfg: Config, provider_name: str) -> dict[str, Any]:
    """The config dict a provider's OWN capability/limits are read from for
    `budget.guard_for` and the query-plan overlay — DELIBERATELY UNMERGED
    with `cfg.apify`, so guard-capability detection stays purely structural:
    Fantastic Jobs' block is `cfg.api` (has "plan" -> weekly guard); the
    legacy actor's is `cfg.apify` (has "max_monthly_budget_usd" -> monthly
    guard); every v1.2 Apify-based provider's is its own `cfg.providers[name]`
    entry (also has "max_monthly_budget_usd", even if null -> monthly guard);
    RemoteOK/We Work Remotely's blocks have neither key -> no guard. Merging
    `cfg.apify` in here would leak "max_monthly_budget_usd" into the FREE
    providers' resolved config too, wrongly guarding them — so a provider
    that needs shared Apify AUTH (`token_env`/`tokens_env`) reads `config.apify`
    directly inside its own `fetch()`, not through this resolver; this
    function is for guard/limit purposes only."""
    if provider_name == "fantastic-jobs":
        return cfg.api
    if provider_name == "fantastic-jobs-actor":
        return cfg.apify
    return cfg.providers.get(provider_name, {}) or {}
