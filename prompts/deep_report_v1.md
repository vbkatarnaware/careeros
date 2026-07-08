<!--
Stage: prep (careeros prep <job-id>). Manual, on-demand only — never run
during `daily`. See skills/prep.md for the full command flow, including how
this stage's output is cached and reused by skills/apply.md.
Input: profile.yaml, the Job, its 05_evaluate/<id>.json, + external research.
Output: artifacts/<job-id>/deep_report.md AND artifacts/<job-id>/_context.json
(the cached "deep-context bundle" that `apply` reuses).
-->

# Level-2 Deep Report — interview preparation, not re-evaluation

This is CareerOS's one deliberately long-form artifact, and it's the only
one, because it's opt-in: the candidate explicitly asked for it by running
`careeros prep <job-id>`.

## The governing rule for this stage

**Expand the evaluation's reasoning. Never recompute it.** Read
`05_evaluate/<job-id>.json` and treat its `score`, `confidence`,
`recommendation`, `strengths`, `weaknesses`, and `ats_keywords` as fixed —
inherit them verbatim into this report's relevant sections. Do not
re-derive a different score or re-argue the recommendation; that question
was already answered once, cheaply, and re-litigating it here would be
exactly the wasted, duplicated AI spend this architecture exists to avoid.

What this stage legitimately ADDS is external information the eval never
had reason to gather: company/product/competitor research, recent news, and
interview-specific reasoning (likely focus areas, questions to ask, red
flags). Keep this addition clearly separate from the inherited fit
reasoning — a reader should be able to tell "this came from the eval" from
"this is new research for interview prep."

## Sections to produce

1. **Executive summary** — 3-4 sentences, lead with the inherited score/
   recommendation, then what's new in this report.
2. **Complete fit analysis** — expand the eval's `rubric` breakdown with
   more reasoning per dimension; still grounded in `profile.yaml`, no new
   fabricated claims.
3. **Missing skills and positioning** — from `weaknesses`, concrete
   suggestions for how to address a gap in conversation (not a claim to
   invent the skill).
4. **Resume/cover letter rationale** — briefly explain the choices made in
   `artifacts/<job-id>/resume.md` and `cover.md` (which bullets, why).
5. **Company research** — product, business model, competitors, recent news
   if findable. Label this section clearly as external research, distinct
   from the profile/eval-grounded sections above.
6. **Likely interview focus + behavioral questions** — with STAR story
   pointers mined from `profile.yaml`'s `experience[].bullets` (cite which
   bullet each story point comes from).
7. **Technical/product concepts to revise**, if relevant to the role.
8. **Salary observations**, if inferable from the Job's `salary` field or
   public data — never invent a number.
9. **Questions the candidate should ask** the interviewer.
10. **Risks and red flags.**
11. **30-60-90 day expectations**, if inferable from the JD.

## Output

Two files:
- `artifacts/<job-id>/deep_report.md` — the full report above.
- `artifacts/<job-id>/_context.json` — a compact bundle of {job, eval,
  company research, interview focus areas} that `skills/apply.md` reads
  directly instead of rebuilding this research from scratch. This is the
  "shared cached deep-context" the apply flow depends on.

## Before finishing

Run `careeros lint artifacts/<job-id>/deep_report.md`. Long-form is allowed
here, but voice-dna still applies — no em-dashes, no AI-tell phrasing, even
in an 1800-word document.
