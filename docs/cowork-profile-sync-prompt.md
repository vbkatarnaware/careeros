# Cowork prompt — sync LinkedIn + Wellfound to the CareerOS knowledge base

Copy everything inside the fence below into Claude Cowork (with Chrome
access). Written 2026-08-01 against `profile.yaml` v10.

---

```
You have Chrome access. I need you to bring my LinkedIn and Wellfound profiles
in line with my current, accurate professional record. Both are out of date.

## Step 1 — Read my knowledge base first. Do not skip this.

My source of truth is this repo:
  /Users/vipulkatarnaware/Documents/AI Agents/careeros

Read these, in this order:

1. `.careeros/profile.yaml` — THE source of truth. Everything about me lives
   here: candidate details, headline, tagline, 4 summary_variants, full
   experience with per-bullet `tags`/`visibility`, projects, 65 skills with
   `category`/`level`/`visibility`, education, targets, role_priorities,
   ranking_notes, product_philosophy, deal_breakers, location, comp, logistics.
2. `prompts/voice-dna.md` — how I write. Follow it for every word you produce.
   Especially: no em-dashes, no "leverage/robust/seamless"-style AI vocabulary,
   and NO negative-parallelism constructions ("It's not X, it's Y") — that
   pattern is banned outright.
3. `prompts/resume_v4.md` — read the "Truthfulness rule" and "Founder-experience
   rule" sections. They govern how my founder work should be described. Apply
   the same rules here.
4. `AGENT_GUIDE.md` — repo context, if you want the bigger picture.

DO NOT use any generated resume PDF under `.careeros/runs/` as your reference.
Those are deliberately tailored to one specific job posting. LinkedIn and
Wellfound need the general version, which means `profile.yaml` plus the
`summary_variants` entry with id `default`.

## Step 2 — Rules you cannot break

These are absolute, and they matter more than making the profile sound good:

- **Never state a fact that is not in `profile.yaml`.** No invented metrics,
  employers, dates, tools, or claims. If a sentence would read better with a
  number you don't have, write the weaker sentence.
- **Every number must survive exactly.** If a bullet says "26 paying restaurant
  partners" or "$1.45M+ (₹12.05 Cr+)" or "1,99,640 paid bills", those figures
  must appear unchanged or not at all. Never round, never inflate.
- **Respect `visibility` on bullets and skills.** Use `headline` and
  `supporting` items. NEVER surface anything marked `hidden` — those are
  deliberately withheld.
- **Respect `level` on skills.** A skill marked `ai-assisted` (Mixpanel,
  Amplitude, Tableau, Power BI) must never be presented as if it were
  `hands-on`. If a platform forces a binary, omit the ai-assisted ones rather
  than overstate them.
- **Do not touch my recommendations, endorsements, or connections.**
- **Before editing any section, capture its current text** and save all of it
  to `/tmp/profile-backup-<platform>-<date>.md` so every change is reversible.

## Step 3 — Positioning decisions (I've already made these — apply them)

1. **My experience level must be obvious and findable.** I have ~3 years
   full-time: ICICI Bank Jul 2023 – Aug 2024, QRapid Sep 2024 – present.
   Recruiters filter on years, so state it plainly somewhere visible. Do not
   inflate it, and do not count my college venture toward it.

2. **Address the "will he leave to start another company?" question head-on.**
   Some recruiters see a founder and assume I'll leave. Defuse it explicitly,
   in my own voice, along the lines of: I built and ran QRapid for two years
   and I'm looking to bring that ownership into a product team, not to start
   over. Make it sound like me, not like a disclaimer.

3. **My headline is currently 209 characters — a paragraph, not a headline.**
   Rewrite it to be scannable in about 5 seconds while still carrying: Product
   Manager, my ~3 years, FinTech + B2B SaaS + AI products, and the founder
   angle. `profile.yaml`'s `tagline` field ("Product Manager | B2B SaaS |
   FinTech | AI Products") is a good skeleton to build on.

4. **Kaagjaat (Jul 2022 – Jun 2023) was a college venture**, run during my
   final year at VJTI. Label it as such. Left ambiguous, it either inflates my
   apparent tenure or makes me look like a job-hopper. Both are bad.

5. **My notice period is 1 week.** That's a genuine advantage for startups.
   Surface it on Wellfound, where availability is a real field.

6. **Target roles, in priority order:** Product Manager, AI Product Manager,
   Founder's Office, Growth Product Manager, Product Operations, and
   Associate Product Manager LAST. I'm open to APM at a genuinely strong
   company, but never signal that it's my preference.

7. **VJTI is a Tier-1 engineering college in Mumbai** and carries real weight
   with Indian recruiters. Make sure it's complete and prominent.

## Step 4 — LinkedIn (linkedin.com/in/vipul-katarnaware)

Update, in this order:
- **Headline** — per decision 3 above.
- **About** — build from the `default` summary_variant, then add the founder
  intent line from decision 2. Front-load the concrete outcomes (26 paying
  partners, $1.45M+ processed, the ICICI Business Rules Engine at millions of
  applications a year). Keep it tight; nobody reads paragraph four.
- **Experience** — all four roles from `profile.yaml`, correct titles and
  dates. Use `headline` bullets first, then `supporting` ones that add
  something new. Follow `resume_v4.md`'s founder rule: surface the PM
  substance (discovery, prioritisation, trade-offs, metrics I defined) rather
  than company-building language. Label Kaagjaat as a college venture.
- **Skills** — from the 65 in `profile.yaml`, respecting `level` and
  `visibility`. Lead with the 12 `headline` ones.
- **Education** — all three entries, VJTI first with the B.Tech and dates.
- **Featured / Projects** — CareerOS (https://careeros.codes), Rizent AI
  (https://rizent.me, private beta — say so), MoatDaily
  (https://instagram.com/moatdaily). Use their `profile.yaml` bullets. Note
  that `projects_philosophy` says a project's real status (pre-launch,
  internal, validated experiment) must survive onto the page — honour that.
- **Contact** — portfolio https://vipulkatarnaware.in, GitHub
  https://github.com/vbkatarnaware.
- **Open to work** — the six target roles above, Remote plus Mumbai and Navi
  Mumbai onsite.

## Step 5 — Wellfound (wellfound.com)

Same source material, but Wellfound reads differently — it's startup-facing,
so the founder experience is an asset there, not a risk. Lead with it.
- Headline / one-liner, About, and all four roles as above.
- **Skills and role preferences:** the six target roles in priority order.
- **Location / remote:** Remote preferred; onsite acceptable in Mumbai and
  Navi Mumbai only.
- **Availability:** 1 week notice.
- **Salary expectation:** my target is 20–28 LPA INR. Only fill this in if
  Wellfound has a dedicated field for it — never put it in free text.
- Link the same three projects.

## Step 6 — Go ahead and make the changes, then report

You have my authorisation to apply these edits directly. Don't stop to ask me
section by section. Two things still apply, because they aren't approval gates:
save the before-snapshot from Step 2 first, and never invent a fact.

When you're finished, give me one report covering:
- Every section you changed, on both platforms, with before → after.
- Anything you could NOT change (a field that's locked, a login wall, a UI you
  couldn't reach) — state it plainly rather than working around it.
- Anything in `profile.yaml` that looked out of date or contradictory while you
  were working, so I can fix the source.
- Where you saved the backup files.
```

---

## Notes for future me

- `profile.yaml` is version 10 as of 2026-08-01. If it's been bumped since,
  re-read it before reusing this prompt — the numbers above may have moved.
- The positioning decisions in Step 3 were made 2026-08-01 after finding that
  66% of fetched jobs required 5+ years experience against ~3 actual, which is
  the likely cause of ~100 applications producing zero recruiter calls.
- Full-autonomy editing was an explicit, informed choice. If you'd rather
  review section by section next time, change Step 6 to require approval
  before each save.
