# /careeros daily

The full daily pipeline. Run this in your host coding CLI (Claude Code,
Codex, Gemini CLI, OpenCode, etc.) — it drives a sequence of deterministic
`careeros` CLI calls interleaved with a few reasoning steps you (the agent)
perform directly. `careeros scan` is an alias for the same pipeline.

Target: end to end in 15-30 minutes. Most of that time is the deterministic
stages and I/O; the reasoning steps are deliberately narrow and batched to
keep token spend low.

## Step 1 — Discover (deterministic)

```
careeros discover --date {today}
```

By default this runs one segmented query per `profile.yaml`'s
`work_mode_priority` tier (e.g. global-remote, India-remote — all onsite
cities in `location.onsite_ok` are consolidated into a single query, not one
per city), each searching all `role_priorities` at once — see
`careeros/pipeline/queryplan.py`. Prints a line per query with its own limit
and count, then the combined total. Set `discovery_mode: single` under
whichever block your active provider reads (`api:` for the default REST
provider, `apify:` for the legacy actor) in `.careeros/config.yaml` to fall
back to one broad query instead. Each tier uses `--limit` by default;
`tier_limits` (same block) can override the limit for a specific tier (e.g.
give a historically high-converting tier more headroom) — check `run.json`'s
cost-per-selected-job over a few days before tuning this, rather than
guessing.

Reports N raw items fetched. If N is 0, check credentials for whichever
provider is active (`config.provider`): the default REST provider needs
`api.transport` set plus `FANTASTIC_API_KEY`/`RAPIDAPI_KEY`; the legacy
actor (`fantastic-jobs-actor`) needs `APIFY_TOKENS`/`APIFY_TOKEN`. Tell the
candidate and stop — nothing downstream can run without jobs. If a query
fails with a clean provider error (e.g. the actor's token budget exhausted),
it's already tried rotating through every configured token — report the
message to the candidate rather than retrying.

## Step 2 — Normalize (deterministic)

```
careeros normalize --date {today}
```

## Step 3 — Dedupe (deterministic)

```
careeros dedupe --date {today}
```

Drops jobs already seen in a prior run or already in the Sheet, plus the same
role posted separately per country (segmented discovery can surface one
posting multiple times — e.g. the same role tagged for Poland/Bulgaria/Spain
— which this collapses to one entry, keeping the highest work-mode-priority
copy). Report the counts (in-run / cross-location / vs-history / vs-sheet) so
the candidate can sanity-check volume.

## Step 4 — Constraints (deterministic hard deal-breakers)

```
careeros constraints --date {today}
```

Applies the two objective, non-negotiable rules — location (onsite/hybrid
outside `profile.yaml`'s `location.onsite_ok` cities) and salary (only when a
number is actually known and confidently below `comp.floor_lpa`) — via
`careeros/pipeline/constraints.py`. Rejected jobs are written to
`04_constraints/rejected.json` with `_reject_reasons` and never reach the AI
gate, so no tokens are spent evaluating a job that can never be applied to.
Report the eligible/rejected split.

## Step 5 — AI Gate (cheap reasoning)

```
careeros gate --prepare --date {today}
```

This prints an instruction block. Follow it exactly: read
`prompts/gate_v1.md` and `.careeros/profile.yaml`, then for each
`05_gate/_input_N.json` batch write the matching `_output_N.json`. Then:

```
careeros gate --finalize --date {today}
```

If finalize reports validation errors, fix only the listed items and
re-run `--finalize` — do not regenerate the whole batch.

## Step 6 — Final Evaluation (the real reasoning step)

```
careeros evaluate --prepare --date {today}
```

This writes cache hits directly and tells you how many jobs actually need
evaluation (often far fewer than the gate's keep count, thanks to caching
across runs). For the jobs that need it: read `prompts/eval_v2.md` and
`.careeros/profile.yaml` (in particular `role_priorities`, `ranking_notes`,
`work_mode_priority`), write one `06_evaluate/<job-id>.json` per job. Every
job reaching this stage already passed the deterministic constraints check,
but still set `recommendation: "skip"` if you independently spot a
deal-breaker the structured fields couldn't catch (e.g. a JD-stated
requirement not present in `Job.salary`/`Job.remote`). Then:

```
careeros evaluate --finalize --date {today}
```

Fix any schema-validation errors for just the listed jobs and re-run
`--finalize`.

## Step 7 — Threshold (deterministic)

```
careeros threshold --date {today}
```

Selects evaluated jobs scoring at or above the configured threshold (default
4.0) AND `recommendation == "apply"` AND still passing the deterministic
constraints re-check (`select_final` in `careeros/pipeline/threshold.py`) —
this is the guaranteed backstop against the AI mislabeling a hard-reject as
"apply." Everything evaluated still gets reported to the candidate; only the
selected subset gets artifacts generated below (this is the cost control —
resume/cover/report generation is the most expensive-per-job step, skip it
for jobs that won't become an application).

## Step 8 — Artifacts, for each selected job

```
careeros artifacts --prepare --date {today}
```

Cache hits (same job content + profile version + eval score + prompt
version as a prior run) are written directly to
`artifacts/<job-id>/{resume,cover}.md` with zero reasoning spent. For
whatever's left, this prints an instruction block naming exactly which
job(s) need a resume and/or cover letter. For each one:

1. **Resume** — read `prompts/resume_v1.md`. Write
   `artifacts/<job-id>/resume.md` following the selector-not-writer rule.
2. **Cover letter** — read `prompts/cover_v1.md`. Write
   `artifacts/<job-id>/cover.md`.

Then:

```
careeros artifacts --finalize --date {today}
```

This runs the voice-dna lint on both files and the deterministic verbatim
truthfulness check (`careeros verify-resume`) on the resume, and only caches
artifacts that pass. If it reports issues, fix only the listed files (using
actual profile text, not a paraphrase) and re-run `--finalize` — do not
regenerate artifacts that already passed.

Finally, for each selected job, render its Level-1 report (deterministic,
zero AI):

```
careeros render-report {job-id} --date {today}
```

## Step 9 — Day summary (deterministic, zero AI)

```
careeros summary --date {today}
```

Renders `.careeros/runs/{today}/summary.md` — funnel counts, the Apply
(≥threshold) list, the Review (near-miss, threshold-0.5 to threshold) list,
and cost-per-selected-job, all computed from `run.json` + the day's Eval
JSONs. This is the P2.6 KPI made visible every run: **maximize interview-
worthy jobs per dollar, never a fixed daily quota** — 0 selected on a given
day is a legitimate, supply-limited outcome, not a failure to report as one.
Runs BEFORE Drive/Sheets below since both may include/link it.

## Step 10 — Drive backup (optional, off by default)

```
careeros drive --date {today}
```

Only runs if `drive.enabled: true` in `.careeros/config.yaml` (otherwise
prints one line and exits — nothing to do). Uploads shortlisted jobs'
resume/cover/report plus `run.json`/`summary.md` to Drive as an ADDITIVE
backup; local Markdown is never moved or replaced. **Any failure here (auth,
network, quota) is caught and reported as a warning — never let a Drive
problem block Sheets or stop the pipeline.** Writes
`.careeros/runs/{today}/drive_links.json` on success, which Sheets (next
step) reads to populate the Drive Folder column.

## Step 11 — Sheets (deterministic)

```
careeros sheets append --date {today}
```

Appends one row per selected job (including a Drive Folder link if Step 10
ran successfully) and records their ids to `.careeros/seen.jsonl` so
tomorrow's `dedupe` skips them automatically.

## Step 12 — Report to the candidate

Read `summary.md` back and relay it, briefly. Point them at the Sheet. Do not
dump full report/resume/cover text into the chat — the artifacts are the
deliverable, `summary.md` is the day-level pointer to them.

## On failure

Every stage's output lives in `.careeros/runs/{today}/`, so `daily` is
resumable: if a stage fails partway, fix the cause and re-run from that
stage, not from the beginning. Never mark a step as done in your summary
without confirming its output file actually exists on disk.
