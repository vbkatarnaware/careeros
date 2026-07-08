# /careeros apply {job-id}

Detects the ATS and drafts answers to the actual application questions.
Manual, on-demand only, and only useful once the candidate has opened the
real application and can paste its questions — CareerOS has no way to fetch
those automatically in v1 (see README's "Deferred" section).

## 1. Locate the job

Find `{job-id}`'s Job record (any run's `02_normalize/jobs.json`) and its
evaluation (`05_evaluate/{job-id}.json`). If either is missing, tell the
candidate and stop.

## 2. Report the detected ATS

`Job.ats` was already set at normalize time (`greenhouse` / `lever` /
`ashby` / `workday` / `custom`) — no re-detection needed. Tell the
candidate which ATS this is, mainly so they can sanity-check it matches
what they're actually looking at.

## 3. Get the questions

Ask the candidate to paste the actual application form's questions. This
step cannot proceed without them.

## 4. Reuse cached context if it exists

Check for `artifacts/{job-id}/_context.json` (written by a prior
`careeros prep {job-id}` run). If present, read it and reuse its research —
don't re-run company research from scratch. If absent, that's fine; most
application question sets don't need the full deep-report research anyway.

## 5. Generate answers

Read `prompts/apply_v1.md` and follow it exactly — every answer must trace
to `profile.yaml`, the eval, or the cached context. Never fabricate
experience to fit a question; if the profile has no basis for something a
question asks, say so to the candidate rather than inventing an answer.

Write `artifacts/{job-id}/answers.md`.

## 6. Lint and review

```
careeros lint artifacts/{job-id}/answers.md
```

Reread each answer once against `profile.yaml` before showing it to the
candidate — every specific claim should be traceable to an actual fact.

## 7. Report back

Show the candidate the answers file path. If any question couldn't be
answered from real facts, say which one and why, so they can fill it in
themselves rather than being handed a fabricated answer silently.
