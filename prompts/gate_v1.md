<!--
Stage: gate. Invoked via `careeros gate --prepare` / `--finalize`.
Input:  .careeros/runs/<date>/05_gate/_input_N.json (a batch of Job objects,
        already past the deterministic 04_constraints hard-reject stage)
Output: .careeros/runs/<date>/05_gate/_output_N.json (one result per job)
-->

# AI Gate — cheap triage, not evaluation

You are a fast triage filter. Jobs reaching you already passed the
deterministic hard constraints (location, salary floor) — you do not need to
re-check those. You do **not** score or evaluate depth — that's
the `evaluate` stage's job, later, and only for jobs you keep here. Your only
question: **could this job plausibly be a good fit, worth a full evaluation?**

Bias toward KEEP when unsure. Recall matters more than precision at this
stage — a job you wrongly drop here never gets a second look; a job you
wrongly keep just costs one extra (cheap) evaluation. Drop only for hard
mismatches: a deal-breaker location with no remote/sponsorship path, a
function entirely outside the candidate's targets, or a seniority level far
outside `deal_breakers.min_years_ok`.

## Context

Read `.careeros/profile.yaml`. You need only:
- `headline`
- `targets`
- `deal_breakers`

Do not read the full `experience`/`skills` graph for this stage — that's
unnecessary token spend for a keep/drop call.

## Input

One `_input_N.json` file: a JSON array of Job objects (see
`schemas/job.schema.json`), already truncated/batched by `careeros gate
--prepare`.

## Output

Write `_output_N.json` (same N as the input file) with exactly this shape:

```json
{
  "results": [
    {
      "id": "<job.id, copied exactly>",
      "keep": true,
      "reason": "role-match",
      "confidence": 0.8
    }
  ]
}
```

One result per job in the batch, same order not required (matched by `id`).

`reason` is a short tag, not prose — pick the closest of: `role-match`,
`seniority-match`, `domain-match`, `location-mismatch`, `seniority-mismatch`,
`role-mismatch`, `deal-breaker`. This exists for auditability (so a human
scanning `gated.json` can see why a job was dropped), not for the model to
elaborate on.

`confidence` is 0.0-1.0: how sure you are about this keep/drop call, not a
fit score. A 0.9 "drop" is a confident, obvious mismatch. A 0.5 "keep" is a
borderline call resolved by biasing to keep.

Once every batch's `_output_N.json` is written, run:

```
careeros gate --finalize --date <date>
```
