<!--
Stage: cover (part of skills/daily.md's artifact-generation step).
Cache key: sha1(job_hash + profile.version + eval.score + prompt_version).
Input: profile.yaml, the Job (including its full `description`), and its
06_evaluate/<id>.json.
Output: artifacts/<job-id>/cover.md

v2 changes three things versus v1, all aimed at the same failure. Measured
across the 18 letters generated on 2026-07-26, v1 produced openings that were
interchangeable between companies:

  "Marvin's focus on AI-driven B2B SaaS solutions caught my attention."
  "Mercury's focus on providing financial tools ... caught my attention."
  "I am applying for the Product Manager role at Capgemini Invent."

Root cause: v1 told you to build the letter from the eval's
`company_summary`, but 67 of that run's 149 evals (45%) have a thin or
generic summary — literal values like "Company details are limited but the
role focuses on core PM execution." With nothing concrete to work from, the
letter fell back to filler.

The job's OWN description was passed to this stage the whole time and v1
never mentioned it. v2: (1) mine the JD first, (2) research the company
where it's findable, (3) choose an entry angle from the evidence instead of
running one fixed template. The grounding rules are unchanged and absolute.
-->

# Cover Letter v2 — specific, researched, and true

Import `prompts/voice-dna.md` for tone; it governs every sentence you write.

## The rule that outranks everything below

**Every claim about the candidate traces to `profile.yaml`.** Research and
JD-mining change what you can say about *the company* — they never expand
what you can say about the candidate. If a fact about Vipul isn't in the
profile, it does not exist, however well it would fit the letter. Reframe a
JD theme in the candidate's own adjacent wording; never adopt the JD's term
as his own unless a profile fact actually backs it.

## Step 1 — Mine the job description

Before you look at anything else, read the Job's `description` in full and
pull out what is *specific to this posting*. You are looking for one concrete
hook — something no other company's letter could contain:

- a named product, feature, or internal tool
- a problem the team states it has ("our onboarding takes three weeks")
- team shape, stage, or who the role reports to
- a specific technology, metric, scale figure, or customer segment
- an unusual responsibility that isn't standard for the title

This is the highest-value input available to you and it is usually the only
genuinely differentiated one. **Read it before falling back to anything else.**

## Step 2 — Research the company

Look up what you can verify about the company and its domain: what it sells
and to whom, its stage, recent launches or news, and one domain-specific
detail that shows real familiarity with the space (a regulatory constraint, a
competitive dynamic, a known hard problem in that vertical).

Two limits, both absolute:

- **Research informs the company paragraph only.** It is context about them,
  never a claim about him.
- **If you cannot verify it, do not write it.** An invented funding round,
  product name, or customer count is far worse than a plainer letter — it is
  the exact failure this whole pipeline exists to prevent, and it is the kind
  of error a hiring manager catches instantly. Silence beats a guess.

## Step 3 — Rank your raw material

For the "why this company" content, in strict order of preference:

1. **A specific detail from the JD** (step 1) — always strongest.
2. **Verified research** (step 2).
3. **The eval's `company_summary`** — use it only when it says something
   concrete. If it reads like "company details are limited," it is not
   usable material; skip it rather than padding with it.
4. **Nothing.** If none of the above yields something real, do not
   manufacture enthusiasm. Open instead on the candidate's most relevant
   shipped work — a letter that is plain but true reads far better than one
   performing excitement about a company it knows nothing about.

The eval's `fit_paragraph` remains the spine of the *fit* content — it
already answers "why this candidate," reasoned once at evaluation time.
Build on it rather than re-deriving fit from scratch.

## Step 4 — Choose an entry angle

Do not run the same shape every time. Pick the angle the evidence actually
supports:

- **Problem-first** — the JD names a pain he has solved before. Open on the
  problem, not on himself.
- **Parallel** — he has shipped this specific thing. Lead with it plainly.
- **Question** — the JD raises a real product question worth asking. Only if
  it's genuine; a rhetorical question is worse than no question.
- **Wedge** — one unusual overlap most applicants won't have (fintech
  scale + AI automation, founder ownership + enterprise process).

**Choose on evidence, not for variety.** Two near-identical JDs *should*
produce two similar letters — that is correct behaviour, not repetition to
be avoided. Rotating angles to seem different is its own kind of dishonesty.

## Banned phrasing

These appeared across v1's output and mark a letter as machine-written.
Never use them or close variants:

- "caught my attention"
- "I am applying for the {role} role at {company}"
- "is a compelling fit" / "highly compelling fit"
- "This is an exceptional fit across all dimensions"
- "perfectly matches" / "aligns closely with"
- "well-equipped to"
- "I am excited to apply"
- "I look forward to hearing from you"

More generally: any sentence that would still be true with the company name
swapped out is filler. Cut it.

## Shape

- **Opening**: the hook from step 1/2, in the angle chosen in step 4. Never a
  generic statement of interest.
- **Middle** (1-2 short paragraphs): the fit, built from `fit_paragraph` plus
  1-2 supporting profile facts with their real numbers. Specific, and not a
  restatement of the resume.
- **Close**: brief and confident, with a clear next step. Say something with
  texture, or stop — no sign-off filler.
- **Length**: <=250 words. Shorter and sharper beats longer and safer.

## Before finishing (mandatory)

1. Reread: does every specific claim about the candidate (domain, metric,
   employer, skill) actually appear in `profile.yaml`? Fix any that don't.
2. Reread: is every company fact either in the JD or genuinely verified? Cut
   anything you cannot source. **This is the fabrication check — be strict.**
3. Scan for the banned phrases above, and for any sentence that survives the
   company-name-swap test. Cut what fails.
4. Run `careeros lint artifacts/<job-id>/cover.md` and resolve every issue.
5. Critical Review Gate: read it once as the hiring manager receiving it.
   Does it sound like a template with names swapped in, or like someone who
   actually read this posting? If the former, the opening is usually the
   problem — go back to step 1.
