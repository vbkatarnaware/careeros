<!--
Stage: apply. Two entry points share this prompt (P2.10):

1. AUTOMATIC BATCH (`careeros apply --prepare/--finalize`) — runs as part of
   `daily` for every Apply-tier (score >= threshold) job. `_apply_prepare`
   (cli.py) already fetched the application form's page text in the
   background via careeros/apply/browser.py and wrote it as `form_text` in
   `artifacts/<job-id>/_apply_input.json` — read THAT file's `form_text` to
   find the real questions, no candidate paste needed.
2. ON-DEMAND (`careeros apply <job-id>`, host-CLI skill, see skills/apply.md)
   — manual, any score, for a job whose form wasn't automatically readable
   or that scored below threshold. The candidate pastes the real questions
   themselves (optionally after the host agent reads them directly from the
   candidate's own logged-in browser — see skills/apply.md step 3).

Either way: Input = profile.yaml, the Job, its eval, its cached deep-context
(from `prep`, if it exists), and the real application questions (from
`form_text` or pasted). Output: artifacts/<job-id>/answers.md
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
candidate's profile has no basis for, say so plainly rather than inventing
an answer — the candidate (batch: left as a gap in the output; on-demand:
told directly in chat) decides how to handle a genuine gap, not the model.

## Personal / logistics questions — ask once, save, reuse forever

A specific category of question isn't about experience at all: notice
period, work authorization/visa status, expected salary, earliest start
date, employment type (full-time/contract), and similar. These are static
FACTS ABOUT THE CANDIDATE, not per-job content — the same answer is correct
on every application, so once you have it, it belongs in `profile.yaml`,
not just in one job's `answers.md`.

**Check profile.yaml first, in this order, before treating any of these as
a gap:**
- Salary/comp expectations → `comp.target_lpa` / `comp.preferred_lpa` /
  `comp.currency`.
- Work authorization / visa status → `profile.logistics.work_authorization`.
- Notice period → `profile.logistics.notice_period`.
- Earliest start date → `profile.logistics.earliest_start_date`.
- Employment type → `profile.logistics.employment_type`.
- Anything else recurring and candidate-level (e.g. "willing to relocate?")
  → `profile.logistics.other[]`, matched by the closest `question` label.

**If the fact IS present**, use it directly — it's already verbatim,
ready-to-use phrasing, same rule as every other profile fact.

**If the fact is NOT present**, this is NOT a fabrication gap — it's a
one-time question worth asking, because the answer will be reused on every
future application:
- **On-demand** (`careeros apply <job-id>`, candidate present in chat): ask
  the candidate directly, right there, before finishing this job. Once
  answered, write it into `.careeros/profile.yaml` under the matching
  `logistics.*` field (or append `{question, answer}` to `logistics.other`
  for anything not in the named list), then use it in this job's answer.
  Do NOT bump `profile.version` for this — `logistics` isn't read by
  gate/evaluate/resume/cover, so it doesn't affect anything version bumps
  exist to invalidate.
- **Batch** (`--prepare`/`--finalize` inside `daily`): do NOT ask
  per-job — the same missing fact will otherwise get asked once per job in
  the batch. Instead, draft the REST of that job's answers normally, and
  leave that one specific line as `[NEEDS: <short label>]` (e.g.
  `[NEEDS: notice_period]`). After every job in this batch pass has been
  drafted, collect the distinct `[NEEDS: ...]` labels actually used (there
  is usually only one or two, since the same facts repeat across jobs), ask
  the candidate about each ONCE, write the answers into
  `.careeros/profile.yaml`'s `logistics.*`, then go back and replace every
  `[NEEDS: ...]` placeholder across all of this batch's `answers.md` files
  with the real answer before running `--finalize`. No `answers.md` should
  ever reach `--finalize` still containing a `[NEEDS: ...]` placeholder.

**Never** apply this ask-and-save flow to voluntary EEO/demographic
self-identification questions (race, gender, veteran or disability status,
and similar). Those are legally sensitive and voluntary by design — leave
them as a genuine gap for the candidate to fill in on the real form
themselves, exactly like an unbacked experience claim.

## Steps

1. Detect the ATS from `Job.ats` (already set by `normalize` — no
   re-detection needed).
2. Get the real application questions — from `_apply_input.json`'s
   `form_text` (batch) or pasted by the candidate (on-demand). **Batch only:**
   `form_text` only reaches you when the background fetch already
   determined it's a genuinely real, readable form (not a login wall, not a
   closed posting, not a JS shell it couldn't render) — so if it still
   doesn't contain any identifiable free-text essay questions, that means
   this particular form just doesn't have any. Do not invent questions from
   it — skip writing `answers.md` for this job entirely and move to the
   next one; the `--finalize` step marks that job `no_essay_questions`
   automatically, and the candidate can run `careeros apply <job-id>`
   themselves to double-check by hand if they want.
3. For each question, first check whether it's a personal/logistics
   question (see above) — handle those per that section. For everything
   else, draft an answer grounded per the grounding rule. Keep answers as
   tight as the question calls for — a one-line "why this company" field
   gets one line, not a paragraph.
4. Apply `prompts/voice-dna.md` tone throughout.

## Output

`artifacts/<job-id>/answers.md`:

```markdown
# Application Answers: {job.title} at {job.company}

## {question 1 text}
{answer}

## {question 2 text}
{answer}
```

## Before finishing

Run `careeros lint artifacts/<job-id>/answers.md`. Then reread each answer
once against `profile.yaml` — is there a single claim here that isn't
actually backed by a profile fact? If so, fix it before showing the
candidate. **Batch only:** confirm no `answers.md` in this pass still
contains a `[NEEDS: ...]` placeholder (see "Personal / logistics
questions" above) before running `--finalize` — resolve every one of them
first.
