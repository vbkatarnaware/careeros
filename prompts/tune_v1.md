<!--
Stage: tune (v2.0). Triggered automatically by `careeros tune --check-due`
(skills/daily.md's Step 0.5) when it finds a pattern worth asking about, or
run manually via `skills/tune.md`. See careeros/pipeline/tuning.py and
careeros/pipeline/ledger.py for the mechanics this prompt sits on top of.
-->

# Query tuning — ask the candidate, in plain language, before proposing anything

This is a REASONING step, but a much narrower one than Gate/Evaluate: the
guardrails (`careeros/pipeline/tuning.py`) already enforce arming, the
one-change-per-cycle limit, the exclusion-list cap and sunset, the tier-limit
delta cap, and the record floor mechanically. Your job is not to invent a
clever query strategy — it's to look at ONE armed tier's actual rejected jobs,
confirm the pattern is real, and ask the candidate a plain yes/no question
before anything changes.

**The candidate is not assumed to be technical.** Never show them a tier
name, a file path, a field name, or a JSON blob. Translate everything into a
plain sentence with real numbers — `careeros tune --check-due` already
drafts one for you (`.careeros/tuning/pending_flags.json`'s `label` field)
as a starting point.

## Before you ask the candidate anything

1. `careeros tune --check-due` (already run automatically in Step 0.5) has
   written `.careeros/tuning/pending_flags.json` if anything is worth
   asking about — that's what triggered this prompt. Read it: each entry
   names a tier, a reject reason, and how many times it happened.
2. The ledger deliberately does NOT store per-job titles (that data would
   let a query-tuning pass double as a second eval-influencing surface) —
   so a flag's reason (e.g. `role-mismatch`) tells you THAT jobs were
   dropped for that reason, not WHICH specific title pattern is driving it.
3. To find the actual pattern, read the real job titles behind that reason
   yourself — open a few recent `05_gate/gated.json` / `04_constraints/
   rejected.json` files for that tier's dates and look at the titles the
   gate or constraints stage actually dropped. This is the one place this
   stage still asks you to look at real data, because the ledger's own
   privacy-by-design means it can't hand you the answer pre-packaged.
4. Ask the candidate ONE plain question per flag, with the real numbers you
   found, e.g.: *"Over the last month, 23 jobs in your Mumbai search were
   pharma marketing roles, not real product jobs — want me to stop showing
   you those?"* A simple yes / no / "tell me more" is all you need back.
5. Only proceed to `--propose` if they say yes. If no, do nothing further —
   it's fine if the same pattern surfaces again next week; the candidate
   can always say yes then. Never propose something they didn't agree to,
   and never re-ask about something they already declined without a new,
   different reason to bring it up.
6. `--evidence` is not a formality — `careeros tune --propose` requires a
   non-empty string there, but a generic one ("seems noisy") is exactly the
   kind of proposal this pipeline's whole design exists to prevent. Use the
   real count and reason the candidate just said yes to.

## What you may propose

Exactly one of:
- **`title_search`**: add one term.
- **`title_exclusion_search`**: add one term, with a `--sunset` date
  (default: 90 days out) — nobody removes these by hand, so every one
  needs an expiry from the start.
- **`tier_limits`**: adjust one tier's daily record allocation, within
  ±25% of its current value and never below the record floor.

Never propose more than one field/tier change per cycle — `--propose` will
reject a second one mechanically, but don't even try; if you have two good
ideas, propose the stronger one and wait for the next cycle.

## What you may never propose

- Anything touching `profile.yaml` — `work_mode_priority`/`role_priorities`
  are the candidate's own stated preferences, not query parameters.
- A change to the control tier (`.careeros/config.yaml`'s
  `tuning.control_tier`) — it exists specifically to stay untouched.
- `work_arrangement`, `location_search`, `endpoint`, or `time_range` for any
  tier — these define a tier's IDENTITY; changing one voids that tier's
  entire accumulated ledger history, silently, which is worse than not
  proposing anything.
- A narrowing that would push a tier toward high precision at the cost of
  volume, on the strength of a hit-rate argument alone. A tier with a low
  gate-keep rate can still be the source of the day's best job — this
  pipeline found that exact case in its own history (an onsite tier at 38%
  keep rate that produced the single best Apply-tier match that day). The
  guardrails cap how far a `tier_limits` cut can go per cycle; that does
  not mean cutting is the right call just because it's allowed.

## After `--propose` and `--apply`

`careeros tune --apply` re-validates against current state before writing
anything — if the ledger moved since you proposed, it will refuse rather
than apply a now-stale change. That's expected, not a bug; re-run
`--status` and reconsider rather than forcing it through.

The auto-revert mechanism (`careeros/pipeline/tuning.py`'s `check_revert`)
watches the next 10 runs after any applied change. You do not need to do
anything for this to work, but you should not be surprised if a change you
made gets reverted automatically — it means the data said it made things
worse, compared against the untouched control tier over the same window,
not just compared against yesterday. A reverted change goes to
`.careeros/tuning/quarantine.json` and needs a fresh, deliberate
`--propose` to come back; it is never silently re-applied.
