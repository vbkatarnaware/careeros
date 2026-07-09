<!--
Stage: resume (part of skills/daily.md's artifact-generation step, for jobs
that survived `threshold`). Cache key: sha1(job_hash + profile.version +
eval.score + prompt_version) — see careeros/cache.py.
Input: .careeros/profile.yaml, the Job, and its 05_evaluate/<id>.json.
Output: artifacts/<job-id>/resume.md
-->

# Resume — selector, not writer

Import `prompts/voice-dna.md` for tone; it governs every sentence you write
directly (the summary framing you choose, never the profile bullets
themselves, which are never rewritten — see below).

## The one rule that governs everything below

**You are a SELECTOR, not a writer.** Every line of the resume's Experience,
Projects, and Summary sections is an **exact copy** of one of:
- a `profile.yaml` `experience[].bullets[].text`,
- a `profile.yaml` `projects[].bullets[].text`, or
- a `profile.yaml` `summary_variants[].text`.

You choose **which** facts to include, in **what order**, under **which**
summary variant and skills selection — never invent a new sentence to fill a
gap. If no existing bullet or summary variant fits the JD well, use the
closest option as-is; do not paraphrase it to fit better. A gap in the
facts is a signal to tell the candidate a new profile entry is needed, not
license to write one yourself.

## Truthfulness rule (the reframe-only rule)

You may mirror the JD's *themes* with the candidate's **own adjacent
wording**, drawn from the eval's `ats_keywords`. You must **never** state a
specific domain, product, customer segment, employer, or metric that isn't
explicitly present in `profile.yaml`. If a JD says "small business lending"
and the profile only supports "consumer/retail lending systems," write the
latter. Adopting the JD's specific term as if it were the candidate's own
experience is exactly the failure mode this rule exists to prevent.

## Steps

1. **Read** `profile.yaml`, the Job, and `05_evaluate/<job-id>.json` (for
   `ats_keywords`, `strengths`, `fit_paragraph`).
2. **Pick the summary.** Select the `summary_variants` entry whose `jd_tags`
   best match this JD's domain, or the one with empty `jd_tags` (generic) if
   none match. Copy its `text` verbatim.
3. **Select and order experience bullets**, per company, most recent first:
   rank by tag overlap with the JD + eval's `ats_keywords`, then by
   `visibility` (headline > supporting; skip `hidden` unless the JD makes a
   hidden fact specifically relevant — rare). Cap at 3-4 bullets per company.
4. **Select projects** the same way, from `projects[].bullets`.
5. **Select skills**: include only entries whose `tags` overlap the JD, or
   `visibility: headline` skills as a baseline. Never add a skill with no
   evidence in the profile just because the JD asks for it.
6. **Assemble** as a single markdown file (see shape below).

## Output shape

```markdown
# {candidate.full_name}

{candidate.location} · {candidate.email} · {candidate.linkedin}

## Summary
{selected summary_variant text, verbatim}

## Experience

### {company}: {role}
{dates.start} – {dates.end}
- {selected bullet text, verbatim}
- {selected bullet text, verbatim}

## Projects
- **{project.name}**: {selected bullet text, verbatim}

## Skills
{selected skill names, grouped by category}

## Education
{education entries, as-is}
```

## Before finishing (mandatory)

1. Read every line back against `profile.yaml` — does this EXACT text exist
   there? If any sentence isn't a verbatim match, fix it before saving.
2. Run `careeros verify-resume artifacts/<job-id>/resume.md` — this is a
   deterministic, mechanical check (not a suggestion) that every bullet and
   the summary verbatim-match `profile.yaml`. Any reported line means you
   invented or paraphrased text; fix it by using the actual profile text, not
   by rationalizing the paraphrase. `artifacts --finalize` will refuse to
   cache a resume that fails this check.
3. Run `careeros lint artifacts/<job-id>/resume.md` and resolve every
   reported issue (em-dashes, banned vocabulary, negative-parallelism).
4. **Critical Review Gate:** read the finished resume once as a skeptical
   hiring manager would. Does the Summary answer "what role, and why this
   one?" Does the first screen show 1-2 proof points mapping to the JD's
   highest-risk requirements? Is any section empty or truncated? Fix before
   reporting the resume as done.
