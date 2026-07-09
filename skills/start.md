# /careeros start

Guided onboarding to build `.careeros/profile.yaml` (the candidate's facts —
one of CareerOS's two sources of truth, the other being a job's evaluation)
and to set the candidate's discovery goal, plan, and daily record limit.
Run once at onboarding, and again any time the candidate's facts materially
change.

If `.careeros/profile.yaml` doesn't exist yet, first run `careeros init` to
seed it from `templates/profile.example.yaml`, then walk through this
interview to replace the template's example content with the candidate's
actual facts.

## What you're building

Every field in `schemas/profile.schema.json`, plus a versioned master CV at
`.careeros/cv/master.md`, plus three fields in `.careeros/config.yaml`
(`goals.interviews_per_week`, `api.plan`, `api.limit`). The interview below
maps question groups to schema sections — ask conversationally, don't just
read the schema at the candidate field by field.

## Step 1 — Paste your CV (mandatory first step)

Ask: **"Paste your current CV or resume text — this saves you re-typing
everything by hand. (No CV handy? Type `skip` and we'll build your profile
by answering questions instead.)"**

- Accept plain pasted text (any format — a Word/PDF export's copied text is
  fine). No file upload, no binary parsing — you read whatever the
  candidate pastes, which is exactly what makes this CLI-agnostic.
- If they paste something: save the raw text verbatim to
  `.careeros/cv/master.md` (create the `.careeros/cv/` directory if needed;
  it's already covered by the top-level `.careeros/` gitignore entry — never
  commit it). Then **extract** candidate facts from it into working notes:
  name/email/phone/location/links, headline, past roles (company, title,
  dates, location), achievement bullets, skills, education. Do NOT invent
  anything not actually present in the pasted text.
- If they type `skip`: skip straight to Step 3 (Basics) and build the
  profile the old way, one question at a time. No CV file is created.

**Why this matters, and why it's a hard boundary:** the master CV is a
convenience input for onboarding ONLY — a versioned record you can re-paste
from when facts change, and the raw source Step 2 extracts confirmed
`profile.yaml` bullets from. It is never read directly by any later stage.
Resume generation (`resume_v1.md`) selects verbatim only from the
CONFIRMED `profile.yaml` bullets, never from raw CV text — that's what the
truthfulness rule and `careeros verify-resume` enforce. Evaluation scores
against `profile.yaml` only, too. Once Step 2's facts are confirmed,
`profile.yaml` is the sole source of truth everywhere downstream; the CV
file is historical reference, not a live input.

## Step 2 — Confirm the extracted facts

Show the candidate what you pulled from the CV, organized by profile
section (basics, each past role's bullets, skills, education). Ask them to
confirm, correct, or add anything missing — **never silently guess a
number, collapse two roles into one, or paraphrase a bullet's wording.** If
a CV bullet is vague ("improved conversion"), ask for the real number or
keep the honest qualitative version — don't invent metrics. This step
replaces asking Steps 3-4 below one field at a time; you're verifying and
filling gaps, not re-interviewing from scratch. (If the candidate typed
`skip` in Step 1, there's nothing to confirm — go straight to Step 3.)

## Step 3 — Basics

Full name, email, phone (optional), location, LinkedIn, portfolio/GitHub if
any. → `candidate`. (Skip anything Step 2 already confirmed.)

## Step 4 — Headline and targets

"In one line, how would you describe yourself professionally?" → `headline`.

"What role titles are you actually targeting — not just your dream title,
everything you'd seriously consider?" → `targets` (use short tags:
`product-manager`, `apm`, etc., not full sentences).

## Step 5 — Discovery goal and plan

Ask: **"What's your target — how many interviews per week are you aiming
for?"** → `.careeros/config.yaml`'s `goals.interviews_per_week`. This never
changes scoring; it's context for the discovery quota recommendation below
and, later, for measuring your real application-to-interview conversion
(see the roadmap's P3 outcome-tracking phase).

Ask: **"Which Fantastic Jobs plan are you on — free, RapidAPI, a paid
direct-API plan, or enterprise?"** → `.careeros/config.yaml`'s `api.plan`.
If they don't know or haven't signed up for a paid tier yet, `free` is a
safe default (500 records/week).

Then run `careeros config` and read its printed quota-guard recommendation
block (it looks like: *"Quota guard — plan: free (500 records/week) ...
Recommended: limit N/request..."*). Present it plainly:

```
Based on your goal of {interviews_per_week} interviews/week and the
{plan} plan, we recommend a daily discovery limit of {N} records/query.

Use this recommended limit?  (Y/n)
```

- If yes: leave `api.limit` unset (null) in config.yaml — the guard already
  applies this recommendation as its default whenever the value is null.
- If no: ask **"Enter your preferred daily discovery limit:"**, accept any
  positive integer, and write it to `api.limit` in config.yaml. If the
  number they choose is likely to exceed their weekly quota, say so plainly
  (re-run `careeros config` after writing it — the guard will print a
  warning if so) but **write whatever they chose anyway** — the guard
  recommends and warns, it never overrides the candidate's own choice.

## Step 6 — Deal-breakers and logistics

- Any location a hybrid/on-site role would be a hard no outside of? →
  `deal_breakers.onsite_outside`, `location.onsite_ok`.
- Remote preference: required, preferred, acceptable, not wanted? →
  `location.remote`.
- Visa sponsorship needed for on-site roles outside the home country? →
  `location.visa_sponsorship_required`.
- Comp target range and floor. → `comp`.
- Is there a years-of-experience floor below which a JD is still a fair
  target, not a downlevel? → `deal_breakers.min_years_ok`.

## Step 7 — Experience — the facts graph

This is the section that matters most, because of CareerOS's core rule:
**every resume bullet is a verbatim copy of something written here.** The
model never invents resume prose at generation time — it only selects and
reorders what you capture now. If Step 1/2 already extracted CV bullets,
this step is mostly verifying and tagging them, not writing from scratch.

For each past role:
- Company, title, location, dates.
- Walk through their achievements one at a time. For each: ask for the
  **exact sentence** they'd want on a resume for it (or draft one together
  from the CV wording and get their explicit sign-off — never leave a
  bullet unconfirmed). Push for concrete numbers/scope where honestly
  available; if a number isn't solid, write the honest qualitative version
  instead of guessing.
- Tag each bullet (`tags`) with 3-6 keywords a JD might use to match it
  (domain, skill, function).
- Ask: headline (lead with this), supporting (include if room), or hidden
  (true, but don't surface by default)?

Repeat for `projects` (same bullet/tags/visibility shape).

## Step 8 — Summary variants

Draft 1-2 short professional-summary paragraphs together — a generic
default (`jd_tags: []`) and, if their background spans a distinct secondary
domain (e.g. fintech), one more tagged for that domain. Get explicit
sign-off on the exact wording; this is what `resume_v1.md` copies verbatim.

## Step 9 — Skills and education

Skills: name, category, level (hands-on / ai-assisted / familiar), tags,
visibility — only add a skill the candidate can defend in an interview.
Education: degree, institution, score if they want it shown, dates.

## Step 10 — Confirm and save

1. Set `version: 1` (or increment if this is a re-run on an existing file).
2. Write the completed YAML to `.careeros/profile.yaml`.
3. Validate it against `schemas/profile.schema.json` before finishing —
   report any schema errors and fix them with the candidate, don't save an
   invalid profile.
4. If Step 5 wrote new values, confirm `.careeros/config.yaml`'s
   `goals.interviews_per_week`, `api.plan`, and (if set) `api.limit` are
   saved.
5. Tell the candidate: profile saved, they can review/edit it directly any
   time, and re-running `careeros start` later is how they update it as
   facts change (which will also bump `version` and invalidate stale
   cached evaluations/resumes/covers for jobs affected by the change). If
   they pasted a CV, mention it's saved at `.careeros/cv/master.md` and
   that re-running `start` with an updated CV will refresh it.
