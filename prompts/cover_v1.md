<!--
Stage: cover (part of skills/daily.md's artifact-generation step).
Cache key: sha1(job_hash + profile.version + eval.score + prompt_version).
Input: profile.yaml, the Job, and its 05_evaluate/<id>.json.
Output: artifacts/<job-id>/cover.md
-->

# Cover Letter — grounded, not invented

Import `prompts/voice-dna.md` for tone.

## Ground every claim

Every claim in the letter must trace to `profile.yaml` or to
`05_evaluate/<job-id>.json`. The **same truthfulness rule** as the resume
applies: reframe the JD's themes with the candidate's own adjacent wording,
never adopt the JD's specific term as the candidate's own unless it's
actually backed by a profile fact.

## Reuse, don't recompute

The eval's `fit_paragraph` is the spine of this letter — it already answers
"why does this candidate fit," reasoned once, at evaluation time. Build the
letter around it rather than re-deriving fit from scratch. Pull supporting
detail from `profile.yaml`'s `experience[].bullets` and `company_summary`
(also already on the eval) for the "why this company" paragraph.

## Shape

- **Opening**: the role, the company, one sentence on why this specific
  company (drawn from `company_summary`), not a generic "I am excited to
  apply."
- **Middle** (1-2 short paragraphs): the fit, built from `fit_paragraph` +
  1-2 supporting profile facts. Specific, not a restated resume.
- **Close**: brief, confident, a clear next step. No "I look forward to
  hearing from you" filler — say something with more texture, or nothing.
- **Length**: <=250 words total. Shorter and sharp beats long and generic.

## Before finishing (mandatory)

1. Reread: does every specific claim (domain, metric, employer, skill)
   actually appear in `profile.yaml`? Fix any that don't.
2. Run `careeros lint artifacts/<job-id>/cover.md` and resolve every issue.
3. Critical Review Gate: read it once as the hiring manager receiving it.
   Does it sound like a template with names swapped in, or like someone who
   actually read this JD? If the former, cut the generic parts.
