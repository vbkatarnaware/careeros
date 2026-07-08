<!--
Stage: apply (careeros apply <job-id>). Manual, on-demand only — never runs
during `daily`, and only once the candidate has the real application
questions in hand (they don't exist before that point). See skills/apply.md.
Input: profile.yaml, the Job, its eval, its cached deep-context (from `prep`,
if it exists), and the pasted application questions.
Output: artifacts/<job-id>/answers.md
-->

# Application Answers — grounded, never fabricated

## Reuse before researching

If `artifacts/<job-id>/_context.json` exists (written by a prior
`careeros prep <job-id>` run), read it and reuse its company research and
interview-focus reasoning directly — do not re-research the company or
re-evaluate the job. If it doesn't exist, build only the minimal research
slice this specific set of questions actually needs; don't run the full
Level-2 deep report just to answer a couple of application questions.

## The grounding rule (same as resume/cover)

Every answer must be built from:
- `profile.yaml` facts (verbatim bullets, reframed only, never rewritten
  into an unbacked claim),
- `05_evaluate/<job-id>.json` (strengths, fit_paragraph, company_summary),
- the cached deep-context, if present.

**Never fabricate experience.** If a question asks about something the
candidate's profile has no basis for, say so plainly in your response to the
candidate rather than inventing an answer — the candidate decides how to
handle a genuine gap, not the model.

## Steps

1. Detect the ATS from `Job.ats` (already set by `normalize` — no
   re-detection needed).
2. Take the application questions the candidate pasted (this stage cannot
   run without them — there is no automated way to fetch a specific
   application form's questions in v1).
3. For each question, draft an answer grounded per the rule above. Keep
   answers as tight as the question calls for — a one-line "why this
   company" field gets one line, not a paragraph.
4. Apply `prompts/voice-dna.md` tone throughout.

## Output

`artifacts/<job-id>/answers.md`:

```markdown
# Application Answers — {job.title} at {job.company}

## {question 1 text}
{answer}

## {question 2 text}
{answer}
```

## Before finishing

Run `careeros lint artifacts/<job-id>/answers.md`. Then reread each answer
once against `profile.yaml` — is there a single claim here that isn't
actually backed by a profile fact? If so, fix it before showing the
candidate.
