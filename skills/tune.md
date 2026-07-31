# /careeros tune

The v2.0 query-tuning loop's host-CLI workflow.

**Normally you don't need to run this manually at all.** `skills/daily.md`'s
Step 0.5 already calls `careeros tune --check-due` automatically, every day,
before any job scoring happens — it's cheap, reads only last month's
already-written ledger (never today's jobs), and stays silent unless it
finds a real pattern worth asking the candidate about. This file is for
checking in on demand between daily runs, or for the manual propose/apply/
revert steps once the candidate has actually said yes to something.

**One rule stays absolute regardless of when this runs:** the agent turn
that scores today's jobs (Gate/Evaluate) must never be the same turn that
decides on a query change. A tuning decision is derived only from last
month's ledger, never from re-reading today's job descriptions (see
`AGENT_GUIDE.md`'s query-tuning section).

**Dormant by design.** Every step below refuses to act until a tier clears
~4 weeks / 400 records / 8 apply-or-consider events. If nothing is armed yet,
that's the correct, expected state — not a failure to fix.

## Step 1 — Check (automatic in daily, or run it yourself anytime)

```
careeros tune --check-due
```

If it's been less than a week since the last check, this is a fast no-op.
Otherwise it refreshes the ledger (`.careeros/learning/{ledger.md,
ledger.jsonl}`, from recent runs' `07_select/outcomes.json` +
`.careeros/processed.jsonl`, quarantined dates excluded) and writes
`.careeros/tuning/pending_flags.json` if anything is worth asking about.
Zero AI cost either way — this is pure aggregation of what the
deterministic pipeline already wrote.

## Step 2 — If something was flagged, ask the candidate

Read `prompts/tune_v1.md` in full before saying anything to the candidate —
it governs this step. In short: for each entry in `pending_flags.json`, look
at the real rejected job titles behind it (the ledger deliberately doesn't
store titles — open `05_gate/gated.json`/`04_constraints/rejected.json` for
that tier's recent dates yourself), then ask ONE plain-language yes/no
question with real numbers, no jargon, no file paths, no tier names.

If they say no, stop — do nothing further this cycle. If nothing was
flagged in Step 1, there's nothing to ask and nothing to say; continue
silently.

## Step 3 — Only after the candidate says yes: Propose

```
careeros tune --propose --tier <tier> --field <title_search|title_exclusion_search|tier_limits> \
  --value <value> --evidence "<what you actually saw>" [--sunset YYYY-MM-DD]
```

Mechanically validated against every guardrail (arming, one-change-per-cycle,
control-tier protection, exclusion cap + sunset requirement, tier-limit delta
cap and record floor — see `careeros/pipeline/tuning.py`). A rejection prints
exactly which guardrail fired; that is not something to work around, only to
report back to the candidate if it seems wrong.

## Step 4 — Apply

```
careeros tune --apply
```

Re-validates against current state (in case the ledger moved since Step 3)
before writing to `.careeros/tuning/overlay.yaml` and logging the change to
`.careeros/tuning/changes.jsonl`. Never touches `.careeros/config.yaml`
itself.

## Auto-revert (no action needed, but expect it)

Over the next `tuning.revert_window_runs` (default 10) completed runs,
`careeros/pipeline/tuning.py`'s `check_revert` compares the changed tier
against its own baseline AND against the fixed control tier
(`tuning.control_tier`) over the same window — a diff-in-differences, so a
market-wide swing doesn't get blamed on the one tier that happened to change.
If it fires, the overlay is restored automatically and the change is
quarantined (`.careeros/tuning/quarantine.json`); it will never be silently
re-applied. Report a revert to the candidate the same way you'd report any
other pipeline event — plainly, with what changed and why.

## Manual revert

```
careeros tune --revert
```

Reverts the most recent active (non-reverted) change immediately, without
waiting for the automatic window. Use this if a change is obviously wrong
before the data has had time to say so.
