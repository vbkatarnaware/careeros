# Agent Guide

Canonical onboarding for any AI coding CLI working in this repo (Claude
Code, Antigravity/Gemini, Codex, OpenCode, or anything else). Read this
file first, in full, before running `careeros daily` or touching pipeline
code. `CLAUDE.md` at the repo root is a thin redirect to this file for CLIs
that auto-load a per-tool file — this is the only copy of the actual
content, kept in sync by editing here only.

## What this repo is

CareerOS is a job-search pipeline with no server and no database. The
filesystem is the message bus: every stage reads one JSON file under
`.careeros/runs/<date>/` and writes another. See `README.md`'s
[Architecture](README.md#architecture) section for the full diagram —
don't duplicate it here, it drifts.

## The deterministic/reasoning boundary

This is the single most important thing to internalize before running
anything:

- **Deterministic (Python, `careeros` CLI)** — discover, normalize,
  dedupe, constraints, threshold, sheets, drive, lint, cache. Pure code,
  no model calls, byte-for-byte reproducible given the same inputs. You
  invoke these as shell commands; you do not reimplement their logic.
- **Reasoning (you, the agent)** — AI Gate, Final Evaluation, Resume,
  Cover Letter, Application Answers, Deep Report. You read a prompt file
  from `prompts/*_vN.md` plus `.careeros/profile.yaml`, then write the
  output file the CLI told you to write. You do not skip this by having
  the CLI "just generate" something — every reasoning step is a real
  read-prompt-then-write-file step, and every output is checked
  (schema validation, voice-dna lint, verbatim-truthfulness check)
  before it's accepted.

Never blur this boundary: don't hand-write what a deterministic stage
should compute, and don't let a deterministic stage silently stand in for
a reasoning step (e.g. don't fabricate eval scores instead of actually
reading the job and profile).

### Reasoning stages must be reasoned, never scripted

This has happened in practice — under batch-size or time pressure, an
agent wrote a Python script that pattern-matched job titles/keywords to
produce Gate keep/drop calls and Evaluate scores, instead of actually
reading each job. The output looked plausible (right shape, plausible
numbers) but wasn't real judgment, and it silently degraded the results a
candidate relies on.

> Gate, Evaluation, Resume, Cover Letter, and Application-Answer output
> must come from actually reading each job (and the profile) and reasoning
> about it — every single time, no exceptions for batch size. You must
> NEVER write or run a script — Python, keyword-matching, a fixed
> formula, anything — that assigns a keep/drop call or a rubric score as a
> stand-in for reading the job. If a batch is large, split it across
> sub-agents that each genuinely reason over their own slice; don't
> collapse the judgment into code. The only arithmetic allowed in these
> stages is deterministic math a prompt explicitly specifies over
> already-reasoned values (e.g. the eval rubric's weighted-average
> formula, or `evaluate --finalize`'s score clamp) — never a substitute
> for the reasoning itself.

This is the same boundary as above, stated as a rule for the moment it's
most tempting to cross it.

**Passing the deterministic checks is necessary, never sufficient.** Found
live 2026-07-29: a host CLI generated 25 resumes for 25 genuinely different
companies (ClanX, two different Kotak roles, DBS Bank, and others) and every
one of them carried the exact same QRapid bullets, character for character.
Nothing was technically wrong — `verify-resume` passed all 25, because
copying a bullet verbatim can never invent a number. But `resume_v4.md`'s
actual instruction is to **reword** each bullet to mirror that specific
JD's own language; verbatim copying is what you fall back to only when a
genuine reword isn't possible, not the default. Skipping the reword is the
same shortcut as the Gate/Evaluate scripting above, wearing a disguise: the
output has the right shape and passes every mechanical check, but the
per-job thinking the prompt actually asked for never happened.

> The same applies to every reasoning stage, not just Resume: doing the
> minimum that satisfies the deterministic checker (schema validation,
> voice-dna lint, fact-preservation, page-count) is not the same as doing
> what the prompt for that stage actually asked. Before finalizing any
> reasoning-stage output, ask "did I actually tailor this to THIS job, or
> did I take the safest path that would merely pass?" — and if you're
> not sure, re-read the prompt file again before writing the file.

## Pipeline stages

Run via the `daily` skill (`skills/daily.md`) — read that file for the
full step-by-step sequence, exact commands, and what each stage's output
means. This file states the *rules*; `skills/daily.md` states the
*steps*.

### Query tuning: an automatic weekly CHECK, but never the same reasoning pass as scoring

v2.0 adds a (dormant-until-armed) query-tuning loop — `careeros/pipeline/
tuning.py`. Its cheap, deterministic CHECK (`careeros tune --check-due`) runs
automatically as `skills/daily.md`'s Step 0.5, **before** discovery/gate/
evaluate for the day — that's deliberate and safe, not an exception to the
rule below. What must never happen is **the agent turn that scores TODAY'S
jobs (Gate/Evaluate) also being the turn that decides on a discovery-query
change.** `--check-due` only ever reads last month's already-written ledger
(`.careeros/learning/ledger.md`, `careeros/pipeline/ledger.py`) — never
today's fresh job batch — so there is no reasoning pass to reuse in the
first place. If it surfaces a pattern, follow `prompts/tune_v1.md`: ask the
candidate a plain-language question with real numbers BEFORE continuing to
Step 1, using only last month's aggregated data, never anything from the
run that's about to happen.

The reason is the same Goodhart risk that motivates the calibration harness
(`careeros/calibration.py`): if a query tuner could improve its own apparent
yield by influencing how jobs get scored, it would learn to do that, because
it is cheaper than actually improving discovery. Structural separations that
enforce this, so it is never just a policy an agent could ignore under
pressure: the tuner's decision metrics (gate-keep rate, constraints-rejection
rate) come from the deterministic constraints stage and the *gate* agent, not
the eval agent's own score; the config overlay it writes
(`.careeros/tuning/overlay.yaml`) is merged in by `careeros/config.py`'s
`load_config` through a hard-coded key allowlist the tuner cannot widen no
matter what it writes; and it runs on its own command (`careeros tune`), on
its own cadence (monthly by default), never inside `daily`.

## Secrets handling

Real credentials live in `.careeros/secrets.env` (gitignored, never
committed). Rules, no exceptions:

- **Source it, never cat/print/grep its contents.** Load it with
  `set -a && source .careeros/secrets.env && set +a` before running any
  `careeros` command that needs a credential. If you need to check
  whether a key is set, check presence/length only (`[ -n "$VAR" ]`,
  `echo ${#VAR}`), never echo the value itself.
- **Never write a raw secret value into `.careeros/config.yaml`.**
  Config fields like `api_key_env`/`rapidapi_key_env` hold an ENV
  VAR **NAME** (a string like `"FANTASTIC_API_KEY"`), looked up via
  `os.environ.get(...)` at runtime — not the key itself. Writing the raw
  key into one of these fields is a real misconfiguration bug that has
  happened before; if you're editing one of these fields, the value you
  write should look like a shouty env-var name, never like a token.
- **Never persist a raw API credential anywhere**, including new state
  files. If you add a cache keyed by a credential, store only a `sha256`
  fingerprint — never the key itself.

## The Failure Handling Principle

This is the standing rule for **any** non-trivial failure anywhere in
this pipeline — not just discovery. It supersedes and replaces any
narrower "stop if provider X fails" wording you might see referenced
elsewhere; those are historical special cases now folded into this one
rule. Every skill checkpoint (`skills/daily.md`, `skills/apply.md`,
`skills/prep.md`, `skills/start.md`, `skills/job.md`) should point back to
this section rather than restate it.

> If any non-trivial step in the pipeline cannot complete as intended
> (provider failure, credential issue, quota exhaustion, network error,
> missing dependency, unexpected API change, a Drive/Sheets write
> failure, resume/cover/answers generation failure, or anything else),
> the agent must:
> 1. Clearly explain what failed.
> 2. Explain why it failed (if known).
> 3. Explain what impact it has on the current run — what still
>    completed, what's now missing or reduced.
> 4. Present the available options: fix the issue and retry, continue
>    with reduced functionality, or abort.
> 5. Wait for explicit user confirmation before continuing.
>
> The agent must never silently skip important work or make assumptions
> about what the user prefers. This applies uniformly across every stage
> — Fantastic Jobs/any discovery provider, Drive, Sheets,
> Playwright/form-reading, resume/cover/answers generation, network
> timeouts — alike. One rule, applied consistently, not a per-stage
> special case.

This holds even when running under a permissions-skipping flag
(`--dangerously-skip-permissions` or equivalent) — the stop here is a
reasoning-level judgment call about incomplete/degraded work, not a
tool-permission gate, so it is not bypassed by permission settings.

## Before you start: run doctor

`careeros doctor` is a fast, read-only sanity check (Python version,
profile, discovery credentials, Sheets/Drive config, per-provider
last-run health, current vs recommended daily job limit) — by default it
makes no network calls and modifies nothing. Run it before `careeros daily`
so configuration problems surface up front instead of mid-run. See
`skills/daily.md`'s Step 0. Pass `--live` to opt into actually verifying
Fantastic Jobs against its real API (a small, bounded amount of real quota)
instead of trusting local/stored state alone — use this whenever local state
and reality might have diverged (e.g. right after rotating a key), never as
the default.

## Testing

`pytest careeros/tests/ -q` from the repo root (needs the `[dev]` extra:
`pip install -e ".[dev]"`). See `README.md`'s
[Testing](README.md#testing) section for what's covered and what isn't.
