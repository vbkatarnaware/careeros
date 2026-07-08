<!--
Stage: evaluate. Invoked via `careeros evaluate --prepare` / `--finalize`.
Input:  .careeros/runs/<date>/06_evaluate/_input.json (list of {job, job_hash})
Output: .careeros/runs/<date>/06_evaluate/<job-id>.json, one per input entry,
        matching schemas/eval.schema.json exactly.

This is the ONLY stage that writes fit judgment. Every later artifact
(daily report, resume, cover letter, deep report, application answers)
reads this file's output and must never re-score, re-rank, or override it.

v2 changes from v1: adds profile-driven role-priority/work-mode ranking
guidance (as DATA, not hardcoded role names — this prompt is role-agnostic
by construction) and a mandatory deal-breaker override rule. No new rubric
dimensions; weights unchanged from v1.
-->

# Final Evaluation — Career Ops rubric, JSON output only

Evaluate each job against the candidate profile using the rubric below.
Write ONE JSON file per job (`<job-id>.json`), matching
`schemas/eval.schema.json` exactly. **No markdown report, no prose outside
the JSON fields.** This is CareerOS's deliberate departure from Career
Ops-style long-form evaluation reports: the JSON itself is the entire
output, and the Level-1 daily report is later rendered from it with zero
additional AI cost.

## Jobs reaching you already passed hard constraints

The deterministic `constraints` stage (see `careeros/pipeline/constraints.py`)
already removed jobs with an objectively known deal-breaker (onsite outside
the profile's accepted cities, or a confidently-known salary below
`comp.floor_lpa`) before this stage ever ran. You do not need to re-derive
those two checks. **But this prompt still must NOT recommend "apply" on a
deal-breaker it can see in the JD text that the deterministic check
couldn't** (e.g. the JD states a visa requirement the candidate can't meet,
or states a salary band below floor that wasn't in the structured salary
field). If you find one: set `recommendation: "skip"` regardless of the
weighted score, and say why in `fit_paragraph`. This is the fix for a real
bug found in QA, where a hard constraint got diluted into a passing
weighted score — never let that happen again, at either layer.

## Rubric (Career Ops methodology, adapted)

Score each dimension 0-5 (one decimal). `score` is the weighted average,
rounded to 1 decimal.

| Dimension | Weight | What it measures |
|---|---|---|
| `role_fit` | 0.30 | Does the actual day-to-day work match the candidate's real strengths and `targets`? Read the JD body, not just the title. Use `profile.yaml`'s `role_priorities` (an ordered list, tier 1 = highest) and `ranking_notes` (free text) to weigh this — these are profile DATA, not hardcoded logic, so apply whatever this specific candidate's priorities and notes say, even if that means a lower-tier role should still score well when the notes say so (e.g. an exceptional company/opportunity). A title match with mismatched substance (e.g. a role titled to match a top priority but whose actual JD body describes a completely different function) scores low here — read the substance, not the label. |
| `seniority_fit` | 0.20 | A growth-fit measure, not a rigid level-match. Respect `deal_breakers.min_years_ok` — a JD at or below that bar is a straightforward match, not a downlevel to penalize. Follow `ranking_notes` for how to treat a step-down or step-up in level (e.g. many profiles want NO artificial boost for a junior-titled role just because it's junior — score a step-down as moderate unless the opportunity is genuinely exceptional, and an over-senior JD as a real, honestly-scored gap). |
| `skills_match` | 0.25 | Hard requirements the candidate demonstrably has, per `profile.yaml`'s `experience[].bullets` and `skills[]` — never credit a skill that isn't in the profile just because the JD wants it. If `ranking_notes` calls out a specific background (e.g. AI/LLM/automation experience) that should boost fit when genuinely relevant to this JD, apply that boost only when the JD's actual substance calls for it, not just a keyword match (e.g. an "AI"-titled role that's actually a different discipline entirely does not earn the boost). |
| `domain` | 0.15 | Relevance of the candidate's actual industry/domain background to this JD's domain. |
| `logistics` | 0.10 | Since the hard location/salary constraints already passed, this dimension is a RANKING signal among constraint-passing jobs: use `profile.yaml`'s `work_mode_priority` (ordered, tier 1 = highest) to rank remote-vs-onsite variants, and `comp.preferred_lpa` as a positive signal when comp is known and at/above it. This is not a pass/fail check anymore — score honestly on preference-fit. |

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
  "prompt_version": "v2",
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
  `.careeros/config.yaml`) AND no deal-breaker was found per the rule above;
  else `"skip"`. Confidence reflects your certainty in the *evaluation*, not
  a hedge on the recommendation.

Once every job's file is written, run:

```
careeros evaluate --finalize --date <date>
```
