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
    # v1.9: how much of a job's `description` the Gate stage's _input_N.json
    # actually gets, separate from `description_max_chars` (which bounds
    # what normalize.py stores on the Job, and what `evaluate` reads later).
    # Measured 2026-08-08 across this project's own past gate runs: 86% of
    # gate tokens are the description field (2,403 of 2,789 chars/job), for a
    # stage that only needs to answer "could this plausibly be a fit" — not
    # the depth `evaluate` needs. Trimming the GATE's copy only (not the
    # stored Job, not what evaluate reads) cuts gate cost ~3x with no loss of
    # evaluation quality. Chosen small enough to matter, large enough that a
    # gate call still sees the substance of a role (the pharma
    # brand-management pattern this project's gate has caught before —
    # SUN PHARMA/DWD, see providers/README.md's "Evaluated and removed" era
    # notes — needs more than the title to catch).
    "gate_description_max_chars": 900,
    # 2026-08-10: fixing the geography reachability gap and enabling
    # global_remote raises the eligible-job count well past what a
    # single-employer's posting volume used to keep in check (measured: one
    # employer alone produced 64% of a day's discovery). Both caps below are
    # applied ONLY at the Gate-input boundary (careeros/cli/gate_evaluate.py
    # _gate_prepare) — they never touch `04_constraints/eligible.json` or
    # any provider/dedupe output, so nothing discovered is ever deleted or
    # permanently excluded. A job dropped by either cap simply isn't sent to
    # the AI Gate TODAY; since it was never gated/evaluated it's never
    # written to `.careeros/processed.jsonl`, so it remains a normal
    # candidate on every future run. Both default OFF (None) — an OSS user
    # with a small/well-scoped provider config may never need either.
    #
    # Per-company rotation cap: at most this many of one company's jobs are
    # sent to the Gate in a single run. Rotation state lives in
    # `.careeros/gate_rotation.json` (job_id -> last_shown_date) so a
    # flooding company's UNSEEN jobs are prioritized over ones already shown
    # — a company is never permanently capped at the same N jobs forever.
    "max_jobs_per_company_per_run": None,
    # Overall Gate volume ceiling, applied AFTER the per-company cap, via a
    # plain deterministic sort (work_mode_priority tier rank, then
    # role_priorities title-match rank, then posted_at recency) — no ML,
    # no embeddings, auditable in `05_gate/_selection_meta.json`. This is a
    # COST control, not a relevance filter: it exists because Gate/Evaluate
    # are genuine per-job AI calls (see AGENT_GUIDE.md's deterministic/
    # reasoning boundary), and volume growth from better geo recall
    # shouldn't translate 1:1 into AI spend without an explicit ceiling.
    "gate_max_jobs": None,
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
        # v2.2: "advanced" (default) sends ONE boolean `title_advanced`
        # expression; "basic" sends the older bare `title` param, which
        # pipeline/queryplan.py must then CHUNK into groups of 3.
        # Live-verified 2026-08-05: the bare param silently drops its
        # `-exclusion` clause at 4+ OR-terms, which had made
        # `title_exclusion_search` above a no-op for any profile with more
        # than 3 role_priorities. See providers/fantastic_jobs.py's
        # `build_title_advanced`.
        "title_mode": "advanced",
        "work_arrangement": [],
        "remove_agency": True,
        "has_salary": None,
        # v2.1: server-side years-of-experience bands to fetch. The API's
        # own `ai_experience_level` values are exactly "0-2" | "2-5" |
        # "5-10" | "10+" (measured: those four cover 100% of 311 real
        # records, no nulls). Verified live that the filter works but takes
        # ONE value per request, so each band listed here becomes its own
        # query per tier — extra requests, but NOT extra records, since the
        # daily total divides across however many queries exist.
        #
        # Ships EMPTY (= fetch every band, pre-v2.1 behavior) because the
        # right bands depend entirely on the candidate's own experience;
        # `careeros start` / the profile's deal_breakers.min_years_ok is
        # what should inform it. For a candidate with ~3 years,
        # ["0-2", "2-5"] is the reachable set and cuts roughly two thirds
        # of wasted quota.
        "experience_levels": [],
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
    # v2.0: anti-inflation checks run by `evaluate --finalize` (see
    # careeros/calibration.py) before an eval batch is cached. Every
    # threshold below has a code default, so this block only needs an entry
    # for whatever you're explicitly overriding — see that module's
    # docstring for why each default is what it is (the forensic
    # investigation that produced them is worth reading before changing
    # any of these).
    # v2.0: the (currently dormant — see careeros/cli/tune.py) query-tuning
    # loop. `enabled: false` is the honest default: the tuner independently
    # checks per-tier arming via careeros/pipeline/ledger.py regardless of
    # this flag, so setting it true doesn't make anything act before there's
    # enough data — it's a second, explicit switch, not the only one.
    "tuning": {
        "enabled": False,
        # Where the tuner writes its proposed api.* overrides. NEVER
        # .careeros/config.yaml itself — that file is full of hand-written
        # comments a yaml.safe_dump would destroy, and an overlay is the
        # structural guardrail that makes "the tuner can only touch these
        # three keys" enforced by the code that READS config, not by the
        # agent that writes it (see TUNING_OVERLAY_ALLOWLIST below).
        "overlay_path": ".careeros/tuning/overlay.yaml",
        # Fixed control tier, rotated at most annually by a human — a tuner
        # that could pick its own control would pick the one that flatters
        # it. Must be a real tier name from your query plan (see
        # queryplan.py's build_query_plan), not a profile.work_mode_priority
        # entry — those can consolidate (e.g. two onsite tiers -> "onsite").
        "control_tier": "india_remote",
        # Per-tier arming floors — ALL three must hold before the tuner may
        # act on that tier. See careeros/pipeline/ledger.py's compute_arming
        # for the sample-size arithmetic behind these numbers.
        "min_days": 28,
        "min_records": 400,
        "min_events": 8,
        # How often `careeros tune --check-due` is willing to even LOOK
        # (cheap, deterministic, zero AI cost) — separate from `cadence_days`
        # below, which is how often an actual CHANGE may be applied. Looking
        # weekly is safe; only ACTING needs the longer wait.
        "check_cadence_days": 7,
        # Exclusion-list decay: each title_exclusion_search entry is
        # individually defensible and collectively catastrophic, so entries
        # are capped and expire unless re-justified by a human.
        "max_exclusion_entries": 8,
        "exclusion_sunset_days": 90,
        # Max relative change to one tier's `tier_limits` entry per cycle.
        "tier_limit_max_delta_pct": 0.25,
        # Auto-revert window (completed runs after a change) and burn-in
        # (runs before ANY revert may fire) — see careeros/pipeline/
        # tuning.py's check_revert.
        "revert_window_runs": 10,
        "revert_burn_in_runs": 5,
    },
    "calibration": {
        "enabled": True,
        "arithmetic_tolerance": 0.051,
        "min_batch_n": 10,
        "clone_min_records": 4,
        "clone_min_companies": 3,
        "uniformity_max_stdev": 0.25,
        "uniformity_min_pass_rate": 0.90,
        "dimension_mode_share_warn": 0.60,
        "dimension_exempt": ["logistics"],
        "seniority_fit_max_with_marker": 3.5,
    },
}


@dataclass
class Config:
    threshold: float
    consider_threshold: float
    gate_batch_size: int
    description_max_chars: int
    gate_description_max_chars: int = 900
    max_jobs_per_company_per_run: int | None = None
    gate_max_jobs: int | None = None
    discovery_max_workers: int = 4
    goals: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    sheets: dict[str, Any] = field(default_factory=dict)
    api: dict[str, Any] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)
    drive: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    tuning: dict[str, Any] = field(default_factory=dict)

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


# v2.0: the ONLY api.* keys a tuning overlay may ever set — enforced here,
# at the code that READS config, so this is a guardrail an agent cannot
# violate by writing a differently-shaped overlay file (unlike every other
# tuner guardrail, which is a policy in careeros/pipeline/tuning.py that a
# buggy or malicious proposal COULD in principle bypass). Deliberately
# excludes anything that defines a tier's IDENTITY (work_arrangement,
# location_search, endpoint, time_range) — changing one of those means the
# tier's accumulated ledger history no longer describes the same tier.
TUNING_OVERLAY_ALLOWLIST = ("title_search", "title_exclusion_search", "tier_limits")


def _load_tuning_overlay(overlay_path: Path, allowlist: tuple[str, ...] = TUNING_OVERLAY_ALLOWLIST) -> dict:
    """Reads the tuning overlay (if present) and returns ONLY its
    allowlisted `api.*` keys, silently dropping anything else — same
    silent-drop convention `load_config` already applies to unknown
    top-level keys in the user's own config.yaml. `careeros tune --status`
    is where a human-facing warning about a rejected key belongs, not this
    loader (called from many contexts, including every test in this repo)."""
    if not overlay_path.exists():
        return {}
    with open(overlay_path) as f:
        raw = yaml.safe_load(f) or {}
    api_block = raw.get("api")
    if not isinstance(api_block, dict):
        return {}
    filtered = {k: v for k, v in api_block.items() if k in allowlist}
    return {"api": filtered} if filtered else {}


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

    # v2.0: the tuning overlay merges LAST, after the user's own config.yaml,
    # and ONLY its allowlisted api.* keys survive — see
    # TUNING_OVERLAY_ALLOWLIST. This is what lets the (currently dormant)
    # tuner change discovery queries without ever touching config.yaml
    # itself. Precedence: DEFAULT_CONFIG < config.yaml < overlay.
    tuning_cfg = merged.get("tuning") or DEFAULT_CONFIG["tuning"]
    overlay_path = Path(tuning_cfg.get("overlay_path", DEFAULT_CONFIG["tuning"]["overlay_path"]))
    overlay = _load_tuning_overlay(overlay_path)
    if overlay:
        merged = _deep_merge(merged, overlay)

    return Config(
        threshold=merged["threshold"],
        consider_threshold=merged.get("consider_threshold", 3.5),
        gate_batch_size=merged["gate_batch_size"],
        description_max_chars=merged["description_max_chars"],
        gate_description_max_chars=merged.get("gate_description_max_chars", 900),
        max_jobs_per_company_per_run=merged.get("max_jobs_per_company_per_run"),
        gate_max_jobs=merged.get("gate_max_jobs"),
        discovery_max_workers=merged.get("discovery_max_workers", 4),
        goals=merged.get("goals", {}),
        prompts=merged["prompts"],
        sheets=merged["sheets"],
        api=merged["api"],
        fx_rates=merged["fx_rates"],
        drive=merged["drive"],
        providers=merged["providers"],
        calibration=merged.get("calibration", DEFAULT_CONFIG["calibration"]),
        tuning=tuning_cfg,
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
