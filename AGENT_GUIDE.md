# Agent Guide

Canonical onboarding for any AI coding CLI working in this repo (Claude
Code, Antigravity/Gemini, Codex, OpenCode, or anything else). Read this
file first, in full, before running `careeros daily` or touching pipeline
code. `CLAUDE.md`/`GEMINI.md`/`AGENTS.md` at the repo root are 3-line
redirects to this file for CLIs that auto-load a per-tool file — this is
the only copy of the actual content, kept in sync by editing here only.

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

## Pipeline stages

Run via the `daily` skill (`skills/daily.md`) — read that file for the
full step-by-step sequence, exact commands, and what each stage's output
means. This file states the *rules*; `skills/daily.md` states the
*steps*.

## Secrets handling

Real credentials live in `.careeros/secrets.env` (gitignored, never
committed). Rules, no exceptions:

- **Source it, never cat/print/grep its contents.** Load it with
  `set -a && source .careeros/secrets.env && set +a` before running any
  `careeros` command that needs a credential. If you need to check
  whether a key is set, check presence/length only (`[ -n "$VAR" ]`,
  `echo ${#VAR}`), never echo the value itself.
- **Never write a raw secret value into `.careeros/config.yaml`.**
  Config fields like `api_key_env`/`token_env`/`tokens_env` hold an ENV
  VAR **NAME** (a string like `"FANTASTIC_API_KEY"`), looked up via
  `os.environ.get(...)` at runtime — not the key itself. Writing the raw
  key into one of these fields is a real misconfiguration bug that has
  happened before; if you're editing one of these fields, the value you
  write should look like a shouty env-var name, never like a token.
- **Never persist a raw Apify token anywhere**, including new state
  files. The token-exhaustion cache (`.careeros/apify_tokens.json`)
  stores only a `sha256` fingerprint, by design — if you're touching that
  file's logic, preserve that property.

## The Failure Handling Principle

This is the standing rule for **any** non-trivial failure anywhere in
this pipeline — not just discovery. It supersedes and replaces any
narrower "stop if provider X fails" wording you might see referenced
elsewhere; those are historical special cases now folded into this one
rule. Every skill checkpoint (`skills/daily.md`, `skills/apply.md`,
`skills/prep.md`, `skills/start.md`) should point back to this section
rather than restate it.

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
> — Fantastic Jobs, Apify/any discovery provider, Drive, Sheets,
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
last-run health, Apify token pool status) — it makes no network calls
and modifies nothing. Run it before `careeros daily` so configuration
problems surface up front instead of mid-run. See `skills/daily.md`'s
Step 0.

## Testing

`pytest careeros/tests/ -q` from the repo root (needs the `[dev]` extra:
`pip install -e ".[dev]"`). See `README.md`'s
[Testing](README.md#testing) section for what's covered and what isn't.
