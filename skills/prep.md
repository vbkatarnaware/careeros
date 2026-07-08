# /careeros prep {job-id}

Generates the Level-2 deep interview-prep report for one job. Manual,
on-demand only — this never runs during `daily`, because most discovered
jobs never become an actual application, and this report is the expensive
one.

## 1. Locate the job and its evaluation

Find `{job-id}` in the most recent run's `05_evaluate/{job-id}.json` (search
`.careeros/runs/*/05_evaluate/` if the date isn't given — the id is stable
across runs). If no evaluation exists for this id, tell the candidate this
job hasn't been evaluated yet and stop; this stage expands an evaluation, it
does not create one.

## 2. Check for an already-generated report

If `artifacts/{job-id}/deep_report.md` already exists, tell the candidate
it exists at that path and ask before regenerating — don't silently redo
research that's already been done.

## 3. Generate

Read `prompts/deep_report_v1.md` and follow it exactly. Key constraint
worth restating here because it's easy to violate by habit: **inherit the
eval's score/recommendation/strengths/weaknesses verbatim — do not
re-evaluate the job.** The new work in this stage is external research
(company, product, competitors, interview focus) layered on top, not a
second opinion on fit.

Write both output files:
- `artifacts/{job-id}/deep_report.md`
- `artifacts/{job-id}/_context.json` (the cached bundle `careeros apply`
  will reuse later — this is what makes `prep` and `apply` share context
  without duplicating research)

## 4. Lint and review

```
careeros lint artifacts/{job-id}/deep_report.md
```

Fix any reported issues. Long-form is allowed here, but voice-dna still
applies throughout.

## 5. Report back

Tell the candidate the report's path and a 2-3 sentence summary of what's
new in it (don't restate the score/recommendation, they already have that
from the daily report) — the report itself is the deliverable, not the chat
message.
