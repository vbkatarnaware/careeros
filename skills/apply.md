# /careeros apply {job-id}

Detects the ATS and drafts answers to the actual application questions, for
ONE job, on demand. This is the manual counterpart to the automatic
Apply-tier batch that already ran inside `daily` (see skills/daily.md step
8a) — use this skill for a job that scored below `threshold` (the batch
only covers Apply-tier, >= threshold, jobs) or one the batch marked with
any non-"✅ Generated" status in the Sheet — 🔒 Login Required, ❌ Closed,
⚙️ Playwright Missing, 📄 No Essay Questions, or 🌐 Network Error (its form
wasn't readable by the background fetch, most often because it's
login-gated).

Unlike the batch stage (which reads the form in an invisible background
browser — see careeros/apply/browser.py), this skill can use the
candidate's own REAL, LOGGED-IN browser, since they're present and chose to
run this command — the right tool for a form that needs an authenticated
session to even show its questions.

## 1. Locate the job

Find `{job-id}`'s Job record (any run's `02_normalize/jobs.json`) and its
evaluation (`05_evaluate/{job-id}.json`). If either is missing, tell the
candidate and stop.

## 2. Report the detected ATS

`Job.ats` was already set at normalize time (`greenhouse` / `lever` /
`ashby` / `workday` / `custom`) — no re-detection needed. Tell the
candidate which ATS this is, mainly so they can sanity-check it matches
what they're actually looking at.

## 3. Get the questions

Two ways to get the real questions — offer both, let the candidate pick:

- **Open it yourself.** If you (the host agent) have browser tools
  available, offer to open `Job.apply_url` in the candidate's own browser
  and read the visible application questions directly — this works even for
  login-gated forms, since it's the candidate's real, already-authenticated
  session, not an automated background fetch. Confirm with the candidate
  before navigating (see your own browser-automation guidelines on
  confirming actions that affect their live browser).
- **Paste them.** Ask the candidate to paste the actual application form's
  questions themselves.

This step cannot proceed without the real questions, one way or the other.

## 4. Reuse cached context if it exists

Check for `artifacts/{job-id}/_context.json` (written by a prior
`careeros prep {job-id}` run). If present, read it and reuse its research —
don't re-run company research from scratch. If absent, that's fine; most
application question sets don't need the full deep-report research anyway.

## 5. Generate answers

Read `prompts/apply_v1.md` and follow it exactly — every answer must trace
to `profile.yaml`, the eval, or the cached context. Never fabricate
experience to fit a question; if the profile has no basis for something a
question asks, say so to the candidate rather than inventing an answer.

One exception, per the prompt's "Personal / logistics questions" section:
notice period, work authorization/visa status, salary expectations,
earliest start date, employment type, and similar candidate-level (not
job-level) facts aren't a fabrication gap — if `profile.yaml` doesn't have
one yet, ask the candidate for it right here in chat, then write the answer
into `.careeros/profile.yaml`'s `logistics.*` (no `version` bump needed —
see the prompt) before moving on, so every future application — batch or
on-demand — reuses it automatically instead of asking again.

Write `artifacts/{job-id}/answers.md`.

## 6. Lint and review

```
careeros lint artifacts/{job-id}/answers.md
```

Reread each answer once against `profile.yaml` before showing it to the
candidate — every specific claim should be traceable to an actual fact.

## 7. Publish to Drive + the Sheet — ALWAYS run this, not optional

```
careeros publish {job-id} --date {date the job was discovered}
```

Run this automatically as the last step, without asking — the candidate
should never have to remember a separate command to get a finished
answers file into Drive and their Sheet. Uploads `answers.md` (rendered to
PDF if the optional `[pdf]` extra is installed) to Drive and fills in that
row's Application Answers (Drive) cell — replacing whatever status label it
showed before (e.g. "🔒 Login Required"). Requires `drive.enabled: true`;
if Drive isn't configured (the only case where this step is skipped), tell
the candidate the answers file's local path instead.

## 8. Report back

Show the candidate the answers file path (and the Drive link if step 7
ran). If any question couldn't be answered from real facts, say which one
and why, so they can fill it in themselves rather than being handed a
fabricated answer silently.
