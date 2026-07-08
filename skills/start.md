# /careeros start

Guided interview to build `.careeros/profile.yaml` — the candidate's facts,
one of CareerOS's two sources of truth (the other is a job's evaluation).
Run once at onboarding, and again any time the candidate's facts materially
change.

If `.careeros/profile.yaml` doesn't exist yet, first run `careeros init` to
seed it from `templates/profile.example.yaml`, then walk through this
interview to replace the template's example content with the candidate's
actual facts.

## What you're building

Every field in `schemas/profile.schema.json`. The interview below maps
question groups to schema sections — ask conversationally, don't just read
the schema at the candidate field by field.

## 1. Basics

Full name, email, phone (optional), location, LinkedIn, portfolio/GitHub if
any. → `candidate`.

## 2. Headline and targets

"In one line, how would you describe yourself professionally?" → `headline`.

"What role titles are you actually targeting — not just your dream title,
everything you'd seriously consider?" → `targets` (use short tags:
`product-manager`, `apm`, etc., not full sentences).

## 3. Deal-breakers and logistics

- Any location a hybrid/on-site role would be a hard no outside of? →
  `deal_breakers.onsite_outside`, `location.onsite_ok`.
- Remote preference: required, preferred, acceptable, not wanted? →
  `location.remote`.
- Visa sponsorship needed for on-site roles outside the home country? →
  `location.visa_sponsorship_required`.
- Comp target range and floor. → `comp`.
- Is there a years-of-experience floor below which a JD is still a fair
  target, not a downlevel? → `deal_breakers.min_years_ok`.

## 4. Experience — the facts graph

This is the section that matters most, because of CareerOS's core rule:
**every resume bullet is a verbatim copy of something written here.** The
model never invents resume prose at generation time — it only selects and
reorders what you capture now. Spend the real time here.

For each past role:
- Company, title, location, dates.
- Walk through their achievements one at a time. For each: ask for the
  **exact sentence** they'd want on a resume for it (or draft one together
  and get their explicit sign-off — never leave a bullet unconfirmed).
  Push for concrete numbers/scope where honestly available; if a number
  isn't solid, write the honest qualitative version instead of guessing.
- Tag each bullet (`tags`) with 3-6 keywords a JD might use to match it
  (domain, skill, function).
- Ask: headline (lead with this), supporting (include if room), or hidden
  (true, but don't surface by default)?

Repeat for `projects` (same bullet/tags/visibility shape).

## 5. Summary variants

Draft 1-2 short professional-summary paragraphs together — a generic
default (`jd_tags: []`) and, if their background spans a distinct secondary
domain (e.g. fintech), one more tagged for that domain. Get explicit
sign-off on the exact wording; this is what `resume_v1.md` copies verbatim.

## 6. Skills and education

Skills: name, category, level (hands-on / ai-assisted / familiar), tags,
visibility — only add a skill the candidate can defend in an interview.
Education: degree, institution, score if they want it shown, dates.

## 7. Confirm and save

1. Set `version: 1` (or increment if this is a re-run on an existing file).
2. Write the completed YAML to `.careeros/profile.yaml`.
3. Validate it against `schemas/profile.schema.json` before finishing —
   report any schema errors and fix them with the candidate, don't save an
   invalid profile.
4. Tell the candidate: profile saved, they can review/edit it directly any
   time, and re-running `careeros start` later is how they update it as
   facts change (which will also bump `version` and invalidate stale
   cached evaluations/resumes/covers for jobs affected by the change).
