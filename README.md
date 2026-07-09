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
| Application Answers | profile facts + eval + pasted questions | fabricate experience |

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
   Review/near-miss list, cost-per-selected-job). Zero AI. The KPI is cost
   per interview-worthy job, supply-aware — a day with 0 selected is a
   legitimate outcome, not a failure, and CareerOS never lowers the quality
   bar just to hit a job count.
10. **Drive** *(optional, off by default)* — additive backup of shortlisted
    artifacts to Google Drive via your own OAuth desktop grant. Any failure
    here only warns; it never blocks the rest of the pipeline.
11. **Sheets** — append one row per selected job (with a Drive Folder link if
    step 10 ran). You open the Sheet and start applying.

Two more commands exist outside the daily loop, deliberately:

- **`careeros prep <job-id>`** — a full interview-prep report, generated
  only when you ask for it, expanding (never re-deriving) the eval.
- **`careeros apply <job-id>`** — application-answer drafting, which can
  only run once you've opened the real application and pasted its
  questions. CareerOS never generates these during `daily`, because the
  questions don't exist yet at that point.

## Commands

| Command | Description |
|---|---|
| `careeros init` | Scaffold `.careeros/` (config, profile template) |
| `careeros start` | Guided onboarding → `.careeros/profile.yaml` + discovery goal/plan. Opens by asking for your CV (optional — `skip` to answer questions instead) |
| `careeros doctor` | First-run checklist: Python version, profile, discovery credentials, Sheets, Drive. Never modifies anything |
| `careeros daily` (alias `scan`) | Run the full daily pipeline |
| `careeros prep <job-id>` | Level-2 deep interview-prep report |
| `careeros apply <job-id>` | Detect ATS, draft answers to pasted questions |
| `careeros config` | Show resolved config, incl. the discovery quota-guard's current recommendation |
| `careeros providers` | List registered discovery providers |

Developer/debug commands — each stage runnable standalone against a run
directory, without re-running the whole pipeline:

`discover` · `normalize` · `dedupe` · `constraints` · `gate` · `evaluate` ·
`threshold` · `artifacts` · `summary` · `drive` · `sheets append` ·
`render-report` · `lint <file>` · `verify-resume <file>`

## Folder structure

```
careeros/
├── careeros/            # the deterministic Python toolkit
│   ├── cli.py
│   ├── config.py  models.py  cache.py  runmeta.py  lint.py  report.py  sheets.py
│   ├── providers/       # one file per discovery source
│   └── pipeline/        # queryplan, normalize, dedupe, constraints, threshold
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

1. **Set the discovery provider's credentials.** `careeros` ships defaulted
   to `provider: fantastic-jobs` — the official Fantastic Jobs REST API
   (recommended for all new users). Pick one transport in
   `.careeros/config.yaml`'s `api:` block and export the matching key:
   - `api.transport: direct` → `export FANTASTIC_API_KEY=...` ([developer.fantastic.jobs](https://developer.fantastic.jobs))
   - `api.transport: rapidapi` → `export RAPIDAPI_KEY=...` (RapidAPI's "Active Jobs DB")

   *(Prefer a no-code/Zapier-style setup instead? Set `provider:
   fantastic-jobs-actor` and `export APIFY_TOKEN=...` — the legacy Apify
   actor backend, kept as a reference/advanced option; see
   `careeros/providers/README.md`. For multiple accounts, set
   `export APIFY_TOKENS=tok1,tok2,...` — a comma-separated rotation pool that
   is preferred over `APIFY_TOKEN` and auto-rotates when a token's budget is
   exhausted.)*
2. **Set up your profile**: `/careeros start` inside your host coding CLI —
   opens by asking you to paste your CV (optional; `skip` to answer
   questions instead), then extracts your facts into `.careeros/profile.yaml`
   and asks your interviews/week goal + Fantastic Jobs plan to recommend a
   daily discovery limit. Or hand-edit `.careeros/profile.yaml` directly —
   see `templates/profile.example.yaml`.
3. **Set up Google Sheets** (the daily results destination): a spreadsheet id
   + service-account credentials path in `config.yaml`'s `sheets:` block.
   First time with Google Cloud? Follow the click-by-click walkthrough in
   **[docs/google-setup.md](docs/google-setup.md)** — it covers creating the
   service account, downloading the key, and the easily-missed step of
   *sharing your Sheet with the service account's email*. (Optional Google
   Drive backup is covered there too.)
4. **Check your setup**: `careeros doctor` — a green/red checklist for
   Python version, profile, discovery credentials, Sheets, and Drive. Fixes
   nothing itself; just tells you exactly what's missing.
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

One append-only `Jobs` worksheet:

`Date · Company · Company LinkedIn · Role · Score · Confidence ·
Recommendation · Tier · Apply URL · Resume Path · Cover Letter Path ·
Report Path · Source · Hiring Contact · Contact LinkedIn · Contact Email ·
Drive Folder · Notes · Job ID`

`Tier` is `Apply` or `Consider` (see Pipeline step 7); a Consider row has
blank artifact/Drive cells and a `Notes` reason it scored below 4.0. Columns
are located by header **name**, not position, and any missing column is added
automatically — so a Sheet created by an earlier version self-migrates on the
next run without losing data or breaking dedupe.

`Job ID` is the join key `prep`/`apply` use to look a row back up. `Company
LinkedIn` and `Drive Folder` are populated when the underlying data exists
(Fantastic Jobs exposes the former on ~100% of postings at zero extra cost;
the latter only if Drive backup — see below — is enabled and succeeds).

## Google Drive backup (optional)

Off by default. When `drive.enabled: true`, `careeros drive` uploads each
shortlisted job's resume/cover/report — plus the day's `run.json` and
`summary.md` — into `<your root folder>/YYYY-MM-DD/<Company>/` as an
**additive** backup; your local `.careeros/runs/` Markdown is never moved,
replaced, or read back by any pipeline stage. Needs the optional extra:

```
pip install -e ".[drive]"
```

and an OAuth **Desktop app** client secret (Google Cloud Console →
Credentials → Create Credentials → OAuth client ID → Desktop app) — not a
service account, since uploads land in your own personal Drive quota. The
first run opens a one-time browser consent; after that, a cached refresh
token (`drive.token_path`, gitignored) makes every later run silent. Any
Drive failure (auth, network, quota) only prints a warning — discovery,
evaluation, Sheets, and every other stage run exactly as if Drive were off.

## Caching and prompt versioning

Every AI-stage output is cached, keyed on a fingerprint of everything that
could change the answer (job content hash + `profile.yaml` version + active
prompt version). Because the prompt version is *inside* the cache key,
`prompts/eval_v2.md` + a one-line config change busts only that stage's
cache — a re-run of `daily` with nothing else changed costs zero AI calls.

## What's built today (v1 vertical slice)

The full pipeline runs end to end: profile-driven segmented discovery through
the Fantastic Jobs REST API (a legacy Apify-actor provider remains available
— see `careeros/providers/README.md`), deterministic
normalize/dedupe/constraints/threshold,
the AI Gate and Evaluate stages with the file-based prepare/finalize contract,
resume/cover generation against your `profile.yaml`, a zero-cost daily report
render, and Google Sheets append. `careeros init` seeds an example
`profile.yaml` (a Product Manager persona in `templates/`); replace it with
your own facts — via `careeros start` or by editing directly — before your
first real run.

## Roadmap

- Google Drive upload + PDF rendering for resume/cover (Markdown only today)
- Direct-API providers for Greenhouse, Ashby, Lever, Workday (no Apify
  actor needed — see `careeros/providers/README.md`)
- Incremental (`date_created_gte`) discovery — deferred out of the P2.7 REST
  migration to keep it a pure parity swap. (LinkedIn/Wellfound/YC via the
  `active-jb` endpoint is now **live** — the default `endpoint: both` queries
  it alongside `active-ats`; see Pipeline step 1.)
- Per-ATS application-question scraping (today: paste them manually)
- Richer profile sections (adaptive framing, negotiation scripts) — kept
  out of v1 deliberately to stay lean
- SQLite if Sheets-as-store ever hits real scaling limits

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
constraints (`constraints.py`), threshold selection, cache-key stability,
dedupe, the resume-truthfulness verbatim check, both Fantastic Jobs
providers' source-side-filter/transport/token-rotation wiring, and a parity
test asserting the REST and legacy-actor providers map identical raw
records to an identical `Job` dict — the pure functions most likely to
silently regress. They do not (yet) cover `normalize.py`, `sheets.py`, or
`report.py`; contributions there are welcome. CI (`.github/workflows/ci.yml`)
runs the suite on Python 3.11 and 3.12 for every push and PR.

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
