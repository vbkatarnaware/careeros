# CareerOS

[![CI](https://github.com/vbkatarnaware/careeros/actions/workflows/ci.yml/badge.svg)](https://github.com/vbkatarnaware/careeros/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/vbkatarnaware/careeros)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

An AI-powered, deterministic job discovery and recommendation engine. Not an
application bot.

CareerOS finds jobs, scores them against your real experience, and writes
the results into a Google Sheet you open every morning. The KPI is simple:
**more interviews, for the least amount of AI and compute cost.**

## Why this exists

Most "AI job search" tools do one of two things badly: they spam
applications with generic resumes, or they burn expensive AI calls
evaluating and writing long reports for every single job they find, most of
which the candidate will never apply to.

CareerOS is built around one rule: **use deterministic code wherever
possible, and AI only where reasoning genuinely adds value.** Discovery,
deduplication, thresholding, and the daily report are plain code — zero
tokens. AI is spent on exactly two things that need judgment: a cheap
triage gate, and one real evaluation per job that survives it.

## The governing principle: two sources of truth

Everything CareerOS generates traces back to exactly two files:

- **`profile.yaml`** — your facts. Verbatim experience bullets, targets,
  constraints. Generated once (via `careeros start`), edited rarely.
- **`eval/<job-id>.json`** — a job's fit judgment. Generated once, by the
  `evaluate` stage. Never recomputed downstream.

Every later artifact is a *derivation*, never a re-derivation:

| Artifact | Derives from | Never does |
|---|---|---|
| Daily Report | eval JSON | costs an AI call — it's a pure template render |
| Resume | profile facts + eval keywords | invent a bullet, or re-score |
| Cover Letter | profile facts + eval's fit paragraph | claim something not in your profile |
| Deep Report | eval JSON + profile + new research | re-score the job |
| Application Answers | profile facts + eval + the form's real questions | fabricate experience |

This is "selector, not writer," applied everywhere: facts and judgments are
generated once and reused. The one place this bends on purpose is the Deep
Report, which legitimately needs external research the cheap daily eval
was never meant to gather — that research is additive and clearly
separated from the inherited, non-recomputed fit judgment.

## Architecture

```
/careeros daily   (a skill in your host coding CLI)
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│  HOST CODING AGENT (Claude Code / Codex / Gemini CLI / …)   │
│  the runtime AND the model — CLI-agnostic by construction.  │
└──────┬─────────────────────────────────┬────────────────────┘
 deterministic (shell out)        reasoning (agent thinks)
       ▼                                  ▼
┌─────────────────────────┐     ┌──────────────────────────┐
│ careeros (Python)        │     │ prompts/*_vN.md           │
│ discover · normalize     │     │ gate · eval · resume ·   │
│ dedupe · threshold ·     │     │ cover · deep_report ·    │
│ sheets · lint · cache    │     │ apply                    │
└──────────┬───────────────┘     └──────────────────────────┘
           │ read/write
           ▼
   ┌─────────────────────────────────────────────┐
   │ .careeros/                                    │
   │  profile.yaml   ← source of truth #1 (facts)  │
   │  cache/         ← fingerprinted reuse          │
   │  runs/<date>/   ← the message bus              │
   │    06_evaluate/<job-id>.json ← source #2       │
   └─────────────────────────────────────────────┘
           │
   ┌───────┴───────┬──────────────────┐
   ▼               ▼                  ▼
 Fantastic Jobs   Google Sheets    Drive (optional)
 [deterministic]  [deterministic]  [deterministic]
```

CareerOS has no server and no database. The filesystem is the message bus:
every pipeline stage reads one JSON file and writes another, under
`.careeros/runs/<date>/`. That makes every run inspectable, resumable, and
cheap to re-run (unchanged inputs hit the cache, not the model).

## Pipeline

1. **Discover** — call a provider (Fantastic Jobs REST API by default; a
   legacy Apify-actor provider is also available — see
   `careeros/providers/README.md`), by default as one segmented query per
   profile work-mode tier rather than a single broad fetch
   (`pipeline/queryplan.py`). The REST provider queries **both** Fantastic
   sources by default — `active-ats` (career sites/ATS: Greenhouse, Lever,
   Ashby…) and `active-jb` (+ LinkedIn/YC/Wellfound) — merged and deduped.
   A production acceptance audit (full 107-job population) found the two
   sources score an equal ~8% ≥4.0 rate but are 92% disjoint, so querying
   both roughly doubles interview-worthy jobs found at the **same** quota
   (the per-tier record allocation is split 50/50, not doubled). Set
   `api.endpoint` to `active-ats` or `active-jb` to use one source.
   Deterministic.
2. **Normalize** — map to the universal `Job` schema. Deterministic.
3. **Dedupe** — drop jobs already seen this run, in a prior run, or already
   in the Sheet. Deterministic.
4. **Constraints** — hard-reject on the two objective deal-breakers, location
   and salary, before any AI is spent. Deterministic (`pipeline/constraints.py`).
5. **AI Gate** — cheap, batched keep/drop triage against your profile's
   targets and deal-breakers. Bias to keep; recall over precision.
6. **Evaluate** — the real reasoning step. Scores against a Career
   Ops-derived rubric, writes structured JSON only (no long report). This
   file is the source of truth for everything downstream.
7. **Threshold** — two tiers, both configurable. **Apply** (score ≥ 4.0 AND
   recommended "apply" AND passing the constraints re-check) gets the full
   pipeline: resume, cover letter, report, Drive, and a Sheet row. **Consider**
   (3.5 ≤ score < 4.0) gets a Sheet row only — score plus a one-line reason it
   fell short — with **no** AI artifacts and no Drive, so near-misses stay
   visible at zero extra AI cost. Below 3.5 is omitted from the Sheet. A hard
   constraint failure (location/salary deal-breaker) is always omitted, never
   shown as Consider.
8. **Artifacts** — resume + cover letter (selected from `profile.yaml`,
   never invented, cache-checked) + a daily report (rendered from the eval
   JSON, zero AI).
9. **Summary** — a deterministic `summary.md` (funnel, the Apply list, the
   Consider/near-miss list, cost-per-selected-job). Zero AI. The KPI is cost
   per interview-worthy job, supply-aware — a day with 0 selected is a
   legitimate outcome, not a failure, and CareerOS never lowers the quality
   bar just to hit a job count. It also includes a **Discovery KPI** block:
   Apply conversion (Apply ÷ Discovered — the discovery-quality
   metric tracked over time against the interviews/week goal), the ATS vs.
   job-board source split, and requests/records used against your weekly
   quota. All of it is read from files other stages already wrote — no new
   API calls.
9a. **Application Answers** *(Apply-tier only)* — for every job that
    just got resume/cover, an invisible **background** fetch
    (`careeros/apply/browser.py`: a lightweight HTTP fetch first, an
    optional headless-Playwright fallback only if the form genuinely needs
    JavaScript to render — never your own browser, never a visible window)
    reads the application form's real questions and drafts answers the same
    "selector, not writer" way as the resume. A form that isn't
    automatically readable is marked with a specific status — 🔒 Login
    Required, ❌ Closed, ⚙️ Playwright Missing, 📄 No Essay Questions, or 🌐
    Network Error — not fabricated; see "Application Answers" below.
10. **Drive** *(optional, off by default)* — additive backup of Apply-tier
    artifacts (Resume/Cover/Application Answers as PDF, Evaluation, Deep
    Report if present) into one flat Drive folder via your own OAuth desktop
    grant. Idempotent (re-uploads update in place). Any failure here only
    warns; it never blocks the rest of the pipeline.
11. **Sheets** — append one row per Apply job (with per-file Drive links if
    step 10 ran — Resume/Cover/Evaluation/Deep Report/Application Answers,
    no shared-folder link) and one row per Consider job (score + reason
    only). You open the Sheet and start applying.

Two more commands exist outside the daily loop, deliberately:

- **`careeros prep <job-id>`** — a full interview-prep report, generated
  only when you ask for it, expanding (never re-deriving) the eval.
- **`careeros apply <job-id>`** — application-answer drafting for one job,
  any score, using your own real logged-in browser (or pasted questions) —
  the manual counterpart to step 9a's automatic Apply-tier batch. Use it for
  a below-threshold job you still want to pursue, or one step 9a marked with
  any of its non-"✅ Generated" statuses.

Both skills (see `skills/prep.md`/`skills/apply.md`) end by ALWAYS running
**`careeros publish <job-id>`** automatically, without you having to ask —
neither writes a Drive link the next `sheets append` would retroactively
pick up on its own, so publishing is a required last step of each skill,
not a separate command you need to remember (see Commands below).

## Commands

| Command | Description |
|---|---|
| `careeros init` | Scaffold `.careeros/` (config, profile template) |
| `careeros start` | Guided onboarding → `.careeros/profile.yaml` + discovery goal/plan. Opens by asking for your CV (optional — `skip` to answer questions instead) |
| `careeros doctor` | First-run checklist: Python version, profile, discovery credentials, Sheets, Drive — plus your current vs. recommended discovery limit and the last discovery failure, if any (from local state — never a live API call). Never modifies anything |
| `careeros daily` (alias `scan`) | Run the full daily pipeline |
| `careeros prep <job-id>` | Level-2 deep interview-prep report |
| `careeros apply <job-id>` | Detect ATS, draft application answers for one job (any score) using your own real browser or pasted questions |
| `careeros publish <job-id>` | Upload one job's current artifacts to Drive and patch just that Sheet row — use after `prep`/`apply <job-id>` so the link shows up without a full `daily` run |
| `careeros config` | Show resolved config, incl. the discovery quota-guard's current recommendation |
| `careeros providers` | List registered discovery providers |
| `careeros migrate-config` | One-time, idempotent rewrite of a config still using the deprecated single `provider:` key to the current `providers:` model |
| `careeros backfill-drive` | Add Drive artifacts + clickable Sheet links to Apply-tier rows from before Drive was enabled. Defaults to `--dry-run` |
| `careeros sheets migrate` | Clean up an existing Sheet right now: remove deprecated columns, add new ones, apply formatting — the same pass `sheets append` already runs automatically on every write |
| `careeros sheets sync-status` | Patch the Application Answers status of EXISTING Sheet rows from a re-run of `apply --prepare/--finalize`, without appending new rows — use after reclassifying old jobs into the newer status taxonomy |
| `careeros --version` | Print the installed version and exit |

Developer/debug commands — each stage runnable standalone against a run
directory, without re-running the whole pipeline:

`discover` · `normalize` · `dedupe` · `constraints` · `gate` · `evaluate` ·
`threshold` · `artifacts` · `apply --prepare/--finalize` · `summary` ·
`drive` · `sheets append` · `render-report` · `lint <file>` ·
`verify-resume <file>`

## Folder structure

```
careeros/
├── careeros/            # the deterministic Python toolkit
│   ├── cli.py
│   ├── config.py  models.py  cache.py  runmeta.py  lint.py  report.py
│   ├── sheets.py  drive.py  pdf.py  budget.py
│   ├── apply/            # Application Answers: HTTP/Playwright form-reading (browser.py)
│   ├── providers/        # one file per discovery source
│   └── pipeline/         # queryplan, normalize, dedupe, constraints, threshold
├── prompts/              # AI step templates, versioned (gate_v1.md, ...)
├── skills/               # host-CLI playbooks (daily, start, prep, apply)
├── schemas/              # JSON Schema — the actual source-of-truth contracts
├── templates/            # example profile/config, safe to commit
└── .careeros/            # your local state (gitignored): profile, cache, runs
```

## Installation

Requires Python 3.11+.

```
git clone https://github.com/<you>/careeros.git
cd careeros
pip install -e .
```

That installs the `careeros` CLI plus the default REST provider's dependency
(`requests`). Two things you'll also need, neither installed by pip:

- **A host coding CLI** (Claude Code, Codex, Gemini CLI, OpenCode, …) — this
  is what actually runs `/careeros daily` and performs the AI Gate/Evaluate
  reasoning steps. CareerOS's own Python package is the deterministic half;
  see [Architecture](#architecture).
- **A Fantastic Jobs API key** (default provider) — see Quickstart below.

## Quickstart

```
$ careeros init
Wrote .careeros/config.yaml
Wrote .careeros/profile.yaml (seeded template — edit with your own facts,
  or run `careeros start` for the guided interview)

Next: in .careeros/config.yaml, set api.transport to "direct" or "rapidapi"
and the matching key env var (FANTASTIC_API_KEY / RAPIDAPI_KEY), set up
Sheets credentials, then run `careeros daily`.
```

Then:

1. **Set your discovery providers' credentials.** `careeros` runs three
   **Core** sources by default (`.careeros/config.yaml`'s `providers:`
   block): the official Fantastic Jobs REST API (your main source — needs a
   key), plus RemoteOK and We Work Remotely (free, direct, no signup —
   nothing to configure). For Fantastic Jobs, pick one transport in
   `api:` and export the matching key:
   - `api.transport: direct` → `export FANTASTIC_API_KEY=...` ([developer.fantastic.jobs](https://developer.fantastic.jobs))
   - `api.transport: rapidapi` → `export RAPIDAPI_KEY=...` (RapidAPI's "Active Jobs DB")

   *(Want more sources? `/careeros start` (below) offers a handful of
   **Optional** paid job boards by name — Naukri, Glassdoor, ZipRecruiter —
   each with a one-line evidence-based pitch; opt in, set a monthly budget,
   and they run on a single shared Apify credential behind the scenes (`export
   APIFY_TOKEN=...`, or `APIFY_TOKENS=tok1,tok2,...` for multi-account
   rotation). See `careeros/providers/README.md`'s "Shipped providers" for
   the full relevance/cost/reliability evidence behind every source,
   including the legacy Apify-actor Fantastic Jobs backend
   (`fantastic-jobs-actor`) kept as an advanced reference option.)*
2. **Set up your profile**: `/careeros start` inside your host coding CLI —
   opens by asking you to paste your CV (optional; `skip` to answer
   questions instead), then extracts your facts into `.careeros/profile.yaml`
   and asks your interviews/week goal + Fantastic Jobs plan (Free / Paid /
   Custom quota) to recommend a daily discovery limit, explained in plain
   English against your own search preferences (e.g. *"CareerOS will run 3
   discovery searches every day. On the Free plan, the recommended limit is
   23 records per search."*) — accept it or enter your own value. **If you
   skip this or never set `api.plan`, CareerOS assumes the Free plan by
   default** (500 records/week) rather than silently over-fetching — you'll
   see a one-time note about the assumption on `discover`/`careeros config`,
   and `careeros doctor` always shows current-vs-recommended. Or hand-edit
   `.careeros/profile.yaml` directly — see `templates/profile.example.yaml`.
   **Change the limit anytime later** by editing `api.limit`/`api.plan` in
   `.careeros/config.yaml`.
3. **Set up Google Sheets** (the daily results destination): a spreadsheet id
   + service-account credentials path in `config.yaml`'s `sheets:` block.
   First time with Google Cloud? Follow the click-by-click walkthrough in
   **[docs/google-setup.md](docs/google-setup.md)** — it covers creating the
   service account, downloading the key, and the easily-missed step of
   *sharing your Sheet with the service account's email*. (Optional Google
   Drive backup is covered there too.)
4. **Check your setup**: `careeros doctor` — a green/red checklist for
   Python version, profile, discovery credentials, Sheets, and Drive. Fixes
   nothing itself; just tells you exactly what's missing. If `discover` ever
   fails, it now classifies *why* — an invalid/expired API key, a network or
   Fantastic Jobs outage, transient rate-limiting, or your request/record
   quota being exhausted are each reported with a distinct, plain-English
   next action instead of a generic error; `doctor` also shows the last
   failure from local state, with no extra API call.
5. **Run it**: `/careeros daily` inside your host coding CLI.

## Example run

```
$ /careeros daily        # run inside Claude Code / Codex / Gemini CLI / etc.
  [discover] query 1/4 (global_remote): 22 items
  [discover] query 2/4 (india_remote): 9 items
  [discover] query 3/4 (navi_mumbai_onsite): 11 items
  [discover] query 4/4 (mumbai_onsite): 42 items
[discover] fantastic-jobs: 84 raw items across 4 queries (11.4s)
[normalize] 84 raw -> 81 jobs (0.1s)
[dedupe] 81 in -> 47 unique, 34 dropped (in-run: 2, history: 30, sheet: 2)
[constraints] 47 in -> 41 eligible, 6 hard-rejected (0.0s)
[gate:finalize] 41 in -> 19 kept, 22 dropped.
[evaluate:finalize] 19 evaluations valid and cached.
[threshold] 19 evaluated -> 4 APPLY (>= 4.0), 6 CONSIDER ([3.5, 4.0))
[artifacts:finalize] 4 job(s), 8 artifact(s) verified, 8 newly cached.
[sheets:append] wrote 10 row(s) (4 Apply, 6 Consider).

4 jobs scored above threshold. Top match: Senior PM at Acme (4.6) — strong
role fit, remote, comp in range. See your Sheet for all 4 with resumes and
cover letters generated, plus 6 near-misses under Consider for visibility.
```

## Google Sheets schema

One `Jobs` worksheet. New rows are inserted directly below the header, not
appended at the bottom — each day's newest run reads at the TOP, so you
never have to scroll past a growing history to find today's jobs. Within a
single run's batch, rows keep their normal Apply-then-Consider order;
across runs, later `daily` runs stack above earlier ones.

`Date · Company · Role · Score · Tier · Recommendation · Confidence ·
Apply URL · Status · Resume (Drive) · Cover Letter (Drive) ·
Evaluation (Drive) · Deep Report (Drive) · Application Answers (Drive) ·
Notes · Source · Company LinkedIn · Hiring Contact · Contact LinkedIn ·
Contact Email · Job ID`

`Tier` is `Apply` or `Consider` (see Pipeline step 7); a Consider row has
blank artifact/Drive cells and a `Notes` reason it scored below 4.0. `Status`
is a dropdown (data validation, not free text) you update by hand as you
actually apply: `Not Applied` (the default on every new row), `Applied`,
`Received Call`, `Interview`, `After Interview`, `Ongoing / In Process`,
`Offer`, `Rejected`. It's yours to track — the pipeline only ever sets the
default on a NEW row and never overwrites it afterward, exactly like `Notes`.

Columns are located by header **name**, not position, and any missing column
is added (deprecated ones removed) automatically — so a Sheet created by an
earlier version self-migrates on the next `sheets append` without losing
data or breaking dedupe; run `careeros sheets migrate` to apply that same
pass right now instead of waiting for the next `daily` run — this also
sorts an older Sheet's existing rows by Date descending, a one-time fix
for history that was written bottom-up. The header row is bold and frozen,
`Score` is conditionally colored — light green at 4.0 and above, light
yellow below — and `Status` shows its dropdown arrow, all applied
automatically so you can scan Apply-quality at a glance.

`Job ID` is the join key `prep`/`apply`/`publish` use to look a row back up.
`Company LinkedIn` is populated for ~100% of postings at zero extra cost.
Every `... (Drive)` column is a direct, per-job clickable link straight to
that job's own file — no shared-folder link (there's only ever one project
folder, so a per-row link to it added nothing) and no local filesystem
paths (useless outside your own machine — an earlier version removed the
old Resume Path/Cover Letter Path/Report Path columns for the same reason).
They're populated only once Drive backup (below) is enabled and that
specific artifact actually exists — `Deep Report (Drive)` stays blank until
you run `prep`, and `Application Answers (Drive)` shows a specific status
label (e.g. **"🔒 Login Required"**, **"❌ Closed"**, **"⚙️ Playwright
Missing"**) instead of a link for an Apply-tier job whose application form
wasn't automatically readable (see below). Got Apply-tier rows from before
Drive backup existed? `careeros backfill-drive` adds Resume/Cover links to
them; `careeros publish <job-id>` adds Evaluation/Deep Report/Application
Answers links to one specific row on demand — see below.

## Google Drive backup (optional)

Off by default. When `drive.enabled: true`, `careeros drive` uploads every
Apply-tier job's Resume + Cover Letter + Application Answers (as **PDF**,
Answers only if generated — see below), Evaluation, and Deep Report (if
you've run `prep` on it) — plus the day's `run.json` and `summary.md` —
into **one flat folder** (`drive.root_folder_id`) as an **additive**
backup; your local `.careeros/runs/` Markdown is never moved, replaced, or
read back by any pipeline stage. Files are named
`Company - Role - Resume.pdf`, `Company - Role - Cover Letter.pdf`,
`Company - Role - Application Answers.pdf`, `Company - Role - Evaluation.md`,
`Company - Role - Deep Report.md` — no per-company or per-job subfolders
(set `drive.date_subfolder: true` if you'd rather group each day's uploads
under a `YYYY-MM-DD/` subfolder instead). Consider-tier jobs never generate
artifacts, so they never upload anything.

Re-running `daily` (or `backfill-drive`/`publish`) for the same job updates
its existing files in place rather than duplicating them — uploads are
idempotent. Needs two optional extras:

```
pip install -e ".[drive,pdf]"
```

`[drive]` (Google API client + OAuth) is required for any upload at all.
`[pdf]` (pure-Python `fpdf2`, no system binaries) renders the PDFs; without
it, Resume/Cover Letter/Application Answers upload as Markdown instead and a
warning is printed — Drive backup still works, just not with PDFs.

You'll also need an OAuth **Desktop app** client secret (Google Cloud Console
→ Credentials → Create Credentials → OAuth client ID → Desktop app) — not a
service account, since uploads land in your own personal Drive quota. The
first run opens a one-time browser consent; after that, a cached refresh
token (`drive.token_path`, gitignored) makes every later run silent. Any
Drive failure (auth, network, quota) only prints a warning — discovery,
evaluation, Sheets, and every other stage run exactly as if Drive were off.

### Backfilling jobs from before Drive was enabled

If you already have Apply-tier rows in your Sheet from before you turned
Drive on (or before upgrading to this version), `careeros backfill-drive`
adds Drive artifacts + clickable Sheet links to them without touching
anything else in those rows:

```
careeros backfill-drive            # dry run (default) — preview only, writes nothing
careeros backfill-drive --no-dry-run   # actually uploads + updates the Sheet
```

It's safe to re-run — rows that already have both Drive links are skipped.
If a row's local `resume.md`/`cover.md` no longer exist on disk (an old run
directory was cleaned up), that row is listed as **needing regeneration**
instead of inventing content — nothing is ever fabricated.

## Application Answers

For every Apply-tier (score ≥ threshold) job, `daily` automatically drafts
answers to that specific job's real application questions — no waiting
until you've manually opened the form. `careeros/apply/browser.py` reads
the form's visible text in the **background**:

1. A lightweight HTTP fetch first (the already-core `requests` dependency —
   nothing extra to install). Most ATS application pages (Greenhouse,
   Lever, Ashby, and similar) are viewable, and therefore readable this
   way, even though *submitting* usually needs an account. The fetched text
   is also checked, generically (no per-ATS selectors), for a login wall, a
   closed-posting notice, or a real page that server-rendered plenty of
   text but never got past an unclicked "Apply now" button (e.g. some
   client-side-routed careers sites) — each is a distinct, specific outcome
   (see below), not a fetch failure.
2. Only if that isn't enough — the page genuinely needs JavaScript to
   render — an **optional** headless-Playwright fallback. Installing it is
   **two steps**, not one — `pip install` alone gets you the Python
   package but not the actual browser:
   ```
   pip install -e ".[apply]"
   playwright install chromium
   ```
   This launches its own isolated, invisible browser context. It never
   touches your real browser, never opens a visible window, and never
   interrupts whatever you're doing on your machine. Run `careeros doctor`
   any time to check whether both steps are done — it reports the two
   independently, so "package installed but browser binary missing" and
   "package not installed at all" show up as different, specific messages
   rather than one opaque failure.

Neither tier has any per-ATS scraping logic (no brittle selectors tied to
one site's current DOM) — both just return the page's text, and the agent
identifies the real questions and drafts grounded answers from it, the same
"selector, not writer" rule as the resume (see `prompts/apply_v1.md`).

A form that isn't automatically readable is never guessed at — that job's
Sheet row shows one of these specific statuses instead of one generic
"couldn't read it":

| Status | Meaning |
|---|---|
| ✅ Generated | Answers drafted and ready |
| 🔒 Login Required | The fetched page is a login wall, not the form |
| ❌ Closed | The posting itself says it's no longer accepting applications |
| ⚙️ Playwright Missing | The form needs the JS fallback, and it isn't installed — the cell includes the exact install command |
| 📄 No Essay Questions | A real, readable form with no free-text questions to draft |
| 🌐 Network Error | The fetch itself failed (DNS, timeout, connection refused) |
| 🛡️ Bot-Blocked | The fetch hit a Cloudflare-style bot-detection challenge, not the real form — never bypassed, only named |
| Manual review required | Fallback for any other, less common failure that doesn't match one of the specific cases above |

Finish any of these yourself with `careeros apply <job-id>` — the on-demand
skill, which can use your own real, already-logged-in browser (or accept
pasted questions) since you're present and chose to run it. It always
finishes by running `careeros publish <job-id>` automatically to upload the
result and patch that row — no separate command to remember.

The same on-demand `apply` skill also works for any job **below** threshold
that you want to pursue anyway — the automatic batch only covers Apply-tier.

### Personal/logistics questions — asked once, reused forever

Notice period, work authorization/visa status, salary expectations,
earliest start date, employment type — these aren't per-job content, they're
the same answer on every application. `prompts/apply_v1.md` checks
`profile.yaml`'s `comp` and `logistics` fields for them first; the first
time one is genuinely missing, it's asked (on-demand: right there in chat;
batch: once per distinct missing fact, after drafting the rest of that
pass, never once per job) and written straight into `.careeros/profile.yaml`
— see `templates/profile.example.yaml`'s `logistics:` block. No
`profile.version` bump needed for these, since they don't affect
gate/evaluate/resume/cover. Every later application, batch or on-demand,
reuses the saved answer automatically. Voluntary EEO/demographic
self-identification questions (race, gender, veteran/disability status) are
deliberately excluded from this — always left for you to answer yourself.

## Caching and prompt versioning

Every AI-stage output is cached, keyed on a fingerprint of everything that
could change the answer (job content hash + `profile.yaml` version + active
prompt version). Because the prompt version is *inside* the cache key,
`prompts/eval_v2.md` + a one-line config change busts only that stage's
cache — a re-run of `daily` with nothing else changed costs zero AI calls.

## What's built today (v1 vertical slice)

The full pipeline runs end to end: profile-driven segmented discovery merged
across multiple providers (Fantastic Jobs REST plus free RemoteOK/We Work
Remotely on by default; Naukri/Glassdoor/ZipRecruiter/Indeed/Foundit and the
legacy Apify-actor Fantastic Jobs backend available opt-in — see
`careeros/providers/README.md` for the evidence behind each), deterministic
normalize/dedupe/constraints/two-tier threshold,
the AI Gate and Evaluate stages with the file-based prepare/finalize contract,
resume/cover generation against your `profile.yaml`, automatic Application
Answers for Apply-tier jobs (background HTTP/Playwright form-reading, with
a specific status — Login Required, Closed, Playwright Missing, and so on —
in place of a generic failure), a zero-cost daily report render, automatic
Google Drive backup (PDF resume/cover/answers, flat layout, idempotent) for
Apply-tier jobs, and Google Sheets append with clickable per-job Drive
links, a hand-editable Status tracking column, and header/Score formatting.
`careeros init` seeds an example `profile.yaml` (a Product Manager persona
in `templates/`); replace it with your own facts — via `/careeros start`
(CV-first) or by editing directly — before your first real run.

## Roadmap

- Direct-API providers for Greenhouse, Ashby, Lever, Workday (no Apify
  actor needed — see `careeros/providers/README.md`)
- Incremental (`date_created_gte`) discovery — deferred out of the REST
  provider migration to keep it a pure parity swap. (LinkedIn/Wellfound/YC
  via the `active-jb` endpoint is now **live** — the default `endpoint:
  both` queries it alongside `active-ats`; see Pipeline step 1.)
- `careeros config get/set/show` — a validated, scriptable config editor so
  hand-editing `.careeros/config.yaml` YAML is never required (`careeros
  config` today is read-only)
- Richer profile sections (adaptive framing, negotiation scripts) — kept
  out of v1 deliberately to stay lean
- SQLite if Sheets-as-store ever hits real scaling limits
- Outcome tracking (applied/response/interview/offer) and calibrating
  scoring/artifacts on real conversion data

## Contributing

Adding a provider is one file — see `careeros/providers/README.md`. The
pipeline never imports a provider directly, so new sources never touch
`pipeline/`, `cli.py`, or any stage.

### Testing

```
pip install -e ".[dev]"
pytest careeros/tests/
```

Unit tests cover the deterministic logic that's genuinely subtle: hard
constraints (`constraints.py`), two-tier threshold selection, cache-key
stability, dedupe, the resume-truthfulness verbatim check, both Fantastic
Jobs providers' source-side-filter/transport/token-rotation wiring, a parity
test asserting the REST and legacy-actor providers map identical raw
records to an identical `Job` dict, the Sheets name-keyed read/write and
additive header migration, the daily-summary render, Drive artifact
upload/backfill/idempotency, and PDF rendering — the pure functions most
likely to silently regress. They do not (yet) cover `normalize.py`;
contributions there are welcome. CI (`.github/workflows/ci.yml`) runs the
suite on Python 3.11 and 3.12 for every push and PR.

## Attribution

The Final Evaluation rubric and matching methodology are adapted from
[Career Ops](https://career-ops.org). CareerOS deliberately diverges from
it on architecture (host-CLI-driven, not a standalone bot), output format
(structured JSON, not long markdown reports for every job), and cost model
(gate before evaluate, cache everything, resume/cover selection built on a
separate philosophy — see `prompts/voice-dna.md` and the truthfulness rule
embedded in `prompts/resume_v1.md`).

## License

MIT.
