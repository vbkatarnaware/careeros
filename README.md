# CareerOS

A supreme, high-quality job discovery and recommendation engine. Not an
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
 Apify          Google Sheets     (Drive — later)
 [deterministic] [deterministic]
```

CareerOS has no server and no database. The filesystem is the message bus:
every pipeline stage reads one JSON file and writes another, under
`.careeros/runs/<date>/`. That makes every run inspectable, resumable, and
cheap to re-run (unchanged inputs hit the cache, not the model).

## Pipeline

1. **Discover** — call a provider (Fantastic Jobs / Apify for v1), by default
   as one segmented query per profile work-mode tier rather than a single
   broad fetch (`pipeline/queryplan.py`). Deterministic.
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
7. **Threshold** — jobs scoring ≥ your configured bar (default 4.0) AND
   recommended "apply" AND still passing the constraints re-check get
   artifacts generated; everything evaluated still appears in the Sheet.
8. **Artifacts** — resume + cover letter (selected from `profile.yaml`,
   never invented, cache-checked) + a daily report (rendered from the eval
   JSON, zero AI).
9. **Sheets** — append one row per selected job. You open the Sheet and
   start applying.

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
| `careeros start` | Guided interview → `.careeros/profile.yaml` |
| `careeros daily` (alias `scan`) | Run the full daily pipeline |
| `careeros prep <job-id>` | Level-2 deep interview-prep report |
| `careeros apply <job-id>` | Detect ATS, draft answers to pasted questions |
| `careeros config` | Show resolved config |
| `careeros providers` | List registered discovery providers |

Developer/debug commands — each stage runnable standalone against a run
directory, without re-running the whole pipeline:

`discover` · `normalize` · `dedupe` · `constraints` · `gate` · `evaluate` ·
`threshold` · `artifacts` · `sheets append` · `render-report` ·
`lint <file>` · `verify-resume <file>`

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

## Example run

```
$ careeros init
Wrote .careeros/config.yaml
Wrote .careeros/profile.yaml

$ careeros start        # or hand-edit .careeros/profile.yaml directly

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
[threshold] 19 evaluated -> 4 >= 4.0 (top: 4.6)
[artifacts:finalize] 4 job(s), 8 artifact(s) verified, 8 newly cached.
[sheets:append] wrote 4 row(s).

4 jobs scored above threshold. Top match: Senior PM at Acme (4.6) — strong
role fit, remote, comp in range. See your Sheet for all 4 with resumes and
cover letters generated.
```

## Google Sheets schema

One append-only `Jobs` worksheet:

`Date · Company · Role · Score · Confidence · Recommendation · Apply URL ·
Resume Path · Cover Letter Path · Report Path · Source · Hiring Contact ·
Contact LinkedIn · Contact Email · Job ID`

`Job ID` is the join key `prep`/`apply` use to look a row back up.

## Caching and prompt versioning

Every AI-stage output is cached, keyed on a fingerprint of everything that
could change the answer (job content hash + `profile.yaml` version + active
prompt version). Because the prompt version is *inside* the cache key,
`prompts/eval_v2.md` + a one-line config change busts only that stage's
cache — a re-run of `daily` with nothing else changed costs zero AI calls.

## What's built today (v1 vertical slice)

The full pipeline runs end to end: profile-driven segmented discovery through
a real Apify provider, deterministic normalize/dedupe/constraints/threshold,
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
dedupe, the resume-truthfulness verbatim check, and the provider's
source-side-filter/token-rotation wiring — the pure functions most likely to
silently regress. They do not (yet) cover `normalize.py`, `sheets.py`, or
`report.py`; contributions there are welcome. CI (`.github/workflows/ci.yml`)
runs the suite on Python 3.10 and 3.12 for every push and PR.

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
