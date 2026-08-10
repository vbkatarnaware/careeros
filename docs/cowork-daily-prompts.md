# Cowork prompts: scheduled daily run + on-demand form filling

Two separate prompts, deliberately split.

**Prompt A** runs unattended on a schedule. It does everything up to and
including generating resumes, cover letters, and reports.

**Prompt B** you run yourself, when you're at the machine. It opens the
applications and fills the forms. It never submits — you do that.

They are split because submitting an application is irreversible and
outward-facing. Prompt A can safely run while you sleep because nothing it
does leaves your machine except a Drive/Sheets sync of your own files.

---

## Prerequisites (both prompts)

The agent must run **on the machine where `.careeros/` lives**. The whole
directory is gitignored — profile, config, secrets, and run history are all
local-only. A clean cloud checkout of the repo cannot run this pipeline:
`careeros doctor` fails on the missing profile, and dedupe loses the
`processed.jsonl` history that stops already-seen jobs from re-entering.

Working directory: `/Users/vipulkatarnaware/Documents/AI Agents/careeros`

---

## PROMPT A — scheduled daily run (~10:00-11:00 IST, all 7 days)

```
Run the CareerOS daily pipeline for today.

Working directory: /Users/vipulkatarnaware/Documents/AI Agents/careeros

Setup for every command:
  source .venv/bin/activate && set -a && source .careeros/secrets.env && set +a

Follow skills/daily.md exactly. It is the canonical playbook — read it first
and do not improvise around it.

Two rules that override any instinct to be helpful and keep going:

1. THE FAILURE HANDLING PRINCIPLE (AGENT_GUIDE.md). If any stage fails or
   reports something unexpected — a provider skipped, a quota error, a
   verify-live concern, a lint failure you cannot fix from real profile
   facts — STOP. State what failed, why, what it means for today's run, and
   what the options are. Do not guess, do not work around it, do not
   silently continue with partial data. Leave it for me to decide.

2. REASONING STAGES MUST BE REASONED, NEVER SCRIPTED. The AI Gate and
   Evaluate stages require actually reading each job description and each
   job's real content. Never write a script, keyword matcher, or heuristic
   to produce keep/drop calls or rubric scores. This has gone wrong before
   in this project and is a standing rule in AGENT_GUIDE.md.

Specific things to watch for today:

- The discovery window is now 24h (changed 2026-08-07). If discover returns
  0 items across all queries, that is a real signal worth flagging, not
  something to retry around.
- config.api.limit is 180, which deliberately exceeds the free plan's
  weekly guard. `careeros doctor` WILL warn about this every run. That
  warning is expected and correct — do not "fix" it by lowering the limit.
- The "5-10" experience band is an UNVALIDATED TEST (see the long comment
  in .careeros/config.yaml). When you report back, tell me separately how
  many 5-10-band jobs were fetched, how many survived the gate, and how
  many reached Apply tier. That specific number is what decides whether the
  band stays.
- Watch for duplicate/repost jobs (same role posted under a second company
  name, or a role already applied to on a previous day). Flag them rather
  than generating a second set of artifacts. Check .careeros/processed.jsonl
  and recent runs' selected.json before assuming a job is new.

Generate resumes and cover letters ONLY for Apply-tier jobs (score >= 4.0),
which is what `careeros artifacts` already does. Every resume must pass
`careeros verify-resume` and `careeros lint` before finalize. If a lint or
truthfulness check fails, fix it using real facts from profile.yaml — never
by inventing or softening a fact to satisfy the checker.

Do NOT open a browser. Do NOT fill or submit any application form. This run
stops after summary/Drive/Sheets.

When done, report:
- The funnel (discovered / unique / eligible / gated / evaluated / selected)
- Each Apply-tier job: score, company, title, and the one-line reason
- Each Consider-tier near-miss with its score
- Anything dropped at gate that was a close call worth my attention
- The 5-10 band numbers described above
- Any job whose application form could not be read, and why
```

---

## PROMPT B — on-demand form filling (run this yourself, when present)

```
Open and fill today's Apply-tier job applications in my logged-in Chrome.

Working directory: /Users/vipulkatarnaware/Documents/AI Agents/careeros

First, read today's run to find the Apply-tier jobs:
  .careeros/runs/<today>/07_select/selected.json

For each one, confirm the generated materials actually exist on disk before
touching a browser:
  .careeros/runs/<today>/artifacts/<job-id>/resume.pdf
  .careeros/runs/<today>/artifacts/<job-id>/cover.pdf
  .careeros/runs/<today>/artifacts/<job-id>/answers.md   (may not exist)

If a job's artifacts are missing, say so and skip it — do not improvise a
resume in the browser.

Then, ONE JOB AT A TIME:

1. Open the job's apply_url in my existing logged-in Chrome session.
2. Read the actual form fields.
3. Fill them from the generated artifacts and .careeros/profile.yaml. Use
   the real cover letter text for cover-letter fields, real logistics
   answers (notice period, etc.) from profile.yaml's `logistics` block.
4. For any free-text question the artifacts do not already answer, write the
   answer in my voice, grounded ONLY in real facts from profile.yaml. Run it
   past `careeros lint` if it is substantial. Never invent experience.
5. STOP before submitting. Show me exactly what you filled, field by field,
   and wait for me to say go.
6. I click submit, or tell you to. Then move to the next job.

Hard rules:
- Never click submit, apply, send, or any final confirmation yourself.
- Never enter passwords, payment details, or government ID numbers. If a
  form asks for those, stop and tell me.
- If a form is behind a login wall you are not already authenticated for,
  or shows a CAPTCHA / bot-check, stop and tell me. Do not attempt to work
  around it.
- If a question has no honest answer from my real background, tell me
  rather than writing something plausible.

At the end, tell me which jobs are filled and awaiting my submit, which were
skipped, and why.
```

---

## Notes on the split

The reason B is not folded into A: submitting a job application is
irreversible and goes out under your name. Prompt A is safe to automate
because everything it produces stays on your machine (plus your own Drive
and Sheet). Prompt B needs you present because a wrong answer, once
submitted, cannot be recalled — and the review step is what has caught real
errors in this project before.
