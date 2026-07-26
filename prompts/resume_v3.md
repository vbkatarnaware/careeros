<!--
Stage: resume (part of skills/daily.md's artifact-generation step, for jobs
that survived `threshold`). Cache key: sha1(job_hash + profile.version +
eval.score + prompt_version) — see careeros/cache.py.
Input: .careeros/profile.yaml, the Job, and its 06_evaluate/<id>.json.
Output: artifacts/<job-id>/resume.json, matching schemas/resume.schema.json.
Rendered to PDF by careeros/typst_render.py — canonical facts (name, contact,
company, role, dates, education) are merged in from profile.yaml at render
time and are NEVER part of this JSON; you cannot alter them here even by
accident.
-->

# Resume v2 — reword to fit the JD, never invent a fact

Import `prompts/voice-dna.md` for tone; it governs every sentence you write.

## The one rule that governs everything below

**You are tailoring language, not facts.** v1 required every resume line to
be an exact copy of a `profile.yaml` bullet. v2 loosens that one constraint:
you may **reword** a bullet's phrasing to mirror this JD's own language and
keywords — but every hard fact inside it (every number, percentage, dollar
amount, headcount, product name, and technology named in the source bullet)
**must survive the reword unchanged**. `careeros verify-resume` enforces this
deterministically on `resume.json` and will reject any reworded bullet that
introduces a number not present in that company's canonical `profile.yaml`
bullets — so there is no benefit to fudging a metric, it will just bounce
back to you.

You still choose **which** bullets/skills to include and in **what order** —
that part of the selector model is unchanged. What's different is that the
chosen bullet's *sentence* can now be rewritten, not just copied.

## Founder-experience rule (surface PM signal, never manufacture it)

Founder and solo-operator roles hide real Product Management work behind
company-building language. When a `profile.yaml` bullet contains factual
evidence of PM responsibility, surface it in PM terms rather than leaving it
implicit. This is a **wording and selection** rule, so it never escapes the
truthfulness rule above: if the profile doesn't state it, it does not exist.

- Prefer the bullet showing **judgment** (a trade-off, a sequencing call, a
  deliberate deferral) over the one showing feature count or implementation
  detail.
- Prefer **roadmap ownership** (what got built, in what order, and what got
  deliberately postponed) over a list of what shipped.
- Prefer **customer discovery** over the technology used to act on it.
- Surface **cross-functional and organization-wide influence** (product,
  engineering, operations, onboarding, support, GTM, partnerships) **only when
  a bullet actually states it.** Never infer a team, a headcount, or a
  reporting line from a founder title alone.
- Do **not** over-index on AI work just because AI projects exist in the
  profile. AI is a capability; select it when the JD makes it relevant, not by
  default.

## Truthfulness rule (the reframe rule, extended)

Same rule as v1, now applied to full sentences rather than just summaries:
mirror the JD's *themes* and *keywords* with the candidate's own adjacent
wording, drawn from the eval's `ats_keywords`. You must **never** state a
specific domain, product, customer segment, employer, or metric that isn't
explicitly present in `profile.yaml`. If a JD says "small business lending"
and the profile only supports "consumer/retail lending systems," write the
latter — reword toward the JD's phrasing only as far as the underlying fact
actually supports. Adopting the JD's specific term as if it were the
candidate's own experience is exactly the failure mode this rule exists to
prevent, in v2 just as much as v1.

## Transferable-language rule (never name the target company)

The resume must read true for whichever employer receives it. **Never write
the target company's name** anywhere in `tagline`, `summary`, or any
experience bullet — not even to say something complimentary about it. The
company/JD context is there so you can infer domain and tone, not to be
quoted back. `careeros verify-resume` checks this deterministically too.

## Steps

1. **Read** `profile.yaml` (including `product_philosophy`, a selection lens —
   see step 5 — and `projects_philosophy`, the equivalent lens for step 7 —
   neither is ever a quotable fact), the Job, and
   `06_evaluate/<job-id>.json` (for `ats_keywords`, `strengths`,
   `fit_paragraph`).
2. **Tagline.** Usually `profile.yaml`'s own `tagline` field, used as-is. You
   may lightly reword it toward this JD's domain if a clear match exists, but
   it must stay generic/transferable (see rule above) — never JD-specific
   wording that would look odd on a different application.
3. **Summary.** Pick the `summary_variants` entry whose `jd_tags` best match
   this JD's domain (or the empty-`jd_tags` generic one if none fit). Reword
   it to mirror the JD's language and this eval's `ats_keywords`, preserving
   every fact it states.
4. **Select companies.** Decide which `profile.yaml` experience companies
   appear on this resume — a page-density and relevance call, not a fixed
   list:
   - **QRapid and ICICI Bank are always included** — the two full-time
     professional roles, never conditional.
   - **Kaagjaat is included by default.** It's a real founder venture (not
     filler) — when it's on the resume, give it the same selection care as
     any other company: pick 2-3 of its `profile.yaml` bullets by JD
     relevance, not just the first one. Only drop it if the JD explicitly
     wants post-college/full-time-only professional experience (see below),
     or if page density genuinely forces a cut after every other lever
     (fewer bullets elsewhere, dropping Yarn Bazaar first) has been tried.
   - **The Yarn Bazaar is optional filler**, not a default-include. Add it
     specifically when it helps — the JD wants more years of experience/
     history depth than QRapid + ICICI Bank + Kaagjaat alone would show, or
     there's genuine spare page room and its bullets are relevant. Otherwise
     leave it off; a tighter, more senior-reading resume beats padding.
   - **If the JD explicitly wants only post-college professional
     experience** (e.g. "no internships," "full-time roles only," "experience
     after graduation"), drop both Kaagjaat and the Yarn Bazaar — the resume
     shows only QRapid and ICICI Bank.
   List the chosen companies in `companies`, most recent first, exactly as
   they appear in `profile.yaml`'s `experience[].company`. Every company
   named here needs a matching entry in `experience` (next step); do not
   name a company here and then skip writing its bullets.
5. **Select and reword experience bullets**, per company selected above,
   most recent first: rank the company's `profile.yaml` bullets by tag
   overlap with the JD + eval's `ats_keywords`, then by `visibility`
   (headline > supporting; skip `hidden` unless the JD makes a hidden fact
   specifically relevant — rare). When two or more bullets tie on tag overlap
   and visibility, use `product_philosophy` as the tiebreak: prefer the
   bullet that shows discovery, a real trade-off, a metric the candidate
   personally defined or tracked, or a decision that changed because of
   evidence, over one that only states an output. `product_philosophy` only
   ever influences *which* bullet you pick or *how* you word it — never
   quote it, and never let it introduce a number or claim that isn't already
   in the bullet itself. Cap at 3-4 bullets per company (Kaagjaat should
   normally get 2-3, not 1, when included — it has three canonical bullets
   available). Reword each selected bullet toward the JD's keywords,
   preserving every number/entity/technology it names.
6. **Select skills**: include only entries whose `tags` overlap the JD, or
   `visibility: headline` skills as a baseline. Group by `category`. Never
   add a skill with no evidence in the profile just because the JD asks for
   it.
7. **Select 2-3 projects**: rank `profile.yaml`'s `projects` by tag overlap
   with the JD + eval's `ats_keywords`, then pick the 2 or 3 most relevant
   (3 if the page has room and more than 2 are genuinely relevant, 2
   otherwise — this is a page-density call, not a fixed number). When two or
   more projects tie on tag overlap, apply `projects_philosophy` as the
   tiebreak: prefer the project that shows discovery, a trade-off,
   validation, or a decision reversed by evidence, over one that only
   demonstrates more technology or more AI usage — a product is never ranked
   higher for being more technically complex or more AI-heavy. If bullets
   still tie after that, prefer `profile.yaml`'s own array order, which
   encodes the candidate's considered ranking of these products.
   `projects_philosophy` is a lens for this choice only: never quote it, and
   never let it introduce a number or claim not already in a project's
   `bullets[].text`. List only each project's `name`, exactly as it appears
   in `profile.yaml`. **Never reword a project's bullets** — this field is
   selection only, same
   selector-not-writer rule as v1; there's no JD-tailoring benefit to
   rewording a side-project blurb, and the renderer looks up the canonical
   bullets/url by name.
8. **Assemble** as a single JSON object (see shape below). Education is NOT
   part of this file — it renders from `profile.yaml` directly.

## Output shape

Write `artifacts/<job-id>/resume.json`, matching `schemas/resume.schema.json`
exactly:

```json
{
  "tagline": "Product Management | AI Products | B2B SaaS | FinTech",
  "summary": "Reworded 3-4 sentence summary, JD-mirrored, fact-preserving.",
  "companies": ["Acme Corp", "Old Co"],
  "experience": [
    {
      "company": "Acme Corp",
      "bullets": [
        "Reworded bullet 1, every number/entity from the source preserved.",
        "Reworded bullet 2."
      ]
    },
    {
      "company": "Old Co",
      "bullets": ["Reworded bullet."]
    }
  ],
  "skills": [
    { "category": "Product", "items": ["Product Strategy", "Discovery"] }
  ],
  "projects": [
    { "name": "CareerOS" },
    { "name": "MoatDaily" }
  ]
}
```

`company` must exactly match a `profile.yaml` `experience[].company` value —
the renderer looks up canonical dates/role/location by that exact string.
`companies` is how you deliberately include/exclude a company (see the
"Select companies" step above) — omitting a company from `companies` drops it
from the resume. If you omit the `companies` field entirely (legacy/fail-soft
path), the renderer falls back to including every `profile.yaml` company, so
always include it explicitly rather than relying on that fallback.

## Before finishing (mandatory)

1. Read every reworded bullet back against its source `profile.yaml` bullet
   — does every number, currency amount, percentage, and named
   product/technology from the source still appear, unchanged, in your
   rewrite? If you added a number that wasn't in the source, or dropped one
   that was, fix it before saving.
2. Confirm `tagline`, `summary`, and every bullet are free of the target
   company's name.
3. Run `careeros verify-resume artifacts/<job-id>/resume.json --company "<the
   target job's company>"` — this is a deterministic, mechanical check (not a
   suggestion) for both of the above.
   Any reported line means an invented/dropped fact or a company-name leak;
   fix it using the actual profile fact, not by rationalizing the change.
   `artifacts --finalize` will refuse to cache a resume that fails this
   check.
4. Run `careeros lint artifacts/<job-id>/resume.json` and resolve every
   reported voice-dna issue (em-dashes, banned vocabulary, negative
   parallelism) in the reworded text.
5. **Critical Review Gate:** read the finished resume once as a skeptical
   hiring manager would. Does the Summary answer "what role, and why this
   one?" Does the first screen show 1-2 proof points mapping to the JD's
   highest-risk requirements? Does the rewording actually sound like this
   candidate, or like a JD's keywords stapled onto their history? Would a
   senior PM reading this conclude the candidate understands customers,
   makes decisions from evidence, can build 0-to-1, and treats AI as a
   capability rather than an identity, or does it read like a founder
   listing achievements? Does the Products section read as evidence of
   product thinking, or as a list of things built with AI? Fix before
   reporting the resume as done.
