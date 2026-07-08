<!--
Stage: evaluate. Invoked via `careeros evaluate --prepare` / `--finalize`.
Input:  .careeros/runs/<date>/06_evaluate/_input.json (list of {job, job_hash})
Output: .careeros/runs/<date>/06_evaluate/<job-id>.json, one per input entry,
        matching schemas/eval.schema.json exactly.

This is the ONLY stage that writes fit judgment. Every later artifact
(daily report, resume, cover letter, deep report, application answers)
reads this file's output and must never re-score, re-rank, or override it.

SUPERSEDED by prompts/eval_v2.md (config.prompts.eval defaults to "v2" now).
Kept for cache/version history — a run pinned to `eval: v1` still works.
-->

# Final Evaluation — Career Ops rubric, JSON output only (v1, superseded)

Evaluate each job against the candidate profile using the rubric below.
Write ONE JSON file per job (`<job-id>.json`), matching
`schemas/eval.schema.json` exactly. **No markdown report, no prose outside
the JSON fields.** This is CareerOS's deliberate departure from Career
Ops-style long-form evaluation reports: the JSON itself is the entire
output, and the Level-1 daily report is later rendered from it with zero
additional AI cost.

## Rubric (Career Ops methodology, adapted)

Score each dimension 0-5 (one decimal). `score` is the weighted average,
rounded to 1 decimal.

| Dimension | Weight | What it measures |
|---|---|---|
| `role_fit` | 0.30 | Does the actual day-to-day work match the candidate's real strengths and `targets`? Read the JD body, not just the title — a title match with mismatched substance (e.g. an "AI PM" posting that's actually ML/data-platform engineering) scores low here. |
| `seniority_fit` | 0.20 | Scope/level alignment. Respect `deal_breakers.min_years_ok` — a JD at or below that bar is a straightforward match, not a downlevel to penalize. A JD wanting significantly more years than the candidate has is a real, honestly-scored gap. |
| `skills_match` | 0.25 | Hard requirements the candidate demonstrably has, per `profile.yaml`'s `experience[].bullets` and `skills[]` — never credit a skill that isn't in the profile just because the JD wants it. |
| `domain` | 0.15 | Relevance of the candidate's actual industry/domain background to this JD's domain. |
| `logistics` | 0.10 | Location, comp band, work model, against `profile.yaml`'s `location`/`comp`/`deal_breakers`. An on-site role outside `deal_breakers.onsite_outside` with no visa sponsorship mentioned is a genuine mismatch here, not a soft preference — score it low. |

## Grounding rule (non-negotiable)

Every claim in `strengths`, `weaknesses`, `ats_keywords`, `company_summary`,
and `fit_paragraph` must be traceable to either (a) the Job's own text, or
(b) a fact actually present in `profile.yaml`. Never invent a domain,
metric, employer, or skill the candidate doesn't have, even if the JD's
wording would make a stronger-sounding match. If the JD says "small business
lending" and the profile only supports "consumer/retail lending systems,"
say the latter — do not adopt the JD's specific term as if it were the
candidate's own experience.

## Output shape

For each job, write `<job-id>.json`:

```json
{
  "id": "<job.id>",
  "score": 4.2,
  "confidence": 0.85,
  "recommendation": "apply",
  "strengths": ["...", "...", "..."],
  "weaknesses": ["...", "..."],
  "ats_keywords": ["...", "..."],
  "company_summary": "3 concise lines about the company.",
  "fit_paragraph": "One paragraph, <=80 words, why this is/isn't a fit.",
  "rubric": {
    "role_fit": 4.5, "seniority_fit": 4.0, "skills_match": 4.0,
    "domain": 3.5, "logistics": 5.0
  },
  "prompt_version": "v1",
  "profile_version": <profile.yaml's version field>,
  "job_hash": "<job_hash from the input entry, copied exactly>"
}
```

Field-specific notes:
- `strengths`: **exactly 3**, most important first.
- `weaknesses`: **exactly 2**, most material first.
- `company_summary` and `fit_paragraph` exist so the daily report needs no
  further AI call — write them as the actual Level-1 report content, not as
  throwaway filler. `fit_paragraph` also becomes the cover letter's spine, so
  make it something worth reusing verbatim.
- `recommendation`: `"apply"` if `score >= 4.0` (the default threshold; the
  candidate's actual configured threshold may differ — check
  `.careeros/config.yaml`), else `"skip"`. Confidence reflects your certainty
  in the *evaluation*, not a hedge on the recommendation.

Once every job's file is written, run:

```
careeros evaluate --finalize --date <date>
```
