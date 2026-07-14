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
Resume generation (`resume_v2.md`) selects and reworks language only from
the CONFIRMED `profile.yaml` bullets, never from raw CV text or invented
facts — that's what the fact-preservation rule and `careeros verify-resume`
enforce. Evaluation scores
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

Ask: **"Which Fantastic Jobs plan are you on?"** and offer exactly these
choices (P2.9.1):
1. **Free** (500 records/week) — the default for most new users.
2. **Paid** (RapidAPI, a paid direct-API plan, or enterprise) — ask which,
   and whether they know their own weekly record allotment.
3. **Custom quota** — they type their own weekly record number directly
   (writes `api.weekly_record_quota`, not `api.plan`).

**Always write their choice to config.yaml explicitly** — even if they pick
Free, write `api.plan: free` rather than leaving it unset. This matters:
CareerOS now assumes Free by default whenever `api.plan` is unset (so a
skipped question never silently over-fetches), but an explicit choice here
means the candidate's config states their real plan instead of relying on
that default, and the assumed-plan disclosure line never has to show up on
their `discover` runs.

Then run `careeros config` and read its printed quota-guard recommendation
block (it looks like: *"Quota guard — plan: free (500 records/week) ...
Recommended: limit N/request..."*). Under the hood its arithmetic is: plan's
weekly record quota ÷ active discovery days ÷ number of query tiers this
candidate's own profile generates (one tier per `work_mode_priority` entry
— see `pipeline/queryplan.py`; **this count is never hardcoded, it's
whatever that candidate's own `work_mode_priority` list produces** — a
remote-only candidate with one entry gets 1 search, a candidate with four
tiers gets 4, and so on).

**Present this to the candidate in plain English, not as raw arithmetic.**
Turn each `_work_mode` value into a short human label — `global_remote` →
"Remote", `{place}_remote` → the place name (title-cased, e.g. `india_remote`
→ "India"), `onsite` → "Onsite" — and list only the tiers this candidate
actually has:

```
Based on your search preferences ({tier labels, comma-separated}), CareerOS
will run {N} discovery search{"es" if N != 1 else ""} every day. On the
{plan} plan ({quota} records/week), the recommended limit is {per-search}
records per search (≈{daily} records/day).

Recommended limit: {per-search}   Accept? (Y/n)
Or enter your own value: _
```

For example, a candidate with `work_mode_priority: [global_remote,
india_remote, mumbai_onsite]` (3 tiers) on the Free plan sees: *"Based on
your search preferences (Remote, India, Onsite), CareerOS will run 3
discovery searches every day. On the Free plan (500 records/week), the
recommended limit is 23 records per search (≈69 records/day)."* A candidate
with only `work_mode_priority: [global_remote]` (1 tier) sees: *"CareerOS
will run 1 discovery search every day. Recommended limit: 71 records per
search."* Never hardcode the tier count or labels — always derive them from
this candidate's own configured tiers.

- If yes: leave `api.limit` unset (null) in config.yaml — `discover` computes
  and applies this exact recommendation as its default whenever the value is
  null and the weekly quota is known (P2.9; see `careeros/budget.py
  recommend()` / `cli.py discover`'s base-limit resolution).
- If no: ask **"Enter your preferred daily discovery limit:"**, accept any
  positive integer, and write it to `api.limit` in config.yaml. If the
  number they choose is likely to exceed their weekly quota, say so plainly
  (re-run `careeros config` after writing it — the guard will print a
  warning if so) but **write whatever they chose anyway** — the guard
  recommends and warns, it never overrides the candidate's own choice.
- Either way, mention this can be changed later by editing `api.limit` in
  `.careeros/config.yaml` directly (see the README's Configuration section),
  or via `careeros doctor`, which shows the current vs. recommended limit on
  every run.

### Optional paid job sources (v1.2)

Three sources are already on by default and need nothing from the
candidate: Fantastic Jobs (the primary source, configured above),
RemoteOK, and We Work Remotely. This step is about additional, paid job
sources the candidate can opt into — reason about these as named job
boards, not as any particular fetch mechanism; that's an implementation
detail that only comes up later, if at all (see the credential note
below).

Ask: **"Want to enable any paid job sources beyond the free ones? A few
options, each a few tenths of a cent per job:**
- **Naukri** — India-focused, low overlap with your main source, every job
  in testing was genuinely relevant.
- **Glassdoor** — general job board, relevant results, but runs slower
  (up to a few minutes).
- **ZipRecruiter** — the most relevant results of the paid options in
  testing, but roughly 1 in 3 runs fails outright and returns nothing
  (CareerOS handles that gracefully and just tries again next run — it
  won't break your daily discovery).

**You can skip this and add sources later."** (Indeed is available too but
tends to return weak results for a "Product Manager"-style search
specifically — mention it only if asked, don't offer it by default. Don't
offer Foundit — evidence says its results are consistently low-quality
regardless of search term.)

- If no (or they want to decide later): leave every paid provider at
  `enabled: false` in `providers:` (already the default — nothing to
  write). Mention `providers/README.md`'s "Shipped providers" section for
  the full evidence behind each one, and its "Turning on a paid provider"
  section for when they're ready.
- If yes: ask **"What's your monthly budget for these paid sources, in
  USD? (This is a shared soft cap across all of them — suggested default:
  $10.)"** — accept any positive number, prefill $10 if they just want the
  default, and write it to `apify.max_monthly_budget_usd` in config.yaml.
  For each source they want on, set `providers.<name>.enabled: true` in
  config.yaml. Mention this is a best-effort estimate, not a hard ceiling
  (`careeros doctor` shows current spend vs. budget). Only now, if they
  don't already have one configured, mention that these paid sources share
  one credential behind the scenes (an Apify account — `APIFY_TOKEN` or
  `APIFY_TOKENS` in `.careeros/secrets.env`) and point them to
  `providers/README.md` for setup; it's worth also setting a matching hard
  spend limit in that account's own console.
- Either way, mention `apify.max_monthly_budget_usd` and each provider's
  `enabled`/`limit` are editable any time by hand-editing config.yaml, and
  `careeros doctor` always shows current budget-vs-spend for whatever's
  enabled.

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
sign-off on the exact wording; this is what `resume_v2.md` selects from and
may reword (never invents facts beyond) when tailoring to a job.

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
