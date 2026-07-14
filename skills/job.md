# /careeros job {job-id}

Give ONE job the full Apply-tier treatment — resume, cover letter, Level-1
report, application answers, all auto-published — **regardless of its
score**. This is the single command for "I want to apply to this specific
job right now" — a Consider-tier near-miss, a below-`consider_threshold`
job, or an Apply-tier job you don't want to wait for the next `daily` run
to finish. It replicates exactly what `daily` already does automatically
for every score ≥ `threshold` job (see skills/daily.md Steps 8-9, 11-12),
scoped to one job, with no separate manual `publish`/`apply` step needed —
this skill runs everything, including publish, itself.

**Failure Handling Principle** applies throughout, same as every other
skill: [`AGENT_GUIDE.md`](../AGENT_GUIDE.md#the-failure-handling-principle).

## Prerequisite: the job must already be evaluated

This skill does NOT run gate/evaluate itself — it reuses whatever
`daily` already scored. If `{job-id}` has never been discovered and
evaluated, tell the candidate to run `/careeros daily` first, then retry.

## Step 1 — Locate the job's eval and its run date

Find which run date has this job's evaluation on file:

```
grep -rl '"id": "{job-id}"' .careeros/runs/*/06_evaluate/ 2>/dev/null
```

(Or just check the most recent `daily` run's date first — that covers the
common case of "I just ran `daily`, now let me pursue one of today's
results.") If no match anywhere, stop and tell the candidate per the
Prerequisite above. Once found, that path's date segment is `{date}` for
every command below. Read the eval JSON and the matching `Job` record
(`.careeros/runs/{date}/02_normalize/jobs.json`) — you'll want the score,
company, and title to report back at the end.

## Step 2 — Resume + cover letter

```
careeros artifacts --job-id {job-id} --date {date} --prepare
```

Cache-checked, same as `daily`'s batch version — a cache hit needs no
reasoning. If it reports something to generate, follow the printed
AGENT INSTRUCTIONS exactly (same rules as skills/daily.md Step 8: resume
tailoring zones only from `prompts/resume_v2.md`, cover letter freely
written from `prompts/cover_v1.md`, verify each with `careeros
verify-resume` + `careeros lint` before moving on). Then:

```
careeros artifacts --job-id {job-id} --date {date} --finalize
```

Fix any listed schema/lint/truthfulness/page-count issues and re-run
`--finalize` — same discipline as the batch path, just scoped to this one
job. This also renders `resume.pdf`/`cover.pdf` locally via Typst.

## Step 3 — Level-1 report (deterministic, zero AI)

```
careeros render-report {job-id} --date {date}
```

## Step 4 — Application answers

Follow **skills/apply.md Steps 2-6** exactly for this job (report the
detected ATS, get the real questions, reuse `_context.json` if a prior
`careeros prep {job-id}` run left one, generate answers from
`prompts/apply_v1.md`, write `artifacts/{job-id}/answers.md`, lint it) —
but **do NOT run its Step 7** (publish). This skill's own Step 5 below
publishes everything generated so far — resume, cover, report, AND
answers — together, in one pass, instead of twice.

## Step 5 — Auto-publish — ALWAYS run this, not optional

```
careeros publish {job-id} --date {date}
careeros summary --date {date}
```

Run both automatically, without asking — the candidate should never have
to remember a separate command. `publish` uploads whatever artifacts exist
to Drive and patches the Sheet row (only if `drive.enabled`/
`sheets.enabled` are set — in local mode it's a documented no-op, not an
error). `summary` always re-renders the local digest
(`.careeros/results/{date}/summary.md`, also `.careeros/results/latest/`)
so a local-only candidate sees this job's fresh resume/cover links there
regardless of Drive/Sheets — this is what makes local mode a first-class
outcome for this skill, not just the Sheets/Drive path.

## Step 6 — Report back

Tell the candidate: this job's score/company/title, that resume, cover
letter, report, and application answers are all generated, and where to
find them — the Drive/Sheet link if Step 5's `publish` uploaded anything,
otherwise the local path (`.careeros/runs/{date}/artifacts/{job-id}/`) and
`.careeros/results/latest/summary.md`. If any application answer couldn't
be generated from real facts, say which one and why, same as
skills/apply.md Step 8.
