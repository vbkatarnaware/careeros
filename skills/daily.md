# /careeros daily

The full daily pipeline. Run this in your host coding CLI (Claude Code,
Codex, Gemini CLI, OpenCode, etc.) — it drives a sequence of deterministic
`careeros` CLI calls interleaved with a few reasoning steps you (the agent)
perform directly. `careeros scan` is an alias for the same pipeline.

Target: end to end in 15-30 minutes. Most of that time is the deterministic
stages and I/O; the reasoning steps are deliberately narrow and batched to
keep token spend low.

**Failure Handling Principle.** Every step below can fail in ways specific
to that step, but the response is always the same rule, defined once in
[`AGENT_GUIDE.md`](../AGENT_GUIDE.md#the-failure-handling-principle): state
what failed, why (if known), the impact on this run, the available
options, then wait for explicit confirmation before continuing. Applies
uniformly to every step in this file — never just the ones that call it
out by name.

## Step 0 — Environment (deterministic)

```
set -a && source .careeros/secrets.env && set +a
careeros doctor
```

Loads real credentials into the shell (never cat/print/grep
`secrets.env` itself — see `AGENT_GUIDE.md`'s Secrets handling section)
and runs the read-only pre-flight checklist: Python version, profile,
discovery credentials, Sheets/Drive config, and per-provider last-run
health. `doctor` makes no network calls and changes
nothing. If it reports any FAIL, that's a configuration problem to
surface via the Failure Handling Principle before Step 1 even starts —
don't discover it mid-run.

## Step 0.5 — Weekly tuning check (deterministic, automatic, v2.0)

```
careeros tune --check-due
```

Cheap and safe to run every single day — most days it's a no-op ("Not due
yet"). Reads ONLY last month's already-written ledger, never today's fresh
jobs, so this never overlaps with the reasoning Gate/Evaluate do later in
this same run (see `AGENT_GUIDE.md`'s query-tuning section for why that
separation matters).

If it prints `"pattern(s) worth asking the candidate about"`, read
`.careeros/tuning/pending_flags.json` and follow `prompts/tune_v1.md`: ask
the candidate about EACH flag in plain language with the real numbers
(no jargon, no file paths) — a simple yes/no is enough. If they say yes,
run `careeros tune --propose` with the value they agreed to, citing the
evidence, then `careeros tune --apply`. If they say no, do nothing further
— it may surface again next week if the pattern persists, and that's fine.

If it prints `"nothing worth flagging this week"`, say nothing about it at
all and continue straight to Step 1 — the candidate asked not to be
bothered on quiet weeks.

## Step 0.6 — Reference registry sync (deterministic, automatic, v2.0)

```
careeros registry sync
```

Only meaningful if `ats-watchlist` is enabled and you're hand-adding
companies to `.careeros/watchlist.yaml` — this keeps the local lookup
(`careeros registry find <name>`) current so you can check "is this
company already in the free hosted snapshot" before writing a new
watchlist entry, instead of guessing or adding a redundant one.

Cheap and self-limiting: it always fetches the small upstream
manifest.json first, and skips the 6.8MB company-directory download
entirely when `manifest.generated_at` hasn't changed since the last sync
— most days this is a few-KB check, not a real download. Safe to run
every single day unconditionally.

**This step is entirely independent of Step 1 and must never gate,
skip, or delay it.** The reference registry answers "which companies do
we know about" — a lookup convenience. The job dataset (Step 1) answers
"what jobs are available today" — a completely different freshness
domain (see `careeros/registry.py`'s module docstring). Run this step,
then always continue to Step 1 regardless of whether anything changed
here.

**Watchlist maintenance needs no separate step.** Every `.careeros/
watchlist.yaml` entry is already re-verified as a normal part of Step 1
— the `ats-watchlist` provider (if enabled) scrapes every entry on every
`discover` run and updates `.careeros/watchlist_state.json` itself
(`verification_status`, `last_checked_at`, `consecutive_failures`, ATS-
migration `history[]`). A watchlist is small by design (a handful of
companies you've hand-identified), so re-checking all of it daily is
cheap — there is no larger set to throttle. Run `careeros watchlist list`
any time to see current status without triggering a fetch.

## Step 1 — Discover (deterministic)

```
careeros discover --date {today}
```

By default this runs one segmented query per `profile.yaml`'s
`work_mode_priority` tier (e.g. global-remote, India-remote — all onsite
cities in `location.onsite_ok` are consolidated into a single query, not one
per city), each searching all `role_priorities` at once — see
`careeros/pipeline/queryplan.py`. Prints a line per query with its own limit
and count, then the combined total. Set `discovery_mode: single` under the
`api:` block in `.careeros/config.yaml` to fall back to one broad query
instead. Each tier uses `--limit` by default;
`tier_limits` (same block) can override the limit for a specific tier (e.g.
give a historically high-converting tier more headroom) — check `run.json`'s
cost-per-selected-job over a few days before tuning this, rather than
guessing.

Reports N raw items fetched across every provider enabled in
`.careeros/config.yaml`'s `providers:` block. A provider that fails
(invalid/exhausted credentials, budget cap hit, an account-level API error)
does NOT abort the others — it's marked `skipped` with a plain-English
reason in the printed output and in `raw.json`'s `meta.<provider>` block,
and the run continues with whatever's left.

**Apply the Failure Handling Principle (`AGENT_GUIDE.md`) if ANY provider
was skipped this run — not just if every provider was.** A single skipped
provider means today's job list is missing a source the candidate
deliberately enabled: read out that provider's own skip reason verbatim
(don't paraphrase it away), explain the impact (fewer sources feeding the
rest of the pipeline), and present the real options — fix it now
(new/rotated API key, raise the weekly quota, edit
`.careeros/config.yaml`) and re-run `discover`, or continue with just the
providers that succeeded. Wait for the candidate's answer either way. If N
is 0 for the whole run (every provider skipped), the same discover output
already lists every provider's reason in one place — nothing downstream
can run without jobs, so this is an abort, not a reduced-functionality
continue.

## Step 2 — Normalize (deterministic)

```
careeros normalize --date {today}
```

## Step 3 — Dedupe (deterministic)

```
careeros dedupe --date {today}
```

Drops jobs already seen in a prior run or already in the Sheet, plus the same
role posted separately per country (segmented discovery can surface one
posting multiple times — e.g. the same role tagged for Poland/Bulgaria/Spain
— which this collapses to one entry, keeping the highest work-mode-priority
copy). Report the counts (in-run / cross-location / vs-history / vs-sheet) so
the candidate can sanity-check volume.

## Step 4 — Constraints (deterministic hard deal-breakers)

```
careeros constraints --date {today}
```

Applies the two objective, non-negotiable rules — location (onsite/hybrid
outside `profile.yaml`'s `location.onsite_ok` cities) and salary (only when a
number is actually known and confidently below `comp.floor_lpa`) — via
`careeros/pipeline/constraints.py`. Rejected jobs are written to
`04_constraints/rejected.json` with `_reject_reasons` and never reach the AI
gate, so no tokens are spent evaluating a job that can never be applied to.
Report the eligible/rejected split.

## Step 5 — AI Gate (cheap reasoning)

Per `AGENT_GUIDE.md`'s "Reasoning stages must be reasoned, never scripted":
every keep/drop call in this step must come from actually reading the job,
regardless of batch size — never a script.

```
careeros gate --prepare --date {today}
```

This prints an instruction block. Follow it exactly: read
`prompts/gate_v1.md` and `.careeros/profile.yaml`, then for each
`05_gate/_input_N.json` batch write the matching `_output_N.json`. Then:

```
careeros gate --finalize --date {today}
```

If finalize reports validation errors, fix only the listed items and
re-run `--finalize` — do not regenerate the whole batch.

## Step 6 — Final Evaluation (the real reasoning step)

Per `AGENT_GUIDE.md`'s "Reasoning stages must be reasoned, never scripted"
and its scoring contract: score every rubric dimension honestly (including
`logistics` — never zero it, or otherwise fudge it, to force a deal-breaker
through). `evaluate --finalize` enforces the "green means apply-able"
guarantee deterministically, so you never need to hand-tune a dimension to
make a skip-recommendation job score low.

```
careeros evaluate --prepare --date {today}
```

This writes cache hits directly and tells you how many jobs actually need
evaluation (often far fewer than the gate's keep count, thanks to caching
across runs). For the jobs that need it: read `prompts/eval_v2.md` and
`.careeros/profile.yaml` (in particular `role_priorities`, `ranking_notes`,
`work_mode_priority`), write one `06_evaluate/<job-id>.json` per job. Every
job reaching this stage already passed the deterministic constraints check,
but still set `recommendation: "skip"` if you independently spot a
deal-breaker the structured fields couldn't catch (e.g. a JD-stated
requirement not present in `Job.salary`/`Job.remote`). Then:

```
careeros evaluate --finalize --date {today}
```

Fix any schema-validation errors for just the listed jobs and re-run
`--finalize`.

## Step 7 — Threshold (deterministic)

```
careeros threshold --date {today}
```

Two-tier selection (`partition_evals` in `careeros/pipeline/threshold.py`),
both thresholds configurable (`threshold`, default 4.0; `consider_threshold`,
default 3.5):
- **Apply** — score ≥ `threshold` AND `recommendation == "apply"` AND still
  passing the deterministic constraints re-check (the guaranteed backstop
  against the AI mislabeling a hard-reject as "apply") → gets artifacts
  generated below (the cost control — resume/cover/report generation is the
  most expensive-per-job step, skip it for jobs that won't become an
  application).
- **Consider** — `consider_threshold` ≤ score < `threshold`, constraints
  passing → a Sheet row only (score + a concise reason), no artifacts, no
  Drive. Gives visibility into near-misses at zero extra AI cost.
- Anything else (a hard-constraint failure, or score < `consider_threshold`)
  is omitted from the Sheet entirely, but still written to
  `07_select/omitted.json` (v2.0) — nothing is silently discarded, it's just
  not artifact/Sheet-worthy.

`threshold` also writes `.careeros/processed.jsonl` (v2.0) unconditionally,
for every job that reached a terminal decision anywhere this run —
constraints-rejected, gate-dropped, evaluate-omitted, consider, and apply —
not just the ones that made it to a Sheet row. This is what tomorrow's
`dedupe` checks to keep an already-decided job from re-entering the pipeline
and re-burning gate/eval tokens; it replaces the old `seen.jsonl`, which was
only ever written from the Sheets step below and only for Apply/Consider
ids, so a local-only run (Sheets disabled) never got any history tracking at
all.

## Step 7.5 — Verify the live posting (deterministic, v2.1)

```
careeros verify-live --date {today}
```

Runs BEFORE artifacts so a job that turns out to be impossible never costs a
generated resume. For each Apply-tier job only (1-3 on a typical day), fetches
the REAL posting and checks the two things the provider gets wrong most often:
years-of-experience and location/work-arrangement.

This exists because of two same-day misses on 2026-07-31, both from trusting
provider metadata: MDOTM was labelled `remote: true` but is Milan-hybrid, and
AU Small Finance Bank was labelled "2-5 years" (with a 1,509-char description
that never stated a bar) while the live posting asks for 7. Neither was a
reasoning failure — the pipeline scored honestly against wrong data, which is
exactly why no smarter filter or prompt could have caught them.

It FLAGS, never auto-rejects. If anything is flagged, surface it to the
candidate in plain language per the Failure Handling Principle **before**
running Step 8 — a job whose live posting contradicts what we scored should
not silently get a resume generated for it. Let them decide: keep, drop, or
re-score. An unreadable posting (login wall, closed, network error) is
reported as "couldn't verify", not as "the job is fine".

## Step 8 — Artifacts, for each selected job

```
careeros artifacts --prepare --date {today}
```

Cache hits (same job content + profile version + eval score + prompt
version as a prior run) are written directly to
`artifacts/<job-id>/resume.json` + `artifacts/<job-id>/cover.md` with zero
reasoning spent. For whatever's left, this prints an instruction block
naming exactly which job(s) need a resume and/or cover letter. For each one:

1. **Resume** — read the resume prompt named by `config.prompts.resume`
   (`prompts/resume_<version>.md`). Write
   `artifacts/<job-id>/resume.json` (tailoring zones only — canonical facts
   like company/dates/education are merged in from `profile.yaml` at render
   time, never written here). The governing rule: reword bullet language to
   mirror the JD's keywords, but every number/entity from the source
   `profile.yaml` bullet must survive the reword unchanged (no invented or
   dropped fact), and no field may name the target company.
2. **Cover letter** — read the cover prompt named by `config.prompts.cover`
   (`prompts/cover_<version>.md`). Write `artifacts/<job-id>/cover.md` —
   freely written, grounded prose.

Then:

```
careeros artifacts --finalize --date {today}
```

This validates `resume.json` against `schemas/resume.schema.json`, runs the
voice-dna lint on both artifacts, and the deterministic fact-preservation +
company-name-leak check (`careeros verify-resume`) on the resume; only
passing content gets cached. It then renders `resume.pdf` (and `cover.pdf`)
**locally** via Typst (`careeros/typst_render.py`) — this happens on every
run, cache hit or not, since the PDF itself isn't cached — gated on an ATS
one-page check (`pypdf`-based): a resume that overflows one page is reported
as an error naming the page count, and the agent must trim bullets/skills in
`resume.json` and re-run `--finalize`, the same discipline as a lint failure.
If it reports schema/lint/validation issues, fix only the listed files
(using actual profile facts, not invented ones) and re-run `--finalize` — do
not regenerate artifacts that already passed. If resume or cover generation
fails outright for a job (not a fixable lint issue — e.g. a missing prompt
file or an unexpected error), apply the Failure Handling Principle before
continuing to the next job.

Finally, for each selected job, render its Level-1 report (deterministic,
zero AI):

```
careeros render-report {job-id} --date {today}
```

## Step 9 — Application Answers, for each Apply-tier job (P2.10)

```
careeros apply --prepare --date {today}
```

Automatic, Apply-tier (>= threshold) only — this is a DIFFERENT command from
the on-demand `careeros apply {job-id}` (see skills/apply.md); no job-id
argument here, it processes the whole batch. For each Apply-tier job, this
already fetched the application form's visible text in an invisible
BACKGROUND fetch (`careeros/apply/browser.py`: a lightweight HTTP fetch
first, an optional headless-Playwright fallback only if that's not enough —
never the candidate's own browser, never a visible window) and reports how
many jobs have a readable form (need drafting) vs. how many don't. A job
that isn't automatically readable is assigned one of several SPECIFIC
statuses rather than one generic "manual review" bucket — 🔒 Login
Required, ❌ Closed, ⚙️ Playwright Missing (with the exact install command),
or 🌐 Network Error — each mechanically detected by `browser.py` and
recorded per-job; these are expected outcomes, not failures to apply the
Failure Handling Principle to individually. If instead something is
broken at the run level (e.g. every single job comes back Network Error,
or Playwright is missing project-wide rather than per-job), that's a
systemic failure — apply the Failure Handling Principle rather than
quietly proceeding job-by-job.

For jobs needing drafting, this prints an instruction block naming exactly
which job(s) need answers, each with its fetched form text. For each one:

1. Read `prompts/apply_v1.md`. Identify the real application questions from
   the job's `form_text` — do not invent questions if the fetched text
   doesn't actually contain any real free-text essay questions (this only
   happens for a job whose form WAS genuinely fetched and readable — the
   login/closed/playwright/network cases above are already ruled out by
   this point); simply skip that job, it's marked "📄 No Essay Questions"
   automatically.
2. Write `artifacts/<job-id>/answers.md` for every job whose questions you
   could identify. Per the prompt's "Personal / logistics questions"
   section: a question about notice period, work authorization, salary,
   start date, or employment type is answered from `profile.yaml`'s
   `comp`/`logistics` fields if present; if not, don't ask mid-batch (the
   same fact would get asked once per job) — leave a `[NEEDS: <label>]`
   placeholder in that answer and keep going.
3. Once every job in this pass is drafted, collect the DISTINCT
   `[NEEDS: ...]` labels actually used across all of them (usually one or
   two, since the same facts repeat job to job) and ask the candidate about
   each ONE TIME. Write the answers into `.careeros/profile.yaml`'s
   `logistics.*` (no `version` bump — `logistics` doesn't affect
   gate/evaluate/resume/cover), then go back and replace every
   `[NEEDS: ...]` placeholder across this batch with the real answer. Skip
   this step entirely if no placeholders were left. Every future batch AND
   the on-demand `careeros apply <job-id>` skill reuse these same saved
   answers automatically — this is a one-time cost, not a per-run one.

Then:

```
careeros apply --finalize --date {today}
```

This runs the voice-dna lint on every `answers.md` written and records each
job's status (one of `generated`, `login_required`, `playwright_missing`,
`closed`, `no_essay_questions`, `network_error`, or the generic
`manual_required` fallback) for the Sheets step below. If it reports
issues, fix only the listed files and re-run `--finalize`.

## Step 10 — Day summary (deterministic, zero AI)

```
careeros summary --date {today}
```

Renders `.careeros/runs/{today}/summary.md` — funnel counts, the Apply
(≥threshold) list, the Consider (near-miss, consider_threshold to threshold)
list, and cost-per-selected-job. Reads `07_select/selected.json`/
`consider.json` (the SAME partition Step 7 already computed) rather than
re-deriving it — the summary must never disagree with what actually got
artifacts/Sheet rows. This is the P2.6 KPI made visible every run:
**maximize interview-worthy jobs per dollar, never a fixed daily quota** —
0 selected on a given day is a legitimate, supply-limited outcome, not a
failure to report as one. Runs BEFORE Drive/Sheets below since both may
include/link it.

## Step 11 — Drive backup (optional, off by default)

```
careeros drive --date {today}
```

Only runs if `drive.enabled: true` in `.careeros/config.yaml` (otherwise
prints one line and exits — nothing to do). Uploads every Apply-tier job's
Resume + Cover Letter (already rendered locally as PDF by Step 8's
`--finalize`, via Typst — the optional `[drive]`/`[resume]` extra; falls
back to a legacy render, then to Markdown, with a warning, if that's
unavailable) + Application Answers (always Markdown, never PDF; only if
Step 9 actually generated one for that job), Evaluation, and Deep Report (if
`prep` has been run on it) into ONE flat Drive folder (no
per-company/per-job subfolders — see `drive.root_folder_id`/
`drive.date_subfolder`), plus `run.json`/`summary.md`, as an ADDITIVE
backup; local Markdown is never moved or replaced. Re-uploading the same
job updates its existing files in place (idempotent). The command itself
catches a Drive failure (auth, network, quota) and reports it as a
warning rather than raising — Sheets (next step) is never blocked by a
Drive problem. That does not mean the agent should let it pass quietly,
though: surface a reported warning to the candidate per the Failure
Handling Principle (what failed, why if known, that today's Sheet rows
will be missing Drive links as a result) rather than only mentioning it
in passing at Step 13. Writes
`.careeros/runs/{today}/drive_links.json` on success, which Sheets (next
step) reads to populate the Resume (Drive) / Cover Letter (Drive) /
Evaluation (Drive) / Deep Report (Drive) / Application Answers (Drive)
columns (no Drive Folder column — P2.10 dropped it as redundant once every
job has its own direct file links).

## Step 12 — Sheets (deterministic, optional — off by default)

```
careeros sheets append --date {today}
```

Only runs if `sheets.enabled: true` in `.careeros/config.yaml` (otherwise
prints one line and exits — nothing to do; same optional/config-gated
pattern as Step 11's Drive backup). In local mode (both `sheets.enabled` and
`drive.enabled` false), Step 10's `summary.md` — elevated to
`.careeros/results/{today}/` — is the candidate's full record of the run;
there is no separate "local digest" step to run.

Inserts one row per Apply job (with per-file Drive links if Step 11 ran
successfully — Application Answers shows a specific status label, e.g.
"🔒 Login Required" or "❌ Closed", instead of a link for a job Step 9
couldn't auto-read) AND one row per Consider job (score + a concise reason,
blank artifact/Drive cells — see Step 7) directly BELOW the header, pushing
every existing row down — each day's newest run reads at the top of the
Sheet, not buried under a growing history (P2.11). Every new Apply/Consider
row also gets a `Status` dropdown cell defaulted to "Not Applied" — that
column is the candidate's own to update by hand afterward (Applied,
Interview, Rejected, ...) and the pipeline never touches it again once set.
(History tracking for tomorrow's `dedupe` no longer happens here — see Step
7's `.careeros/processed.jsonl` note; this step only appends rows.) This
also auto-migrates the Sheet schema (removes
any leftover deprecated columns, adds new ones, applies header + Score-color
+ Status-dropdown formatting) on every call — see `careeros sheets migrate`
to run that pass standalone without appending anything (it also sorts a
pre-P2.11 Sheet's existing rows by Date descending, a one-time fix for
history written bottom-up before this change).

Apply-tier rows from BEFORE Drive backup was enabled don't get these links
retroactively from `daily` — run `careeros backfill-drive` once (dry-run by
default) to add Resume/Cover links to existing Sheet rows; for Evaluation /
Deep Report / Application Answers links on a specific already-appended row,
use `careeros publish <job-id> --date <date>` instead (see skills/prep.md,
skills/apply.md).

A Sheets append failure (auth, quota, network) is not caught the way
Drive's is — apply the Failure Handling Principle immediately: this is the
step that actually delivers today's results to the candidate, so a
failure here has real impact and should not be silently retried or
skipped.

## Step 13 — Report to the candidate

Read `summary.md` back and relay it, briefly. Point them at the Sheet. Do not
dump full report/resume/cover/answers text into the chat — the artifacts are
the deliverable, `summary.md` is the day-level pointer to them.

## Step 14 — Opportunistic company discovery (agentic, bounded, optional)

Non-blocking, runs after Step 13, using today's results as evidence — never
before Step 1, and must never delay reporting to the candidate.

**Cheap no-op check first.** Look at today's `07_select/selected.json` +
`consider.json` and whatever the Gate/Evaluate reasoning already surfaced
about company/industry/source patterns. If nothing plausible comes to
mind, say so in one line and stop — most days this should cost nothing,
not trigger a search.

**If something's worth a look:**

1. Read the FULL `.careeros/profile.yaml`, not a fixed subset of fields —
   `targets`, `role_priorities`, `work_mode_priority`, `location`,
   `deal_breakers`, `comp`, and anything else present are all fair game as
   targeting/exclusion signal. This step must work unmodified for any
   OSS user's profile schema, including fields that don't exist yet.
2. Using today's kept/considered jobs as analogy ("companies like the ones
   that worked today, for these roles/geography/comp expectations"),
   reason toward AT MOST 3 candidate companies. Web search if the host
   provides it; if not, reason from the profile + today's evidence alone
   and skip gracefully — this step must never hard-fail for lacking a
   search tool, and must never become a generalized crawler.
3. **Reference registry is authoritative for "already known."** Run
   `careeros registry find <name>` for each candidate — a match means this
   company is already in the free hosted snapshot; do not propose it,
   full stop, regardless of whether Layer 1 currently surfaces a job for
   it. Also check the current `.careeros/watchlist.yaml` — skip anything
   already tracked there.
4. Check `.careeros/discovery_candidates.json` (agent-maintained, see
   below) for this company's last outcome. Skip a company with a
   `status: unresolved` entry unless `last_checked_at` is 30+ days old OR
   you have a concrete reason to believe it changed (e.g. evidence today
   that it moved careers platforms) — record that reason if you re-check
   early. A missing or unparseable file is just an empty history, not a
   failure — never let a bad read block this step.
5. For each surviving candidate, run
   `careeros watchlist add <name> [--url ... | --ats ... --slug ...]` —
   the existing deterministic validation path. **Never hand-write a
   watchlist entry and never accept your own guess as verified** — an
   UNRESOLVED result from that command is the real answer for this
   company today.
6. Update `.careeros/discovery_candidates.json` yourself (plain JSON,
   normalized company name as key) for every candidate you looked at this
   run:
   ```json
   {
     "acme corp": {"status": "unresolved", "last_checked_at": "2026-08-10", "reason": "no detectable ATS"}
   }
   ```
   Only `unresolved` outcomes need an entry — a company that resolved is
   already tracked in `watchlist.yaml` itself (checking there again next
   time is enough, no need to duplicate that state here), and a company
   skipped because it's already in the registry/watchlist doesn't need
   recording either, since step 3 answers that live, every time, for free.

**On explicit request** ("find more companies like X"), run this same
procedure mid-conversation, any time — not gated to `daily`. A
user-supplied company/URL always goes straight to `careeros watchlist add`
regardless of this step; skip steps 1-2 (there's nothing to reason toward,
the candidate is already named) but still run steps 3-6.

Report at the end of this step: how many candidates were considered, how
many were skipped as already-known (registry/watchlist/recent-unresolved),
how many were verified vs. came back UNRESOLVED. Zero candidates on a
given day is a normal, expected outcome, not a failure to report as one —
same principle as Step 10's zero-selected-jobs case.

## On failure

Every stage's output lives in `.careeros/runs/{today}/`, so `daily` is
resumable: once a failure has been surfaced and resolved per the Failure
Handling Principle, fix the cause and re-run from that stage, not from the
beginning. Never mark a step as done in your summary without confirming
its output file actually exists on disk.

## Related, but NOT part of this sequence (v2.0)

`careeros tune --check-due` (Step 0.5 above) is the one piece of the
tuning loop that DOES run automatically inside `daily` — it's a cheap,
read-only check against last month's data. Everything else stays manual
and separate:

- **`careeros calibrate`** — golden-set drift check (`--check`/`--probe`/
  `--score`/`--approve`). Weekly, or after any prompt/profile version bump.
- **`careeros ledger`** — aggregates recent runs' outcomes into
  `.careeros/learning/ledger.md` on demand (Step 0.5 already refreshes this
  itself when a check is due, so running it separately is only for
  glancing at the numbers between checks).
- **`careeros tune --propose/--apply/--revert`** — the actual query
  changes. Step 0.5 may prompt you to run these (only after asking the
  candidate first); nothing here ever calls them on its own. See
  `skills/tune.md` for the standalone workflow.
