# Changelog

All notable, user-visible changes to CareerOS are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/); versions
follow [Semantic Versioning](https://semver.org/).

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
