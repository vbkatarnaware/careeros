# Changelog

All notable, user-visible changes to CareerOS are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/); versions
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Discovery-recall audit: measured the `ats-dataset` provider against the
real profile and found the dominant loss wasn't infrastructure, it was a
geography reachability bug plus one missing profile tier — 50 matched
jobs/day as configured, 537 once both were fixed.

### Fixed

- **`row_matches_geo` reachability gap** (`providers/ats_dataset.py`) —
  remote-tier matching (`india_remote`, `global_remote`) only checked the
  dataset's `is_remote` column when it was exactly `True`; that column is
  unpopulated (`None`) on ~53% of rows, 100% on greenhouse's, making those
  tiers structurally unreachable regardless of profile config. Added a
  text-based fallback (location/region/title only, never `description`,
  to avoid the documented false-positive class) for `is_remote is None`.
- **Silent truncation** — `ats-dataset`'s `matched[:limit]` now warns when
  the true match count exceeds `limit`, surfaced in `careeros discover`'s
  own output for every provider, not just this one.

### Added

- **`ats-watchlist` provider** (`providers/ats_watchlist.py`) — Layer 2A:
  a small, user-supplied list of specific companies
  (`.careeros/watchlist.yaml`) scraped live via `ats-scrapers`'s
  per-platform adapters, for companies the hosted snapshot doesn't carry
  at all. Deliberately targeted, not automated discovery — see that
  module's docstring for the measured evidence against building more.
  Deterministic ATS-migration detection (`history[]`, never a silent
  overwrite) and a 3-consecutive-failure staleness marker, no AI.
- **Optional reference registry** (`careeros/registry.py`,
  `careeros registry sync`/`find`/`stats`) — a local cache of the public
  ~80K-row company/tenant directory, for "do we already know this
  company" lookups before hand-adding a watchlist entry. Plain CSV
  (stdlib `csv`, zero extra deps) — benchmarked against Parquet at this
  row count and the ~0.15s difference wasn't worth a pandas/pyarrow
  dependency for an occasional lookup command. Never a dependency of
  `careeros discover` itself.
- **Per-company AI Gate fairness cap** (`max_jobs_per_company_per_run`,
  `careeros/cli/gate_evaluate.py`) — caps how many of one company's jobs
  reach the AI Gate in a single run, with rotation
  (`.careeros/gate_rotation.json`) so an over-represented employer's
  unseen postings get priority on the next run rather than the same N
  forever. Applies only at the Gate-input boundary — `eligible.json` is
  never touched, so nothing discovered is ever deleted.
- **`gate_max_jobs`** — an overall AI-cost ceiling applied after the
  per-company cap, via a plain deterministic sort (work-mode tier rank,
  then role-priority title match, then recency) — no ML, auditable in
  `05_gate/_selection_meta.json`.

### Added — v2.2: one discovery system, multiple company sources

Layer 1 (`ats-dataset`) and Layer 2A (`ats-watchlist`) already shared one
filter/scoring pipeline; what was missing was a way for Layer 2A to grow
itself instead of staying a hand-typed list forever, without hardcoding any
one user's role/geography. This closes that gap while keeping the two
layers' distinct acquisition modes (bulk snapshot vs. live per-company
scrape) exactly as they were — see `docs/ats-registry.md`'s "Automated
discovery (v2.2)" section for the full design writeup.

- **`Profile.preferences`** (`schemas/profile.schema.json`, `models.py`) —
  optional `industries`/`company_stage`/`target_companies`/
  `exclude_companies`/`exclude_industries` string arrays. Discovery-
  targeting signal only, read by nothing else — a profile without it loads
  exactly as before.
- **`careeros/ats_resolve.py`** — one deterministic company->ATS resolver
  (fetch the company's own careers page over plain `httpx`, extract
  embedded ATS links, resolve each through `ats_scrapers.resolve`'s own
  host table), reused by `watchlist discover`, `watchlist add`, and
  ATS-change recovery below. No browser, no search engine, never guesses:
  an unresolvable company returns `None`.
- **`careeros watchlist discover`** (`cli/registry_cmd.py`) — profile-driven
  automatic company discovery. Per candidate: skip if already on the
  watchlist (a reference-registry hit is noted, not skipped — registry
  presence isn't the same claim as Layer 1 job coverage, see the "Fixed"
  entry below), skip a recently-unresolved candidate within a 30-day TTL,
  resolve its ATS, live-scrape, require >=1 role-matching job posted
  within ~90 days (deliberately not geo-eligibility, which stays a
  downstream per-job constraint) before appending. `--max-add` is a
  code-enforced ceiling, never a quota — an agent proposing candidate
  names carries no more authority than a human typing `watchlist add` by
  hand; nothing is written without live
  validation against real job data.
- **ATS-change auto-recovery** (`providers/ats_watchlist.py`) — an entry
  that hits the existing 3-consecutive-not-found staleness threshold and
  has a `website` on file is re-resolved before being marked stale; a
  genuine migration (e.g. Lever -> Ashby) updates the stored mapping,
  appends one `history[]` record, and keeps scraping the same run instead
  of sitting dark until a human fixes it. `Job.make_id` keys on provider id
  + company + title + location, not the ATS platform, so a migration
  produces byte-identical job ids and existing dedupe suppresses re-
  surfacing with no new code.
- **`pending_unsupported_ats`** (`discovery_candidates.json`) — a company
  whose ATS resolves to a real platform this install has no adapter for is
  kept, not dropped: its ats/slug/website are retained, and every later
  `watchlist discover` run cheaply rechecks local adapter support (zero
  HTTP) and auto-promotes it the moment support exists, using the mapping
  already on file. Still unsupported after 30 days re-triggers a fresh
  careers-page resolve, in case the company itself changed ATS.
- **`welcometothejungle` and `amazon`** added to `ats-dataset`'s default 35
  enabled platforms (up from 33) — live-measured 2026-08-11: 1,274 and 632
  PM-titled postings respectively, with `amazon` alone adding ~9% to this
  dataset's total India PM reach. See `docs/ats-registry.md`'s "ATS
  platform coverage audit" for the full measurement, including which
  larger-by-company-count platforms were confirmed low-yield and correctly
  stay excluded.

### Fixed

- **`_attempt_ats_recovery` importing `MalformedJSONError` from the wrong
  module** (`providers/ats_watchlist.py`) — caught by the new recovery
  tests added alongside the feature above: the real exception lives in
  `ats_scrapers.fetch`, not `ats_scrapers.exceptions`, so any recovery
  attempt whose corrected mapping raised it would have crashed instead of
  falling back to `stale` as designed.
- **Reference-registry lookup used substring matching as a hard skip gate**
  (`cli/registry_cmd.py`) — `watchlist discover`/`watchlist add` treated
  any substring hit from `registry.find_company` (a convenience search,
  not an identity check) as "this company is already covered," so short
  names silently matched unrelated companies (`"Ramp"` → *Trampoline
  Systems*, `"Gem"` → *Gemini Data*) and were skipped or, for `watchlist
  add`, silently adopted the wrong ats/slug. `_resolve_via_registry` now
  requires an exact normalized name or slug match. Separately, a genuine
  registry hit is no longer a hard skip in `watchlist discover` — registry
  presence isn't the same claim as Layer 1 job coverage (this project's
  own Swiggy investigation, `docs/ats-registry.md`, is exactly that case:
  registered, but confirmed absent from its ATS's dataset slice) — it's
  now reported and the candidate still gets validated live.
- **Ledger reported same-source dedupe collapses as cross-layer
  duplicates** (`pipeline/ledger.py`'s `compute_run_source_stats`) —
  `dedupe_cross_location`'s dominant use is collapsing ONE provider's own
  multi-country reposts, not cross-layer overlap, but every such collapse
  was counted into `duplicate_of_other_source` regardless of whether the
  survivor's source actually differed. Measured live: 89 same-source
  `ats-dataset` collapses in a single run would have reported as "89
  duplicates of ats-dataset" on the `ats-dataset` entry itself. Now
  excluded when `dropped_source == survivor_source`.
- **Discovery quality bar ignored the user's own `title_exclusions`
  config** (`cli/registry_cmd.py`'s `_passes_quality_bar`) — hardcoded the
  default exclusion list instead of reading `providers.ats-watchlist.
  title_exclusions`, the same block `AtsWatchlistProvider.fetch` reads, so
  a company could be auto-added on a job title the user's own config would
  filter out once scraped daily.
- **`pending_unsupported_ats` candidates had no user-visible surface** —
  `watchlist list` now shows parked pending/unresolved discovery
  candidates from `discovery_candidates.json`; previously only visible via
  `cat`.

### Added — Zoho Recruit adapter

- **`careeros/providers/zoho_recruit.py`** — closes an India-priority ATS
  gap the same way `providers/darwinbox.py` did: a research pass (2026-08-11,
  see `docs/ats-registry.md`) found no reusable OSS Zoho Recruit scraper —
  the one candidate targeting it uses browser automation against a
  static-HTML assumption that can't work (Zoho's v2 career sites are
  client-rendered SPAs with zero job data in the initial HTML) — but found
  and verified a real public, unauthenticated JSON endpoint
  (`GET .../recruit/v2/public/Job_Openings?pagename=Careers`) by reading
  Zoho's own career-site JavaScript directly. Live-verified against 6
  independent tenants across both regional domains (`.zohorecruit.com` and
  `.zohorecruit.in`, including an India tenant). Wired into
  `ats_watchlist._scrape_entry` (`ats: zoho_recruit`) and `ats_resolve.py`'s
  embedded-link supplement, same pattern as darwinbox. **Known, measured
  limitation**: `Job_Description`/`Date_Opened` are per-tenant-configurable
  career-site fields — only 2 of the 6 tenants tested exposed them; a
  tenant that doesn't gets `posted_at: None` on every job, which
  `row_is_fresh()` correctly (not a bug) filters out. Title, company,
  location, apply URL, and employment type stay reliable regardless.

### Removed

- **`greenhouse`/`lever`/`ashby` providers and the SQLite ATS registry**
  (`providers/legacy/ats/`, `providers/legacy/discovery/`,
  `seeds/companies.yaml`, `.careeros/registry.db`) — every seeded board
  was permanently `status='unverified'` on this project's own checkout,
  and those providers only ever read `status='live'` boards, so they were
  structurally skip-only and had never returned a single job. Superseded
  by `ats-watchlist` above, which uses `ats-scrapers`' own 50+ maintained
  adapters instead of three hand-written ones.

## [2.0.0] - 2026-08-01

Two problems, found back to back on the same real data.

First: a run that looked like a discovery-quality collapse (1 Apply-tier job
from 95 fetched). Forensic comparison of stored eval scores against their
own rubric's weighted average found the queries hadn't gotten worse — two
prior "great" days had 13/25 and 13/14 evaluations whose score didn't match
the rubric that supposedly produced it (a 3x margin over the two genuinely
honest days' float-rounding noise), one rubric dimension repeating the same
value across eleven unrelated companies, and a weakness naming a seniority
mismatch a passing `seniority_fit` didn't reflect. The discovery layer
itself stayed frozen per `.careeros/ROADMAP.md` — this is evidence that
*conversion measurement* was broken, not that supply is the bottleneck,
which is the opposite of what would justify reopening it.

Second, once measurement could be trusted: the real cause of ~100
applications producing zero recruiter calls. Measured across 311 real
fetched jobs (2026-07-28..07-31), 66% asked for 5+ years of experience
against a ~3-year candidate — two thirds of every day's record quota spent
on jobs that could never convert, before any resume was read.

### Added

- **`careeros/calibration.py`** — five deterministic anti-inflation checks
  wired into `evaluate --finalize`: score-vs-rubric arithmetic (blocks),
  a rubric vector repeated across unrelated companies (blocks), a
  seniority weakness the rubric doesn't price in (blocks), score
  dispersion (warns), and per-dimension repetition (warns, `logistics`
  exempt by design). Reproduces the forensic finding exactly against the
  real historical eval batches (see `test_calibration.py`).
- **Golden set + `careeros calibrate`** (`--check`/`--probe`/`--score`/
  `--approve`) — an ~18-entry, human-reviewed set of known-correct scores
  (`.careeros/golden/`, `schemas/golden.schema.json`) for detecting scorer
  drift between runs/agents/models, sampled deterministically and never
  auto-sealed without an explicit human `sealed_by: "human"`.
- **`.careeros/processed.jsonl`** replaces `seen.jsonl` — written
  unconditionally by `threshold` for every job that reached a terminal
  decision this run (constraints-rejected, gate-dropped, evaluate-omitted,
  consider, apply), not just Apply/Consider jobs gated behind
  `sheets.enabled`. Fixes a real bug: local-mode runs (Sheets disabled)
  never recorded history at all, so every rejected job re-entered the
  pipeline and re-burned gate/eval tokens daily (56% same-run duplicates on
  the triggering run). TTL-aware (`load_processed_ids`), reads the legacy
  `first_seen` key with zero migration step.
- **`07_select/omitted.json`** — the 3.0–3.9 score band, previously
  computed by `partition_evals` and silently discarded.
- **`Job.seniority`** now populated from the Fantastic Jobs API's real
  `ai_experience_level` field (confirmed live: only `"0-2"/"2-5"/"5-10"/
  "10+"` ever appear), mapped conservatively onto the schema's enum
  (`10+` → `"lead"`, never `"principal"`/`"exec"` from this signal alone)
  — previously hardcoded to `None`.
- **Discovery tier provenance** — `Job.tiers`, set from `raw.json`'s new
  `provenance` field and unioned across cross-location dedupe collapses,
  so a job's discovery tier survives all the way to the learning ledger.
  Purely descriptive: excluded from `Job.content_hash()`, never read by
  gate/evaluate/constraints.
- **`careeros/pipeline/outcomes.py`** — derives *why* each evaluated job
  landed where it did via weighted contribution shortfall (`weight *
  (5 - value)`, not raw argmin, which would over-blame low-weight
  dimensions like `logistics`), with no eval-schema change. Written to
  `07_select/outcomes.json`.
- **`careeros ledger`** — aggregates recent runs into
  `.careeros/learning/{ledger.md,ledger.jsonl}`: per-tier records, gate-keep
  rate, constraints-rejection rate, reject-reason histogram, and mean
  rubric shortfall. Decision metrics (gate-keep/constraints-rejection rate)
  are kept structurally separate from descriptive ones (eval scores) — the
  former come from deterministic stages and the gate agent, the latter are
  reported but never a tuning objective. `.careeros/learning/quarantine.json`
  excludes the inflated 07-29/07-30 runs (and 07-12's known dedupe-count
  corruption) from aggregation.
- **`careeros sheets sync-outcomes`** — reads the Sheet's own `Status`
  column (Applied/Interview/Rejected/…, the one signal in this pipeline no
  agent can influence) into `.careeros/learning/outcomes.jsonl`.
- **`careeros tune`** (`--status`/`--propose`/`--apply`/`--revert`) — the
  query-tuning loop. **Ships dormant**: refuses to act on any tier until it
  clears ~4 weeks / 400 records / 8 apply-or-consider events. Writes only
  to an allowlisted config overlay (`.careeros/tuning/overlay.yaml`,
  `TUNING_OVERLAY_ALLOWLIST` = `title_search`/`title_exclusion_search`/
  `tier_limits` only, enforced by `load_config` itself, never
  `.careeros/config.yaml` directly). Guardrails: one change per cycle
  across all tiers, a fixed human-chosen control tier, a per-tier record
  floor, an exclusion list cap with mandatory sunset dates, and every
  proposal requires non-empty evidence. Auto-revert compares a changed
  tier against its own baseline AND the control tier (difference-in-
  differences) over a 10-run window, so a market-wide swing can't be
  blamed on the one tier that happened to change that cycle; a reverted
  change is quarantined and never silently re-applied.
- `AGENTS.md` (Codex's conventional onboarding redirect — `CLAUDE.md`/
  `GEMINI.md` already existed, this was the missing one).
- `careeros doctor` now flags unrecognized top-level `config.yaml` keys
  (previously silently dropped with no warning anywhere — a typo like
  `calibraton:` just quietly never took effect).
- **Experience-band discovery filter** (`api.experience_levels` in
  `config.yaml`) — the Fantastic Jobs API's `ai_experience_level` field
  ("0-2"/"2-5"/"5-10"/"10+") works as a server-side query filter, verified
  live. It accepts one value per request, so each configured band fans a
  search tier out into its own query. Set to `["0-2", "2-5"]`: 3 tiers x 2
  bands = 6 queries x 16 records = the same ~100/day, but every record is
  now in reach. Deliberately did not also add a title exclusion for
  Staff/Principal/Director/VP — measured, all 45 such jobs were already in
  the 5+ bands, so the experience filter removes them with zero added false
  positives.
- **`careeros verify-live`** — new stage between `threshold` and
  `artifacts`. Fetches the real posting for each Apply-tier job (1-3/day)
  and checks years-of-experience and location/work-arrangement against the
  profile, flagging (never auto-rejecting) a mismatch. Built after two
  same-day misses where the pipeline scored honestly against wrong provider
  metadata: MDOTM labelled remote but was Milan-hybrid, AU Small Finance
  Bank labelled "2-5 years" while the live posting asked for 7. Both are
  pinned as regression tests.

### Fixed

- **Same-date re-run contamination in `evaluate --finalize`.** Re-running
  discovery for a date already run today left stale eval files in
  `06_evaluate/` whose ids no longer existed in that run's
  `02_normalize/jobs.json`; the old blanket file glob folded them back in
  as if they were legitimate cache hits, so `run.json`'s `count_out` drifted
  on every re-run of `--finalize`. Now scoped to today's actual gate-kept
  ids.
- **`sheets append` crashed (`KeyError`) on a same-date re-run** when a
  cached Consider/Apply eval's job no longer existed in the current
  `jobs.json` — now skipped with a warning instead of crashing the whole
  append.
- **`dedupe --against-sheet` called the live Sheets API even with
  `sheets.enabled: false`**, relying on a `RuntimeError` to no-op. Now
  checks the flag directly.
- Dedupe drops now carry a `_drop_reason` (`in-run`/`cross-location`/
  `history`/`sheet`) — previously a flat, unlabeled concatenation.
- **Salary floor now compares exactly for same-currency postings.** The
  0.9x margin in `constraints.py` exists to absorb FX conversion error, but
  was also being applied when no conversion happened, silently lowering a
  stated floor by 10% (12 LPA rejected only below 10.8, letting an 11 LPA
  posting through). Same-currency amounts now compare exactly; converted
  amounts keep the margin.
- **`deal_breakers.min_years_ok`: 4 -> 3** in `.careeros/profile.yaml`.
  Actual full-time experience is ~3.0y; the inflated 4 was quietly boosting
  `seniority_fit` on every evaluation. `profile.version` bumped 9 -> 10.
- **`comp.floor_lpa`: 15 -> 12.**

667 tests passing.

## [1.8.0] - 2026-07-28

Everything below shipped locally across 1.7.0–1.7.2 but never reached
GitHub. Before tagging it as one release, a three-pass pre-release audit
(code, tests/CI, docs) went over the whole tree — it found two genuine
runtime bugs, a dead dependency, and a real fabrication hole, all fixed
here alongside the docs it also found stale.

### Fixed — bugs found in the pre-release audit

- **`prep` and `apply` pointed at the wrong stage directory.** Evaluations
  are written to `06_evaluate/`; `05_` is the gate stage. Four files that
  ship in this release said `05_evaluate/` — `skills/apply.md`,
  `skills/prep.md`, and the two active prompts `prompts/deep_report_v1.md`
  and `prompts/apply_v1.md`. Followed literally, both commands found no
  evaluation and stopped. Nothing caught it because no test exercises the
  skill markdown at all.
- **A corrupt cache file crashed the pipeline.** `Cache.get`
  (`careeros/cache.py`) read with a bare `json.load()` and no `try/except`;
  its sibling `budget.load_state` wraps the identical read and is tested for
  it. A truncated file from an interrupted write now degrades to a cache
  miss instead of crashing every subsequent run until someone deletes it by
  hand. `Cache.put` also now writes via a temp file + `os.replace()` so an
  interrupted write can no longer leave a truncated file in the first place.
- **A hallucinated skill shipped clean.** `verify_resume_facts`
  (`careeros/lint.py`) validated bullet numbers, company names, and project
  names against `profile.yaml` — but never read `resume_json["skills"]`,
  despite its own docstring listing `skills` in the payload shape it checks.
  A skill the JD asked for that the candidate has never used rendered
  without any mechanical check catching it. Now checked the same way
  `companies`/`projects` already were.
- **The Playwright-Missing degradation path always skipped in CI.**
  `test_fetch_via_playwright_returns_none_when_not_installed` relied on a
  REAL absent import and skipped itself whenever Playwright was installed —
  and CI (`ci.yml`) always installs the `[apply]` extra, so this test never
  actually ran there. Fixed by forcing the `ImportError` deterministically
  (`sys.modules["playwright.sync_api"] = None`) instead of depending on the
  environment; it now runs, and means something, everywhere.
- **`careeros --version` had been silently wrong for three releases.**
  `__version__` reads back the installed package's `dist-info`, generated at
  `pip install -e .` time — not `pyproject.toml` directly. The installed
  metadata was still stamped `1.6.0` while `pyproject.toml` said `1.7.2`;
  nothing had re-run the editable install since the `1.6.0` release. Added
  a test asserting the two agree, and re-ran the install to fix it for real.
- A Workday-hosted posting (DBS Bank) returned only 132 characters of page
  text — literally the word "Loading" plus nav chrome — because
  `_fetch_via_playwright`'s fixed 1.5s buffer wasn't enough for the SPA to
  finish rendering, and that got misclassified downstream as "no essay
  questions" instead of "never saw the real form." One bounded retry
  (`wait_for_load_state("networkidle", timeout=5000)` as a fallback-only
  second chance, never the primary wait) fixes it — verified live against
  the actual posting: 132 characters before, 6,641 after, the real job
  description. General fix, not Workday-specific; the bug class (a fixed
  wait too short for client-side rendering) isn't unique to one ATS.

### Removed

- **`apify-client` dropped from core dependencies.** Zero imports anywhere
  in the package or tests; its own `pyproject.toml` comment pointed at
  `careeros/providers/legacy/fantastic_jobs_actor.py`, deleted in v1.7.0.
  Every install pulled it for nothing.

### Changed

- **Sheet Status column: color-coded, and the value set finally matches the
  funnel.** Every status rendered identically before this — a 38-row sheet
  gave no visual sense of where anything stood. `STATUS_OPTIONS`
  (`careeros/sheets.py`) drops `Offer` and `Ongoing / In Process` (states
  this funnel doesn't use) and adds `Expired` (a posting that closed before
  it was applied to), then each of the seven values gets its own
  conditional-format fill: grey for Not Applied, green for Applied, blue for
  Received Call, amber for Interview, purple for After Interview, red for
  Rejected, dark grey for Expired — chosen so the column scans by
  temperature (in-flight vs. closed) rather than requiring you to read every
  cell. Existing rows already holding a removed value keep their text; the
  pipeline never rewrites a human-edited cell, so Sheets just flags them as
  outside the dropdown until you update them yourself.
- **Cover letters now read the job posting (`prompts/cover_v2.md`, now the
  default).** Measured across the 18 letters generated on 2026-07-26, every
  opening was interchangeable between companies — *"Marvin's focus on
  AI-driven B2B SaaS solutions caught my attention," "Mercury's focus on
  providing financial tools ... caught my attention."* Root cause: v1 built
  the letter from the eval's `company_summary`, and 67 of that run's 149
  evals (45%) have a thin or generic one (*"Company details are limited but
  the role focuses on core PM execution"*) — while the job's own full
  `description` was passed to this stage the entire time and the prompt
  never once told the model to read it. v2 mines the JD for a concrete hook
  first (a named product, a stated problem, team shape, a specific tool),
  adds real company/domain research with a hard no-fabrication rule (if it
  can't be verified, don't write it — silence beats a guess), and picks an
  entry angle (problem-first, parallel, question, wedge) from what the
  evidence actually supports instead of running one fixed shape every time.
  A list of the observed filler phrases (*"caught my attention," "is a
  compelling fit," "I look forward to hearing from you,"* etc.) is banned
  outright. `cover_v1.md` stays on disk for rollback via `prompts.cover`.
- **Cover letter PDF gets real letter furniture**
  (`careeros/templates/cover.typ`). It was a centered name header over
  justified prose with no date, recipient, salutation, or sign-off — it
  didn't read as correspondence. `render_cover_pdf()` now optionally accepts
  the `Job` (for the company/recipient block, and a named contact when the
  posting has one) and the run's `date` (formatted as "27 July 2026," never
  `date.today()` — re-rendering an old run must reproduce that run's letter,
  not today's). Every added field is genuinely optional: an omitted one
  skips its block in the template rather than printing a blank line, so the
  existing call signature keeps working unchanged.

### Docs

The audit's doc pass corrected drift the code changes above (and v1.7.0's
provider collapse) had left behind: `README.md`'s Status-value list and
prompt-version references, `AGENT_GUIDE.md`'s dead `token_env`/`tokens_env`
mention, `skills/daily.md`/`skills/apply.md` instructing an `apify:` block
and an Apify doctor check that no longer exist, a handful of stale
identifiers in code comments (`_build_run_input`, `stage_output_path`,
`_discover_one_provider`, `cli.py` pointing at what's now the `careeros/cli/`
package), and the website — which still advertised eight removed providers
and shipped two example commands that now error outright
(`docs/examples.astro`, `docs/developer-guide.astro`).

## [1.7.1] - 2026-07-26

### Changed

- **Resume skills section: a floor, then fill (`prompts/resume_v4.md`, now
  the default).** Measured across the 18 resumes generated on 2026-07-26,
  `resume_v3` emitted a median of **9 skills out of the profile's 65**, never
  once surfaced the Tools category, and surfaced Growth once — so tools a
  recruiter searches for by name (Jira, Notion, Figma, GA4, Looker Studio)
  were present in `profile.yaml` but structurally unreachable on the page.
  Two causes, both fixed: a JD's `ats_keywords` never contain a category tag
  like `tools`, so v3's tag-only matching could not reach those entries; and
  with no target count and no space-awareness clause, the model minimized.
  v4 selects in three passes — every `visibility: headline` skill as a
  non-negotiable floor, then supporting skills matched by tag **or by name
  appearing in the JD**, then fill to a 22-28 item target band. Category
  labels are now a fixed set (Product, Growth, AI & Automation, FinTech,
  Analytics, Tools); v3 drifted across nine variants including "Product
  Operations", "Data & Research", and "Automation", which weakened keyword
  matching for no gain.
- **Resume bullets front-load the fact.** v4 adds a compression rule: cut
  leading process filler so a bullet reaches its first number sooner ("Owned
  the full product lifecycle (discovery, PRDs, roadmap, MVP launch, and GTM)
  for…" spends fourteen words before its first metric). Explicitly a
  compression rule, not a truncation one — every number, entity, and
  technology still survives, and no metric is ever dropped to save a line.
  Bullet density is the asset; the goal is fewer wasted words per fact.
- `resume_v3.md` stays on disk for rollback — set `prompts.resume: v3` in
  `.careeros/config.yaml`. Switching versions invalidates the artifact cache
  (the prompt version is part of the cache key), so resumes regenerate.

### Fixed

- Restored the `## [1.6.0]` CHANGELOG heading, which the 1.7.0 entry was
  written over — that release's body had been left orphaned under 1.7.0.
- `careeros/cli/artifacts.py` fell back to `"v2"` when `prompts.resume` was
  unset, two versions behind the shipped default.
- Documentation and schema comments referenced `prompts/resume_v2.md` by
  filename in nine places; they now point at "the active resume prompt" so
  they stop going stale on every prompt version bump.

## [1.7.0] - 2026-07-26

Discovery collapses to one source, and `api.limit` starts meaning what a
person thinks it means. Both changes come out of two weeks of real daily use.

### Changed — BREAKING

- **`api.limit` is now a DAILY TOTAL, not a per-search count.** `discover`
  divides it evenly across however many query tiers your
  `profile.work_mode_priority` generates (`pipeline/queryplan.py`), so
  `limit: 120` on a 3-tier profile fetches 40 per search, 120/day total.
  It previously meant records *per search*, which reliably surprised people:
  the same `120` quietly fetched 360/day and could exhaust a 500/week free
  quota in under two days. `careeros/budget.py`'s `recommend()` no longer
  multiplies by the tier count; it gained `recommended_daily_total` and
  `configured_per_search`, and its printed guard block now leads with daily
  totals and shows the per-search split in parentheses. `--limit` on
  `discover` is a daily total too. **If you had set `api.limit`, divide your
  old value by your tier count to keep the same behaviour** — or leave it
  null and take the guard's recommendation.
  - A daily total smaller than your tier count floors at 1 per search and
    warns, rather than silently dropping tiers.
- **`careeros doctor`'s "Discovery limit" check** compares daily totals and
  spells out the split (`current=120 jobs/day (40 per search × 3 search(es))`),
  so tier drift is visible: add a `work_mode_priority` entry and the daily
  total holds while the per-search number drops.
- **`skills/start.md` Step 5 rewritten.** It asked for a *"daily discovery
  limit"* and wrote the answer straight to the then-per-search `api.limit` —
  the bug above, in the onboarding path. It now asks **"How many jobs per day
  do you want from Fantastic Jobs?"** as a loop over enabled sources, showing
  each source's search count and recommendation derived from the candidate's
  own profile and plan. Tier labels and counts are never hardcoded, so a
  remote-only candidate correctly sees one search, not three.

### Removed — BREAKING

- **Seven discovery providers and the legacy Apify actor.** `remoteok`,
  `we-work-remotely`, `glassdoor`, `ziprecruiter`, `naukri`, `foundit`,
  `indeed`, and `fantastic-jobs-actor` are gone — source files, tests,
  registry entries, and config defaults. `fantastic-jobs` is the only
  registered provider. Evidence from the 2026-07-26 run (524 discovered, 18
  selected): Fantastic Jobs produced 9 of the 18 selected jobs from 13% of
  raw volume at $0, while ZipRecruiter took 60% of the spend and 99% of the
  wall-clock for 1 selected job, and RemoteOK converted nothing from 100
  jobs. Full per-source numbers are preserved in
  `careeros/providers/README.md`'s new "Evaluated and removed" section, along
  with the "don't judge cost from a `--limit 3` trial" lesson. All are
  recoverable from git history.
  - The multi-provider machinery is untouched — `providers:` ordering,
    concurrent fetch, per-provider guards and reporting all still work. This
    is a decision about which sources are worth running, not an architecture
    change. `providers/README.md` gained an "Adding a provider" section.
- **The `apify:` config block**, `Config.apify`, and
  `careeros/providers/_apify_actor_common.py` (shared `run_actor`, token-pool
  rotation, `validate_apify_token`). `doctor` no longer has Apify credential,
  token-pool, or budget checks. `.careeros/apify_tokens.json` and its
  `budget.py` helpers (`token_fingerprint`, `load_apify_tokens_state`,
  `mark_token_exhausted`, `is_token_exhausted`) are gone — that cache existed
  only for the deleted `run_actor`.
  - **Kept deliberately:** `budget.guard_for`'s `"monthly"` capability and
    the rolling-month spend guard (`check_apify_budget`, `load_apify_state`,
    `record_apify_spend`). No provider declares it today, but it is keyed off
    a config *shape* — a `max_monthly_budget_usd` key — not a provider name,
    so a future paid source gets spend-capping without touching `discover`.
    `run.json`'s `apify_cost_usd*` field names and `apify_budget.json` keep
    their names: they hold real accumulated history that a rename would
    orphan.
- **The deprecated `provider:` config key** and its whole upgrade-on-read
  path: `_migrate_legacy_provider`, `LEGACY_PROVIDER_DEPRECATION_NOTICE`,
  `Config.provider`, `Config.provider_migrated`, and the hidden
  `careeros migrate-config` command. The shim existed to upgrade configs
  naming providers that no longer exist. Use `providers:`.
- `careeros/providers/_apify_common.py` is renamed to `_field_mapping.py` —
  its candidate-key lists (`TITLE_KEYS`, `pick_field`, …) were never
  Apify-specific, and no Apify provider remains to justify the name.

## [1.6.0] - 2026-07-14

A senior-UX pass for the open-source launch: a zero-Google local-first mode,
a per-job command for full on-demand treatment of any single job, and a
cleaner CLI surface — no behavior change to the existing Sheets/Drive path.

### Added

- **Local-first mode.** Google Sheets is now optional (`sheets.enabled`,
  default `false`), mirroring Drive's existing optional pattern. A fresh
  clone with only a Fantastic Jobs key (no Google account at all) now runs
  `careeros doctor` fully green and gets full value from `daily`: every run
  writes a stable, human-readable digest to
  `.careeros/results/<date>/summary.md` (+ a `.careeros/results/latest/`
  pointer), with relative links straight to each Apply job's rendered
  resume/cover PDF (`careeros/report.py`'s `render_summary` gained an
  `artifact_links` param for this). `sheets append` and `publish` are both
  graceful no-ops (not errors) when their target is disabled — `publish`
  now also handles the Drive-enabled-but-Sheets-disabled combination
  correctly instead of always assuming both are configured together.
  `skills/start.md` gained a "Google Sheets/Drive, or local-only?" step; the
  onboarding "Next steps" printed by `careeros init` no longer implies
  Sheets is required.
- **`careeros job <job-id>`** (new `skills/job.md`) — give ONE job the full
  Apply-tier treatment (resume, cover, Level-1 report, application answers,
  auto-published to Drive/Sheet if configured, local digest refreshed
  either way) regardless of its actual score, without waiting for a
  re-run. Its enabler: `careeros artifacts --prepare/--finalize` gained a
  `--job-id` filter that reads a single job straight from
  `06_evaluate/<job-id>.json` (any tier, not just the date's Apply-tier
  `selected.json` batch).
- `careeros --help` now groups commands into **Setup**, **Daily**,
  **Per-job**, and **Advanced** panels (Typer `rich_help_panel`) and hides
  ~15 internal pipeline-stage/dev commands (`discover`, `gate`, `evaluate`,
  `artifacts`, `threshold`, `sheets *`, and similar) from the top-level
  listing — still fully runnable standalone for debugging one stage, just
  no longer clutter for a first-time user. A one-line banner ("CareerOS's
  AI steps run inside your coding CLI...") now appears on `--help` and
  every host-CLI-skill stub's message, with consistent WHY → HOW → Playbook
  wording across `daily`/`start`/`prep`/`apply <job-id>`/`job <job-id>`.

### Changed

- `careeros/cli.py` (2833 lines) split into the `careeros/cli/` package,
  one module per concern (`setup`, `doctor`, `discover`, `pipeline`,
  `gate_evaluate`, `artifacts`, `apply_stage`, `perjob`, `reports`,
  `drive`, `sheets_cmds`, `lint_verify`, `stubs`) sharing one Typer `app` —
  a pure mechanical move, no behavior change; every existing entry point
  (`careeros.cli.<name>`) still resolves the same way.
- README restructured quickstart-first: a 60-second, zero-Google quickstart
  now opens the file, ahead of the architecture/philosophy sections that
  used to lead; local-first mode and `job <job-id>` are reflected
  throughout.
- `docs/google-setup.md` and `templates/config.example.yaml` updated to
  reflect Sheets being optional (`sheets.enabled: true` now required to
  activate it, same as `drive.enabled`).

### Fixed

- CI now installs the `[resume]` extra (not `[dev]` alone), so the Typst
  PDF-rendering test path actually exercises Typst in CI instead of
  silently falling back.

## [1.5.0] - 2026-07-14

### Added

- **Resume design overhaul.** `careeros/templates/resume.typ` is a full
  rewrite, adapted from the community `guided-resume-starter-cgc` Typst
  package (Unlicense): small-caps section headers with full-width rules,
  a single one-line contact row (real handle text, not generic "LinkedIn"/
  "GitHub" link labels — some ATS/recruiter tools pattern-match a structured
  profile URL directly out of the visible text, which a generic label
  defeats), `role | company` and `date | location` pairs each on one grid
  row so a long title can never collide with the date column, and
  paragraph-style Selected Product Initiatives. Font switched to
  **New Computer Modern** — the same Computer Modern lineage as LaTeX's
  Latin Modern, bundled inside Typst's own compiler binary, so no font file
  ships with this package at all (see Removed).
- **Company selection**, a new tailoring zone parallel to the existing
  project selection: `resume.json` gained an optional `companies` field (see
  `schemas/resume.schema.json`, `prompts/resume_v2.md`'s new "Select
  companies" step) so a JD can show only the companies relevant to it —
  e.g. dropping an internship when a role wants post-college-only
  experience — instead of every `profile.yaml` company always appearing.
  Omitting the field fails-soft to every company, matching prior behavior.
- **Project taglines.** Each `profile.yaml` project gained a `tagline`
  field (a short, punchy one-liner grounded in the project's own README —
  e.g. Rizent AI: "Your AI co-founder for fundraising.") that now renders
  automatically under the project name on every resume. Canonical fact, not
  AI-authored: `resume.json` still only selects *which* projects appear.
- **Page-density auto-fit, rebuilt.** `_FIT_TIERS` (`careeros/typst_render.py`)
  now scales font size and line leading together across a much finer
  ladder (11.0pt down to 7.8pt), so the renderer settles on the largest
  size that still fits one page — filling it edge-to-edge with even,
  comfortable spacing — instead of jumping straight from "overflows" to
  "leaves visible blank space."

### Changed

- Cover letter (`careeros/templates/cover.typ`) font switched to New
  Computer Modern too, matching the resume, per its own long-standing
  "matches the resume's font/header design" contract.

### Fixed

- **`verify_resume_facts` (`careeros/lint.py`) now catches a typo'd
  `companies`/`projects[].name` entry.** Neither is checked against
  `profile.yaml` at schema-validation time (both are just strings), so a
  misspelled name previously passed every check and then silently vanished
  from the rendered resume — `build_render_data` filters unmatched names out
  rather than erroring. Now flagged as a truthfulness violation at
  `artifacts --finalize` time, same as an invented number or a company-name
  leak.
- **Voice-dna lint and fact-verification now run on every `artifacts
  --finalize`, not just on a cache miss.** Both checks are pure regex
  (zero-token, microseconds) but were previously gated behind the artifact
  cache — hand-editing an already-finalized `resume.json`/`cover.md` in place
  (same job_hash/score/prompt_version, so the same cache key) skipped
  re-verification while the PDF still re-rendered, so an edit-introduced
  em-dash or invented metric could ship unchecked. The cache now only tracks
  "already proven clean once," never gates whether the check runs.

### Removed

- **Bundled font files.** `careeros/assets/fonts/` (Source Sans 3, Source
  Serif 4, ~3MB) is deleted along with the `font_paths` wiring in
  `typst_render.py` — both templates now use Typst's own built-in New
  Computer Modern, so nothing needs to ship in the package at all.

## [1.4.1] - 2026-07-13

### Added

- **Tailored Selected Projects.** `resume.json` (`prompts/resume_v2.md`) now
  selects 2-3 of `profile.yaml`'s projects per job by JD relevance, the same
  selector-not-writer rule as v1 (project bullets are never reworded, only
  which ones appear is tailored). Previously every profile project rendered
  unconditionally on every resume. `schemas/resume.schema.json` gained an
  optional `projects` field; omitting it fails-soft to every profile project,
  matching the old behavior.
- **A third project, MoatDaily**, added to `profile.yaml` — an autonomous
  Instagram content pipeline with a two-stage AI review gate and a real
  production reliability fix, now selectable alongside Rizent AI and
  CareerOS.
- **Page-density auto-fit.** `render_resume_pdf` (`careeros/typst_render.py`)
  now tries an ordered set of font-size/leading/margin presets, largest-first
  (`_FIT_TIERS`), and keeps the first that renders to exactly one page — a
  resume with lighter tailored content now renders bigger and fuller instead
  of leaving visible blank space at the bottom of the page, while heavier
  content shrinks a notch before ever hitting the existing `>1 page` finalize
  error. `resume.typ` reads the chosen tier via a new `fit` sys.inputs value.

### Changed

- Default `prompts.resume` config bumped from `v2` to `v3` — a deliberate
  full cache-bust, since the `resume.json` shape changed (new `projects`
  field) and the render default changed (auto-fit).

## [1.4.0] - 2026-07-13

### Added

- **Redesigned resume PDF rendering, via Typst.** Replaces the plain
  `fpdf2` markdown walker with a real typeset design (`careeros/typst_render.py`
  + `careeros/templates/resume.typ`/`cover.typ`): a bundled OFL Source Sans 3
  font, a modern-sans layout with a slate section-rule accent, right-aligned
  dates, a tab-aligned skills table, one page densely filled — while staying
  fully ATS-safe (single column, ligatures disabled, real selectable text in
  reading order, no tables-as-visual-layout). `typst` bundles its own
  compiler binary (Apache-2.0), so this stays a pure `pip install`, no
  LaTeX/pango/browser system dependency. Rendering now happens **locally**
  at `careeros artifacts --finalize` time (a new `[resume]` extra, folded
  into `[drive]`), so `artifacts/<job-id>/resume.pdf` exists on disk whether
  or not Drive is even enabled — previously the PDF only ever existed
  in-memory at Drive-upload time.
- **Resume content model v2 (`prompts/resume_v2.md`): reword to fit the JD,
  never invent a fact.** v1's rule required every resume line to be an exact
  copy of a `profile.yaml` bullet. v2 loosens that one constraint: the AI may
  now *reword* a selected bullet's language to mirror a target JD's own
  keywords — maximizing ATS keyword match — but every hard fact in it (every
  number, percentage, dollar amount, headcount, and named technology) must
  survive the reword unchanged. A new deterministic guardrail,
  `verify_resume_facts` (`careeros/lint.py`), enforces this mechanically: it
  rejects any reworded bullet introducing a number that isn't anywhere in
  that company's canonical `profile.yaml` bullets, and separately enforces a
  transferable-language rule — the resume must never name the specific
  target company, so the exact same tailored resume reads true for whichever
  employer receives it. `resume.md` is replaced by `resume.json`
  (`schemas/resume.schema.json`): a tailoring-zones-only payload (tagline,
  summary, reworded bullets, skill selection); canonical facts (name,
  contact, company, dates, education) come from `profile.yaml` and are
  merged in at render time, never present in the AI's own output at all.
- **ATS one-page gate.** `careeros artifacts --finalize` now renders every
  resume and checks its page count with `pypdf` (pure-Python, no poppler) —
  a resume that overflows one page is rejected with the exact page count,
  and the agent must trim bullets/skills in `resume.json` and re-run,
  mirroring a lint failure.
- **`careeros doctor`** gained a primary "Resume PDF rendering (Typst)"
  check (FAILs if `typst`/`pypdf` are missing while Drive is enabled) and
  downgraded the old fpdf2 check to "PDF rendering fallback (fpdf2)" — a
  WARN, not a FAIL, since Typst is now the primary renderer and fpdf2 is
  only the last-resort fallback.
- `profile.yaml`'s `Profile` model gained a `tagline` field (a one-line,
  generic/transferable resume tagline rendered under the contact line).

### Changed

- Default `prompts.resume` config bumped from `v1` to `v2`.
- `careeros verify-resume` now validates `resume.json` (fact-preservation +
  company-name-leak) by default; a bare `.md` file is still accepted for any
  not-yet-migrated historical resume, verbatim-matched the old (v1) way.
- `careeros/drive.py` now uploads whichever `resume.pdf`/`cover.pdf`
  `--finalize` already rendered locally, rather than re-rendering from
  Markdown at upload time; the legacy render-at-upload-time path remains as
  a fallback for any artifact that predates this version.

## [1.3.2] - 2026-07-12

### Fixed

- **Resume/Cover Letter silently uploaded to Drive as Markdown instead of
  PDF.** The `[pdf]` extra (`fpdf2`) was a separate, easy-to-forget install
  step from `[drive]` — a missing `fpdf2` degraded every Resume/Cover
  upload to `.md` with only a per-file warning, easy to miss. `fpdf2` is
  now bundled into the `[drive]` extra, so `pip install -e ".[drive]"`
  alone gets PDF rendering by default; `careeros doctor` (with
  `drive.enabled: true`) now also proactively checks for it and FAILs with
  a clear fix if it's ever missing, instead of only surfacing a buried
  warning during `daily`.
- **Only Resume and Cover Letter ever attempt PDF now.** Application
  Answers, Evaluation, and Deep Report always upload as Markdown — not
  "PDF with a fallback," simply never attempted, since only Resume/Cover
  are ever attached to a job application.
- **A Resume/Cover previously uploaded as `.md` (before PDF rendering was
  available) is no longer left orphaned in Drive once PDF rendering
  works.** Drive matches existing files by exact filename, so a new
  `.pdf` upload didn't replace an old `.md` — both used to sit side by
  side in the flat Drive folder. Re-uploading now explicitly deletes the
  stale `.md` when a `.pdf` supersedes it for the same job.

## [1.3.1] - 2026-07-12

### Added

- **`careeros doctor --live`.** Opt-in flag that actually verifies
  Fantastic Jobs and every configured Apify token against their real APIs
  right now (a small, bounded amount of real quota: one 1-record fetch,
  plus a free account-usage check per Apify token, no actor run) instead of
  only reporting local/stored state — catches a bad or exhausted key before
  your first `daily` run, not mid-`discover`.
- **Live provider-quota surfacing.** The discovery summary table now shows
  each provider's real, provider-reported remaining quota (e.g. Fantastic
  Jobs' `x-ratelimit-*` headers) when available, next to its record count
  and timing.

### Fixed

- **Score/recommendation could disagree.** `evaluate`'s `score` is meant to
  mean *applyability* — a green (≥ threshold) score should always mean the
  candidate can actually apply — but a job with a real deal-breaker (an
  onsite location outside your accepted cities, a stated preference
  violation) could still show a high score alongside
  `recommendation: "skip"`. `careeros evaluate --finalize` now
  deterministically caps a skip-recommendation eval's stored score below
  threshold, so the two signals can never disagree, regardless of which
  agent/model produced the evaluation. The 5 rubric dimensions themselves
  stay honest and untouched — only the final score is clamped — so the
  "why" behind a skip is still legible. `prompts/eval_v2.md` documents the
  contract explicitly, including an anchor scale for scoring `logistics`
  honestly as a preference-ranking signal rather than a pass/fail gate.
- **Fantastic Jobs' weekly quota counter was locally calculated, not
  live-verified.** The local `.careeros/discovery_budget.json` weekly
  record counter is independent of which API key is configured, so
  rotating in a fresh key did nothing to clear a "quota exhausted" state.
  The counter is now advisory-only; the live API call (and its
  `x-ratelimit-*` response headers) is authoritative, so a fresh key works
  immediately.
- **An exhausted Apify token stayed "exhausted" for the rest of the billing
  cycle**, even after a mid-month top-up on the same token, with zero
  re-verification. A token now only pre-emptively skips on the *same day*
  it was marked exhausted — any other day gets one fresh live retry before
  being trusted as exhausted again. Old on-disk state (the pre-1.3.1
  bare-list format) migrates automatically.
- **AI reasoning stages could be silently replaced by scripts.** A new
  standing rule in `AGENT_GUIDE.md` ("Reasoning stages must be reasoned,
  never scripted"), referenced from `prompts/gate_v1.md`,
  `prompts/eval_v2.md`, and `skills/daily.md`'s Gate/Evaluate steps, makes
  explicit that Gate/Evaluate/Resume/Cover/Application-Answer output must
  always come from real per-job reasoning — never a keyword-matching or
  formulaic script standing in for it, even under batch-size pressure.

## [1.3.0] - 2026-07-11

### Added

- **Parallel provider discovery.** `discover` now runs every enabled
  provider's `fetch()` concurrently (`ThreadPoolExecutor`, new
  `discovery_max_workers` config knob, default 4) instead of one after
  another — total wall-clock is roughly the slowest single provider
  instead of the sum of all of them. Budget/quota checking and recording
  stays strictly serial and race-free (preflight → concurrent fetch →
  serial bookkeeping), and results always merge in config order regardless
  of completion order, so dedupe's "keep first" contract is unchanged.
  `discovery_max_workers: 1` reproduces the old fully-serial behavior.
- **Resilient Apify credentials.** Multi-token rotation
  (`APIFY_TOKENS`) is now silent — no more alarming per-token failure
  lines on a normal rotation. A new rolling-month `apify_tokens.json`
  caches exhausted tokens by a non-reversible fingerprint (never the raw
  token), so an already-exhausted key is skipped on future calls instead
  of being retried every run. Only when every configured token is
  exhausted does the provider stop with a sharpened error naming the fix
  path.
- **Provider health + timing in `careeros doctor`.** Every enabled
  provider now shows its last-run status (ok / skipped + reason / never
  run) and duration, sourced from data already persisted by `discover` —
  no new data model. Apify-actor providers additionally show how many of
  the configured tokens are currently available this billing cycle.
- **One canonical onboarding doc.** `AGENT_GUIDE.md` (repo root) is now
  the single source of truth for any host coding CLI — repo map, the
  deterministic-vs-reasoning boundary, secrets handling, and a universal
  **Failure Handling Principle**: whenever a non-trivial pipeline step
  can't complete as intended (provider/credential/quota/network failure,
  a Drive/Sheets write failure, a generation failure, anything), the
  agent states what failed, why if known, the impact on the run, the
  available options, and waits for explicit confirmation before
  continuing — one rule, applied uniformly, replacing the narrower
  per-stage stop conditions `skills/daily.md` used to spell out
  individually. `CLAUDE.md`/`GEMINI.md`/`AGENTS.md` are now thin
  redirects to `AGENT_GUIDE.md` for CLIs with a known auto-load
  convention. `skills/daily.md` gained a Step 0 (source
  `.careeros/secrets.env`, run `careeros doctor` as a sanity gate) before
  discovery starts.

### Fixed

- A hard failure from a zero-budget-capability provider (e.g. RemoteOK,
  We Work Remotely) previously had no error handling at all and could
  abort the entire multi-provider `discover` run instead of being marked
  `skipped` like every other provider — now consistent across all three
  provider capability tiers.

## [1.2.0] - 2026-07-11

### Added

- **Multi-provider discovery.** `.careeros/config.yaml` gets a new
  `providers:` block — a dict of provider id -> `{enabled, ...its own
  config}` — replacing the old single `provider:` key as the one discovery
  source model. Every `enabled: true` provider runs in `discover`, IN THE
  ORDER LISTED (dedupe keeps the first occurrence of a duplicate role, so
  list your primary source first), and their results merge into the same
  flat job list every later pipeline stage already consumed — `normalize`
  onward is completely unaware of how many providers ran.
- **Seven new discovery sources**, classified by evidence from a real
  combined live validation (not by assumption) into four tiers — see
  `providers/README.md`'s "Shipped providers" section for the full
  relevance/cost/reliability findings behind each:
  - **Core** (on by default, zero setup): `remoteok`, `we-work-remotely` —
    free, direct, no signup.
  - **Optional** (off by default, recommended to enable deliberately):
    `naukri` (10/10 relevance at n=10, flat ~$0.0005-0.005/run — the
    strongest single recommendation of the five), `glassdoor` (relevant,
    converges to ~$0.005/job at realistic batch sizes — judge its cost from
    a `limit >= 20-30` run, never a `--limit 3` trial), `ziprecruiter`
    (~$0.004/job at n=30 — comparable to Glassdoor, not the cost outlier a
    small trial run suggested; known ~63% actor run-success rate, handled
    gracefully with a retry next run).
  - **Experimental**: `indeed` — good relevance for some queries (e.g.
    "Software Engineer") but ~10% relevant at n=20 for this project's
    default "Product Manager" query; verify against your own search terms
    before enabling.
  - **Not Recommended**: `foundit` (Monster India, rebranded) — irrelevant
    results across multiple independently tested queries, ruling out a
    query-construction bug; kept registered for completeness, not for
    enabling.
  All five Apify-actor sources ship `enabled: false` regardless of tier — a
  fresh clone has no Apify token configured. See `providers/README.md` for
  the "Turning on a paid provider" workflow.
- **The `ProviderResult` contract.** Every provider (old and new) now
  implements exactly `validate(config)`, `fetch(config, **kwargs) ->
  ProviderResult`, `to_job_dict(raw)` — no special cases. `ProviderResult`
  carries per-provider metadata (cost, requests, records, duration,
  warnings/errors, and an explicit `skipped`/`skip_reason` for a provider
  that was enabled but couldn't run this call) surfaced in `run.json` and a
  new provider-by-provider table in `summary.md`.
- **Capability-driven budget/quota enforcement**, never a check on a
  provider's name. Fantastic Jobs keeps its existing weekly-record-quota
  guard untouched; every Apify-actor provider shares a new rolling-month
  USD budget (`apify.max_monthly_budget_usd`, default $10, overridable
  per-provider) — a best-effort soft guard backed by a hard per-call
  `max_total_charge_usd` cap Apify enforces server-side. `careeros doctor`
  shows every enabled provider's credentials status (via its own
  `validate()`) and, for Apify-actor providers, budget-vs-spend.
- **`careeros migrate-config`** — rewrites a config still using the
  deprecated single `provider:` key to the new `providers:` model,
  permanently, on disk. Idempotent, safe to re-run (same shape as
  `careeros sheets migrate`).
- **Provider-centric onboarding.** `careeros init`'s guided setup now asks
  about optional paid sources by name — Naukri, Glassdoor, ZipRecruiter —
  with a one-line evidence-based pitch for each, not "enable Apify." If you
  opt in, it asks for a monthly budget and which providers to enable, saves
  the choice to config, and only then mentions the shared Apify credential
  those sources run on behind the scenes. Foundit isn't offered by default;
  Indeed is mentioned only if asked. Editable later via `careeros doctor`
  or the config file directly.

### Fixed

- **Glassdoor: relative `applyUrl`/`jobLink` silently dropped every job at
  production batch size.** A small (`--limit 3`) trial sample happened to
  return absolute URLs; a `limit: 30` combined-validation run (the kind of
  real, at-scale check driving this release) showed the actor's real output
  is a site-relative partner-tracking path (`/partner/jobListing.htm?...`),
  which `to_job_dict` was rejecting outright. Now resolved against
  `https://www.glassdoor.com` before validation. Caught specifically
  because this release's validation ran at realistic scale instead of
  trusting a small trial — see `providers/README.md`'s "Cost: don't trust a
  `--limit 3` trial" for the general lesson.

### Changed

- The old `provider:` config key is deprecated. A config that still sets it
  (with no `providers:` block) is auto-upgraded IN MEMORY on every load —
  same single source, nothing new enabled — with a one-time notice pointing
  at `careeros migrate-config`. Scheduled for removal in v2.0.
- `careeros init` and `templates/config.example.yaml` now ship the
  `providers:` model directly; a fresh clone never sees the deprecated key.

## [1.1.0] - 2026-07-10

### Added

- **Automatic Application Answers.** For every Apply-tier job, `daily` now
  drafts real answers to that job's actual application questions — no
  waiting until you've manually opened the form. A new background form-reader
  (`careeros/apply/browser.py`) fetches the form's visible text: a
  zero-dependency HTTP tier first, an optional headless-Playwright fallback
  only when the form genuinely needs JavaScript to render. Neither tier uses
  per-site scraping selectors — the agent identifies the real questions from
  plain text and drafts grounded answers the same "selector, not writer" way
  as the resume.
- **`careeros publish <job-id>`** — upload one job's current artifacts
  (resume, cover, evaluation, deep report, application answers — whichever
  exist) to Drive and patch just that Sheet row, without a full `daily` run.
  The `prep` and on-demand `apply <job-id>` skills now run this
  automatically as their last step.
- **A specific status for every unreadable form**, replacing one generic
  fallback: ✅ Generated, 🔒 Login Required, ❌ Closed, ⚙️ Playwright
  Missing, 📄 No Essay Questions, 🌐 Network Error, 🛡️ Bot-Blocked, or the
  generic Manual review required only when none of those match. Each is
  detected generically (login walls, closed-posting notices, Cloudflare-style
  bot challenges, and pages that render real text but never reach an actual
  form) — no per-ATS logic.
- **A `Status` column** in the Sheet (dropdown: `Not Applied` by default,
  `Applied`, `Received Call`, `Interview`, `After Interview`,
  `Ongoing / In Process`, `Offer`, `Rejected`) for tracking your own
  application progress by hand. The pipeline sets the default on a new row
  and never touches it again, exactly like `Notes`.
- **New Sheet columns**: `Evaluation (Drive)`, `Deep Report (Drive)`,
  `Application Answers (Drive)` — direct, per-job links to artifacts that
  were already being generated (and, for Evaluation, already uploaded) but
  had nowhere to show up.
- **Newest-on-top Sheet ordering.** New rows are now inserted directly below
  the header instead of appended at the bottom, so each day's run reads at
  the top without scrolling past a growing history.
- **`careeros sheets sync-status`** — patch the Application Answers status
  of existing Sheet rows after re-running `apply --prepare/--finalize`
  (e.g. reclassifying old jobs into the newer, more specific statuses)
  without appending a duplicate row.
- **Personal/logistics questions, asked once.** Notice period, work
  authorization/visa status, salary expectations, earliest start date, and
  employment type are the same answer on every application. The first time
  one is genuinely needed and missing, it's asked once and saved to
  `profile.yaml`'s new `logistics:` section — every later application,
  batch or on-demand, reuses it automatically. Voluntary EEO/demographic
  self-identification questions are deliberately excluded and always left
  for you to answer yourself.
- **`careeros --version`** — prints the installed version and exits.
- `careeros doctor` now checks Playwright independently at two levels: the
  `[apply]` extra's Python package, and the `chromium` browser binary
  (`playwright install chromium`) — "package installed but browser binary
  missing" and "package not installed at all" are reported as distinct,
  actionable messages instead of one opaque failure.

### Changed

- **Sheet columns removed**: `Resume Path`, `Cover Letter Path`,
  `Report Path` (local filesystem paths, useless outside your own machine)
  and `Drive Folder` (redundant once every artifact has its own direct
  link). `careeros sheets migrate` / `sheets append` remove these
  automatically from an existing Sheet — see Migration notes below.
- CI now installs the optional `[apply]` extra and runs
  `playwright install chromium --with-deps`, so the real headless-browser
  fetch path runs in automated tests instead of being skipped.
- The package version now has a single source of truth
  (`pyproject.toml`); `careeros.__version__` reads it back via
  `importlib.metadata` instead of duplicating the string.

### Fixed

- The Playwright fallback used `wait_until="networkidle"`, which never
  fires on pages with persistent background network activity (analytics
  beacons, a bot-check's own verification polling) — it could hard-timeout
  with zero text captured even though the real content rendered within a
  second or two. Switched to `wait_until="load"` plus a short fixed buffer.
- PDF rendering crashed instead of falling back to Markdown when resume,
  cover, or answers content contained a non-Latin currency symbol (₹, €, £,
  ¥) — blocking the entire Drive upload for that job. Now sanitized to a
  latin-1-safe equivalent, and any other still-unmappable character falls
  back to Markdown instead of failing the upload.
- `careeros sheets sync-status` could overwrite a just-published
  Application Answers link with a blank cell, because the local
  `drive_links.json` cache isn't refreshed by `careeros publish`. It now
  never touches a `generated`-status job's cell — only `publish` does.
- A login-wall, closed-posting, or bot-blocked page returns real,
  substantial (non-empty) text — it's just the wrong page. The batch apply
  stage now checks for these BEFORE treating any non-empty fetch as a
  readable form, so a LinkedIn login-wall page can no longer be silently
  sent to the drafting step as if it were the real application form.

## Migration notes: upgrading an existing Sheet

If you were running CareerOS before this release, your Google Sheet needs
one one-time cleanup pass:

```
careeros sheets migrate
```

This removes the four deprecated columns, adds the four new ones, applies
header/Score/Status formatting, backfills `Status` to `Not Applied` on
existing rows, and sorts your Sheet's existing rows by Date descending (a
one-time fix for history that was written bottom-up before this release).
It's safe to re-run — every step is idempotent, and a Sheet already on the
current schema is a no-op. After this one pass, every future `daily` run
keeps the Sheet current automatically; you never need to run it again
unless you skip several releases at once.

## [1.0.0] - 2026-07-09

Initial public release: profile-driven segmented discovery through the
Fantastic Jobs REST API (with a legacy Apify-actor provider available),
deterministic normalize/dedupe/constraints/two-tier threshold, the AI Gate
and Evaluate stages, resume/cover generation, a zero-cost daily report and
summary, optional Google Drive backup, and Google Sheets output.
