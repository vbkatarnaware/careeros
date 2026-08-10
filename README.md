# CareerOS

[![CI](https://github.com/vbkatarnaware/careeros/actions/workflows/ci.yml/badge.svg)](https://github.com/vbkatarnaware/careeros/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/vbkatarnaware/careeros)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

An AI-powered, deterministic job discovery and recommendation engine. Not an
application bot.

CareerOS finds jobs, scores them against your real experience, and writes a
readable digest of the results — resume and cover letter included for every
strong match. The KPI is simple: **more interviews, for the least amount of
AI and compute cost.** Runs entirely on your own machine, with **no signup
and no Google account required** to try it.

## Quickstart (60 seconds, no Google account needed)

Requires Python 3.11+ and a host coding CLI (Claude Code, Codex, Gemini CLI,
OpenCode, …) — CareerOS's AI reasoning steps (scoring, resume tailoring) run
*inside* your coding CLI, not as a separate service. The `careeros` Python
package is the deterministic half; see [Architecture](#architecture).

```
git clone https://github.com/<you>/careeros.git
cd careeros
pip install -e ".[ats-dataset]"
careeros init
```

That's the whole setup. No API key, no signup. The default discovery
source (`ats-dataset`) reads a free, open, no-auth dataset covering 65 real
ATS platforms (4.85M jobs, 79,906 companies, updated daily), so `apply_url`
is always the company's own real application page, never a LinkedIn mirror.
(A paid Fantastic Jobs option exists too, disabled by default — see
Installation below.)

Then, inside your host coding CLI:

```
/careeros start     # guided onboarding — paste your CV (or `skip`), set your goal
/careeros daily      # discover, score, and generate resumes/covers for today's matches
```

That's it — no Google Sheet, no OAuth, no Drive setup. Every run writes a
plain-Markdown digest to `.careeros/results/latest/summary.md`, linking
straight to each match's rendered resume/cover PDF. Want one specific job
(any score, not just today's Apply-tier matches) fully generated and
published right now? `/careeros job <job-id>`.

Want a shared, clickable Google Sheet + Drive backup instead of (or in
addition to) the local digest? It's fully optional — `/careeros start` asks,
or see **[docs/google-setup.md](docs/google-setup.md)** any time later.

## Example run

```
$ /careeros daily        # run inside Claude Code / Codex / Gemini CLI / etc.
[discover] ats-dataset: 84 raw items across 5 ATS platforms (38.2s)
[normalize] 84 raw -> 81 jobs (0.1s)
[dedupe] 81 in -> 47 unique, 34 dropped (in-run: 2, history: 30, sheet: 2)
[constraints] 47 in -> 41 eligible, 6 hard-rejected (0.0s)
[gate:finalize] 41 in -> 19 kept, 22 dropped.
[evaluate:finalize] 19 evaluations valid and cached.
[threshold] 19 evaluated -> 4 APPLY (>= 4.0), 6 CONSIDER ([3.5, 4.0))
[artifacts:finalize] 4 job(s), 8 artifact(s) verified, 8 newly cached.
[summary] wrote .careeros/results/2026-07-14/summary.md (also: .careeros/results/latest/)

4 jobs scored above threshold. Top match: Senior PM at Acme (4.6) — strong
role fit, remote, comp in range, real application link (not LinkedIn). See
.careeros/results/latest/summary.md for all 4 with resumes and cover letters
generated, plus 6 near-misses under Consider for visibility.
```

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
generated once and reused. The Resume goes one step further: it may *reword*
a selected bullet's language to mirror a JD's keywords, and it also *selects*
— per job, by JD relevance — which companies, which of each company's
bullets, which skills, and which 2-3 side projects appear, so a resume for a
data-heavy role and a resume for a growth role pull genuinely different
material from the same profile.yaml. But every number/entity in a reworded
bullet must survive unchanged, and every selected company/project name must
be a real profile.yaml entry — deterministic checks enforce both, so
"selector" never quietly becomes "inventor" and a typo never silently drops
real content. It renders through a real Typst layout (single column, real
selectable text, no ligature corruption) that auto-fits to exactly one page.
The one place this bends further on purpose is the Deep Report, which
legitimately needs external research the cheap daily eval was never meant to
gather — that research is additive and clearly separated from the inherited,
non-recomputed fit judgment.

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
   │  results/latest/ ← the stable local digest     │
   └─────────────────────────────────────────────┘
           │
   ┌───────┴───────┬──────────────────┬─────────────┐
   ▼               ▼                  ▼             ▼
 ats-dataset      Local digest     Sheets (opt.)  Drive (opt.)
 [deterministic]  [deterministic]  [deterministic] [deterministic]
```

CareerOS has no server and no database. The filesystem is the message bus:
every pipeline stage reads one JSON file and writes another, under
`.careeros/runs/<date>/`. That makes every run inspectable, resumable, and
cheap to re-run (unchanged inputs hit the cache, not the model).

**Onboarding a new host CLI?** Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) —
the canonical repo map, the deterministic/reasoning boundary, secrets
handling, and the Failure Handling Principle every stage follows.
`CLAUDE.md` is a thin redirect to it for CLIs that auto-load a per-tool file.

## Pipeline

1. **Discover** — call a provider (by default `ats-dataset`, a free,
   open, no-signup dataset covering 65 real ATS platforms — Greenhouse,
   Lever, Ashby, Darwinbox, Keka, Workday, and more; see
   `careeros/providers/README.md`), filtered by your own `profile.yaml`
   (role titles, work-mode/location tiers, years-of-experience floor) with
   zero server-side query cost. `apply_url` is always the company's real
   application page, never a LinkedIn mirror. An optional paid alternative,
   the Fantastic Jobs REST API (`active-ats` + `active-jb`, incl.
   LinkedIn/YC/Wellfound coverage), remains fully available — disabled by
   default, one config flip to switch. Deterministic.
2. **Normalize** — map to the universal `Job` schema. Deterministic.
3. **Dedupe** — drop jobs already seen this run, in a prior run, or already
   recorded (Sheet, if enabled). Deterministic.
4. **Constraints** — hard-reject on the two objective deal-breakers, location
   and salary, before any AI is spent. Deterministic (`pipeline/constraints.py`).
5. **AI Gate** — cheap, batched keep/drop triage against your profile's
   targets and deal-breakers. Bias to keep; recall over precision.
6. **Evaluate** — the real reasoning step. Scores against a Career
   Ops-derived rubric, writes structured JSON only (no long report). This
   file is the source of truth for everything downstream.
7. **Threshold** — two tiers, both configurable. **Apply** (score ≥ 4.0 AND
   recommended "apply" AND passing the constraints re-check) gets the full
   pipeline: resume, cover letter, report, and (if configured) Drive + a
   Sheet row. **Consider** (3.5 ≤ score < 4.0) gets a digest/Sheet entry
   only — score plus a one-line reason it fell short — with **no** AI
   artifacts by default, so near-misses stay visible at zero extra AI cost
   (run `/careeros job <job-id>` on one you want to pursue anyway — see
   below). Below 3.5 is omitted. A hard constraint failure
   (location/salary deal-breaker) is always omitted, never shown as
   Consider. The score and recommendation can never disagree: `score`
   means applyability, not just fit quality, so a job blocked by a
   deal-breaker or a stated preference (e.g. onsite outside your accepted
   cities) never shows as a green Apply-tier score — `evaluate --finalize`
   deterministically caps the score below threshold whenever the
   recommendation is "skip", even if the raw fit alone would have cleared it.
8. **Artifacts** — resume + cover letter (selected from `profile.yaml`,
   never invented, cache-checked) + a daily report (rendered from the eval
   JSON, zero AI).
9. **Summary** — a deterministic digest (funnel, the Apply list, the
   Consider/near-miss list, cost-per-selected-job), written to BOTH
   `.careeros/runs/<date>/summary.md` (the internal pipeline trail) and the
   stable `.careeros/results/<date>/summary.md` + `.careeros/results/latest/`
   (what you're actually meant to read — with relative links straight to
   each Apply job's rendered resume/cover PDF). Zero AI. The KPI is cost per
   interview-worthy job, supply-aware — a day with 0 selected is a
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
    artifacts (Resume/Cover as PDF, Application Answers/Evaluation/Deep
    Report as Markdown, Deep Report only if present) into one flat Drive
    folder via your own OAuth desktop grant. Idempotent (re-uploads update
    in place). Any failure here only warns; it never blocks the rest of the
    pipeline.
11. **Sheets** *(optional, off by default)* — append one row per Apply job
    (with per-file Drive links if step 10 ran — Resume/Cover/Evaluation/Deep
    Report/Application Answers, no shared-folder link) and one row per
    Consider job (score + reason only). Off by default; the local digest
    (step 9) is the record when it's off.

Two more commands exist outside the daily loop, deliberately:

- **`careeros job <job-id>`** — give ONE job the full Apply-tier treatment
  (resume, cover, report, application answers, auto-published) regardless
  of its actual score — the fastest path from "I found a Consider-tier
  near-miss I actually want" to a finished application, without waiting for
  a re-run. See `skills/job.md`.
- **`careeros prep <job-id>`** — a full interview-prep report, generated
  only when you ask for it, expanding (never re-deriving) the eval.

`careeros apply <job-id>` (application-answer drafting for one job, any
score, using your own real logged-in browser or pasted questions — the
manual counterpart to step 9a's automatic Apply-tier batch) and
`careeros publish <job-id>` (push one job's current artifacts to Drive/Sheet
right now, without waiting for the next `daily`) are the lower-level pieces
`job <job-id>` composes for you automatically — call them directly only if
you want just one piece of the full treatment.

## Commands

| Command | Description |
|---|---|
| `careeros init` | Scaffold `.careeros/` (config, profile template) |
| `careeros start` | Guided onboarding → `.careeros/profile.yaml` + discovery goal/plan + Sheets/Drive-or-local choice. Opens by asking for your CV (optional — `skip` to answer questions instead) |
| `careeros doctor` | First-run checklist: Python version, profile, discovery credentials for every enabled provider, and (if enabled) Sheets/Drive — plus the last discovery failure, if any (from local state — never a live API call by default). Never modifies anything, never fails just because Sheets/Drive are off. If Fantastic Jobs is enabled, add `--live` to verify it against its real API right now (a small, bounded amount of real quota) instead of trusting local/stored state alone |
| `careeros daily` (alias `scan`) | Run the full daily pipeline |
| `careeros job <job-id>` | Full Apply-tier treatment for ONE job, any score — resume, cover, report, application answers, auto-published |
| `careeros prep <job-id>` | Level-2 deep interview-prep report |
| `careeros apply <job-id>` | Detect ATS, draft application answers for one job (any score) using your own real browser or pasted questions |
| `careeros publish <job-id>` | Upload one job's current artifacts to Drive/Sheet (if configured) and refresh the local digest — use after `prep`/`apply <job-id>` so the result shows up without a full `daily` run |
| `careeros config` | Show resolved config, incl. the discovery quota-guard's current recommendation |
| `careeros providers` | List registered discovery providers |
| `careeros registry init/add/list/verify/import-history/stats` | Manage the ATS company/board registry behind the `greenhouse`/`lever`/`ashby` providers — see [docs/ats-registry.md](docs/ats-registry.md) |
| `careeros sheets migrate` | Apply the current header/formatting/dropdown pass to an existing Sheet right now, instead of waiting for the next `daily` run |
| `careeros backfill-drive` | Advanced: add Drive links to Apply-tier Sheet rows that predate Drive automation. Idempotent, defaults to `--dry-run` |
| `careeros --version` | Print the installed version and exit |

`careeros --help` groups these by purpose (Setup, Daily, Per-job, Advanced)
and hides the internal pipeline-stage commands (`discover`, `gate`,
`evaluate`, `artifacts`, `threshold`, `sheets *`, and similar) from the
top-level listing — they're still fully runnable standalone (e.g.
`careeros discover --help`) for debugging one stage against a run directory
without re-running the whole pipeline, they're just not clutter for a
first-time user. See `AGENT_GUIDE.md` for the full stage list.

## Folder structure

```
careeros/
├── careeros/            # the deterministic Python toolkit
│   ├── cli/               # Typer commands, one module per concern
│   ├── config.py  models.py  cache.py  runmeta.py  lint.py  report.py
│   ├── sheets.py  drive.py  pdf.py  budget.py  typst_render.py
│   ├── apply/            # Application Answers: HTTP/Playwright form-reading (browser.py)
│   ├── providers/        # one file per discovery source
│   │   ├── ats_dataset.py  # the default source (v1.9) — see below
│   │   └── legacy/         # superseded-but-working sources (fantastic_jobs.py, ats/, discovery/)
│   └── pipeline/         # queryplan, normalize, dedupe, constraints, threshold
├── prompts/              # AI step templates, versioned (gate_v1.md, ...)
├── skills/               # host-CLI playbooks (daily, start, prep, apply, job)
├── schemas/              # JSON Schema — the actual source-of-truth contracts
├── templates/            # example profile/config, safe to commit
├── seeds/                # companies.yaml — curated ATS board seeds for the legacy registry, PR-reviewable
└── .careeros/            # your local state (gitignored): profile, cache, runs, results
```

## Installation

Requires Python 3.11+.

```
git clone https://github.com/<you>/careeros.git
cd careeros
pip install -e .
```

That installs the `careeros` CLI. The default discovery source needs one
more (still free, still no signup) extra:

```
pip install -e ".[ats-dataset]"   # pandas + pyarrow + httpx — the ats-dataset provider
```

Two things you'll also need, neither installed by pip:

- **A host coding CLI** (Claude Code, Codex, Gemini CLI, OpenCode, …) — this
  is what actually runs `/careeros daily` and performs the AI Gate/Evaluate
  reasoning steps. CareerOS's own Python package is the deterministic half;
  see [Architecture](#architecture).
- **Nothing else, by default.** No API key required. (If you'd rather use
  the optional paid Fantastic Jobs source instead/as well — e.g. for its
  LinkedIn/YC/Wellfound coverage via `active-jb` — see step 1 below.)

## Setting up your profile and discovery source

1. **Discovery source — works out of the box.** `careeros` ships with
   `ats-dataset` enabled: a free, open, no-signup dataset covering 65 real
   ATS platforms (Greenhouse, Lever, Ashby, Darwinbox, Keka, Workday, and
   more — 4.85M jobs, 79,906 companies, updated daily). Nothing to
   configure; it reads its filters straight from your `profile.yaml` (set
   up in step 2). See `careeros/providers/README.md`'s "The shipped
   provider" for the config keys you *can* override (which ATS platforms to
   pull, daily limit, freshness window).

   *(Want the optional paid alternative instead/as well? Set
   `providers.fantastic-jobs.enabled: true` in `.careeros/config.yaml`, pick
   a transport in the `api:` block, and export the matching key:
   `api.transport: direct` → `FANTASTIC_API_KEY` from
   [developer.fantastic.jobs](https://developer.fantastic.jobs); `rapidapi`
   → `RAPIDAPI_KEY`. Several other sources — RemoteOK, We Work Remotely,
   Glassdoor, ZipRecruiter, Naukri, Foundit, Indeed, and a curated
   Greenhouse/Lever/Ashby registry — were tried across v1.0-v1.8 and
   superseded; all still work, disabled, in `careeros/providers/legacy/`.
   See `careeros/providers/README.md` for the full history and how to
   re-enable any of them.)*
2. **Set up your profile**: `/careeros start` inside your host coding CLI —
   opens by asking you to paste your CV (optional; `skip` to answer
   questions instead), then extracts your facts into `.careeros/profile.yaml`
   (role titles, work-mode/location preferences, years of experience, salary
   floor — everything `ats-dataset` filters on), asks your interviews/week
   goal and daily job limit, and asks whether you want a Google Sheet +
   Drive or to stay local-only (see Quickstart above; nothing extra to set
   up either way). Or hand-edit `.careeros/profile.yaml` directly — see
   `templates/profile.example.yaml`. **Change the daily limit anytime** via
   `providers.ats-dataset.limit` in `.careeros/config.yaml`.
3. **Check your setup**: `careeros doctor` — a green/red checklist for
   Python version, profile, discovery credentials, and (if enabled)
   Sheets/Drive. Fixes nothing itself; just tells you exactly what's
   missing — and never fails just because Sheets/Drive are off.
4. **Run it**: `/careeros daily` inside your host coding CLI.

## Local-first results digest

Every `daily` run writes a plain-Markdown digest to
`.careeros/results/<date>/summary.md`, with `.careeros/results/latest/`
always pointing at the newest one — funnel counts, the Apply list (with
relative links straight to each job's `resume.pdf`/`cover.pdf`), the
Consider/near-miss list, and cost-per-selected-job. This is the full record
of a run with zero Google setup; enabling Sheets/Drive below is an upgrade
to a shareable, clickable tracker, not a requirement to get value out of
CareerOS.

## Google Sheets schema (optional)

Set `sheets.enabled: true` (see [docs/google-setup.md](docs/google-setup.md))
to also get one `Jobs` worksheet. New rows are inserted directly below the
header, not appended at the bottom — each day's newest run reads at the TOP,
so you never have to scroll past a growing history to find today's jobs.
Within a single run's batch, rows keep their normal Apply-then-Consider
order; across runs, later `daily` runs stack above earlier ones.

`Date · Company · Role · Score · Tier · Recommendation · Confidence ·
Apply URL · Status · Resume (Drive) · Cover Letter (Drive) ·
Evaluation (Drive) · Deep Report (Drive) · Application Answers (Drive) ·
Notes · Source · Company LinkedIn · Hiring Contact · Contact LinkedIn ·
Contact Email · Job ID`

`Tier` is `Apply` or `Consider` (see Pipeline step 7); a Consider row has
blank artifact/Drive cells and a `Notes` reason it scored below 4.0. `Status`
is a dropdown (data validation, not free text) you update by hand as you
actually apply: `Not Applied` (the default on every new row), `Applied`,
`Received Call`, `Interview`, `After Interview`, `Rejected`, `Expired`. It's
yours to track — the pipeline only ever sets the default on a NEW row and
never overwrites it afterward, exactly like `Notes`.

Columns are located by header **name**, not position, and any missing column
is added (deprecated ones removed) automatically — so a Sheet created by an
earlier version self-migrates on the next `sheets append` without losing
data or breaking dedupe; run `careeros sheets migrate` to apply that same
pass right now instead of waiting for the next `daily` run — this also
sorts an older Sheet's existing rows by Date descending, a one-time fix
for history that was written bottom-up. The header row is bold and frozen,
`Score` is conditionally colored — light green at 4.0 and above, light
yellow below — and `Status` both shows its dropdown arrow and colors each
cell by value (grey Not Applied, green Applied, blue Received Call, amber
Interview, purple After Interview, red Rejected, dark grey Expired) — all
applied automatically so you can scan Apply-quality and application
progress at a glance, without reading every cell.

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
Apply-tier job's Resume + Cover Letter (as **PDF** — the only two artifacts
PDF is ever attempted for), Application Answers (Markdown, always — see
below), Evaluation, and Deep Report (if you've run `prep` on it) — plus the
day's `run.json` and `summary.md` — into **one flat folder**
(`drive.root_folder_id`) as an **additive** backup; your local
`.careeros/runs/` Markdown is never moved, replaced, or read back by any
pipeline stage. Files are named `Company - Role - Resume.pdf`,
`Company - Role - Cover Letter.pdf`,
`Company - Role - Application Answers.md`, `Company - Role - Evaluation.md`,
`Company - Role - Deep Report.md` — no per-company or per-job subfolders
(set `drive.date_subfolder: true` if you'd rather group each day's uploads
under a `YYYY-MM-DD/` subfolder instead). Consider-tier jobs never generate
artifacts by default, so they never upload anything (unless you ran
`careeros job <job-id>` on one — then its artifacts upload like any other).

Re-running `daily` (or `backfill-drive`/`publish`) for the same job updates
its existing files in place rather than duplicating them — uploads are
idempotent. Needs one optional extra:

```
pip install -e ".[drive]"
```

This installs the Google API client + OAuth deps (required for any upload
at all) **and** `typst` + `pypdf` for Resume/Cover PDF rendering — one
extra, everything by default, nothing else to install separately. `typst`
(the primary renderer, `careeros/typst_render.py`) bundles its own compiler
binary — pure pip install, no LaTeX/pango/browser system dependency — and
renders a densely-filled, one-page, ATS-clean design (single column,
ligatures disabled, real selectable text). Rendering happens **locally**,
at `careeros artifacts --finalize` time, so `artifacts/<job-id>/resume.pdf`
exists on disk whether or not Drive is even enabled. If PDF rendering is
ever unavailable anyway (a corrupted install, or an edge-case render
failure), it falls back to a plainer legacy renderer, then to Markdown, with
a warning printed at each step down — Drive backup still works either way;
`careeros doctor` also flags a missing `typst` proactively when Drive is
enabled.

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
If a row's local `resume.json`/`cover.md` no longer exist on disk (an old
run directory was cleaned up), that row is listed as **needing
regeneration** instead of inventing content — nothing is ever fabricated.

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
digest/Sheet entry shows one of these specific statuses instead of one
generic "couldn't read it":

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
result and refresh the local digest — no separate command to remember.

The same on-demand `apply` skill also works for any job **below** threshold
that you want to pursue anyway — the automatic batch only covers Apply-tier
(or use `careeros job <job-id>` for the full treatment in one shot — see
Pipeline above).

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

## What's built today

The full pipeline runs end to end: profile-driven discovery from a free,
open, no-signup ATS dataset (`ats-dataset`, v1.9 — apply_url is always the
company's real application page, never LinkedIn; several earlier sources,
including a paid REST aggregator, were tried and superseded — see
`careeros/providers/README.md` for the evidence and how to re-enable any of
them), deterministic normalize/dedupe/constraints/two-tier threshold,
the AI Gate and Evaluate stages with the file-based prepare/finalize contract,
resume/cover generation against your `profile.yaml`, automatic Application
Answers for Apply-tier jobs (background HTTP/Playwright form-reading, with
a specific status — Login Required, Closed, Playwright Missing, and so on —
in place of a generic failure), a zero-cost daily report render, a stable
local results digest (`.careeros/results/latest/`, zero Google setup
needed), a per-job `job <job-id>` command for full on-demand treatment of
any single job at any score, optional Google Drive backup (PDF
resume/cover/answers, flat layout, idempotent) and optional Google Sheets
append with clickable per-job Drive links, a hand-editable Status tracking
column, and header/Score formatting.
`careeros init` seeds an example `profile.yaml` (a Product Manager persona
in `templates/`); replace it with your own facts — via `/careeros start`
(CV-first) or by editing directly — before your first real run.

v1.8 added `greenhouse`/`lever`/`ashby` — direct-API providers that read
their company list from a small SQLite registry (derived from the
git-tracked `seeds/companies.yaml`) instead of config-driven search filters,
so `apply_url` can be a company's real application page instead of a
LinkedIn mirror. Superseded by `ats-dataset` (v1.9, same benefit at 65
platforms instead of 3, zero registry to curate), and **removed in v2.0**:
every seeded board was permanently `status='unverified'` on this project's
own checkout, and those providers only ever read `status='live'` boards —
they were structurally skip-only and had never returned a single job.

v1.9 replaced Fantastic Jobs (paid, quota-limited, 100% LinkedIn apply
URLs for this profile) with `ats-dataset` as the default: a free, open,
no-signup dataset covering 65 real ATS platforms, updated daily, filtered
entirely from your own `profile.yaml`. Two deterministic filters (a
seniority-title cut and a JD years-of-experience check) replace what
Fantastic Jobs' server-side experience-level param used to do, and the AI
Gate stage now reads a trimmed description (`gate_description_max_chars`)
to keep the higher volume cheap.

v2.0 fixed a geography reachability gap (`row_matches_geo` couldn't match
remote tiers on the ~53% of rows where the dataset's `is_remote` column is
unpopulated — measured 100% on greenhouse), added an `ats-watchlist`
provider (Layer 2A: a small user-supplied list of specific companies,
scraped live via `ats-scrapers`' own adapters, for companies the hosted
snapshot doesn't carry at all — see
[docs/ats-registry.md](docs/ats-registry.md)), and added a per-company AI
Gate fairness cap with rotation (`max_jobs_per_company_per_run`) so one
high-volume employer can't dominate a day's AI evaluation budget. The v1.8
trio and its SQLite registry were removed as part of this release —
`fantastic-jobs` remains registered, disabled, for instant rollback.

## Roadmap

- Workday direct-API provider
- Incremental (`date_created_gte`) discovery for the legacy Fantastic Jobs
  provider — deferred to keep that migration a pure parity swap.
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
`pipeline/`, `careeros/cli/`, or any stage.

Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) before touching pipeline code or AI
prompts — it's the canonical source for the rules that actually govern this
codebase: the deterministic/reasoning boundary, and that Gate/Evaluate/
Resume/Cover/Application-Answer output must always come from real
per-job reasoning, never a script standing in for it.

### Testing

```
pip install -e ".[dev]"
pytest careeros/tests/
```

Unit tests cover the deterministic logic that's genuinely subtle: hard
constraints (`constraints.py`), two-tier threshold selection, cache-key
stability, dedupe, the resume-truthfulness fact-preservation check (numbers,
company/project names, and skill names all verified against `profile.yaml`
— see `verify_resume_facts` in `careeros/lint.py`), the `ats-dataset`
provider's profile-driven filtering (title/geo/seniority/years-of-experience,
NaN-safety, salary-period ambiguity), the legacy Fantastic Jobs provider's
source-side-filter and transport wiring, the Sheets name-keyed
read/write and additive header migration, the daily-summary render (incl.
the local-first results digest), Drive artifact upload/backfill/idempotency,
and PDF rendering — the pure functions most likely to silently regress. They
do not (yet) cover `normalize.py`; contributions there are welcome. CI
(`.github/workflows/ci.yml`) runs the suite on Python 3.11 and 3.12 for
every push and PR.

## Attribution

The Final Evaluation rubric and matching methodology are adapted from
[Career Ops](https://career-ops.org). CareerOS deliberately diverges from
it on architecture (host-CLI-driven, not a standalone bot), output format
(structured JSON, not long markdown reports for every job), and cost model
(gate before evaluate, cache everything, resume/cover selection built on a
separate philosophy — see `prompts/voice-dna.md` and the fact-preservation
rule embedded in the active resume prompt, `prompts/resume_v4.md`).

## License

MIT.
