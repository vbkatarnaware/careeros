# Changelog

All notable, user-visible changes to CareerOS are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/); versions
follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-07-10

### Added

- **Automatic Application Answers.** For every Apply-tier job, `daily` now
  drafts real answers to that job's actual application questions — no
  waiting until you've manually opened the form. A new background form-reader
  (`careeros/apply/browser.py`) fetches the form's visible text: a
  zero-dependency HTTP tier first, an optional headless-Playwright fallback
  only when the form genuinely needs JavaScript to render. Neither tier uses
  per-site scraping selectors — the agent identifies the real questions from
  plain text and drafts grounded answers the same "selector, not writer" way
  as the resume.
- **`careeros publish <job-id>`** — upload one job's current artifacts
  (resume, cover, evaluation, deep report, application answers — whichever
  exist) to Drive and patch just that Sheet row, without a full `daily` run.
  The `prep` and on-demand `apply <job-id>` skills now run this
  automatically as their last step.
- **A specific status for every unreadable form**, replacing one generic
  fallback: ✅ Generated, 🔒 Login Required, ❌ Closed, ⚙️ Playwright
  Missing, 📄 No Essay Questions, 🌐 Network Error, 🛡️ Bot-Blocked, or the
  generic Manual review required only when none of those match. Each is
  detected generically (login walls, closed-posting notices, Cloudflare-style
  bot challenges, and pages that render real text but never reach an actual
  form) — no per-ATS logic.
- **A `Status` column** in the Sheet (dropdown: `Not Applied` by default,
  `Applied`, `Received Call`, `Interview`, `After Interview`,
  `Ongoing / In Process`, `Offer`, `Rejected`) for tracking your own
  application progress by hand. The pipeline sets the default on a new row
  and never touches it again, exactly like `Notes`.
- **New Sheet columns**: `Evaluation (Drive)`, `Deep Report (Drive)`,
  `Application Answers (Drive)` — direct, per-job links to artifacts that
  were already being generated (and, for Evaluation, already uploaded) but
  had nowhere to show up.
- **Newest-on-top Sheet ordering.** New rows are now inserted directly below
  the header instead of appended at the bottom, so each day's run reads at
  the top without scrolling past a growing history.
- **`careeros sheets sync-status`** — patch the Application Answers status
  of existing Sheet rows after re-running `apply --prepare/--finalize`
  (e.g. reclassifying old jobs into the newer, more specific statuses)
  without appending a duplicate row.
- **Personal/logistics questions, asked once.** Notice period, work
  authorization/visa status, salary expectations, earliest start date, and
  employment type are the same answer on every application. The first time
  one is genuinely needed and missing, it's asked once and saved to
  `profile.yaml`'s new `logistics:` section — every later application,
  batch or on-demand, reuses it automatically. Voluntary EEO/demographic
  self-identification questions are deliberately excluded and always left
  for you to answer yourself.
- **`careeros --version`** — prints the installed version and exits.
- `careeros doctor` now checks Playwright independently at two levels: the
  `[apply]` extra's Python package, and the `chromium` browser binary
  (`playwright install chromium`) — "package installed but browser binary
  missing" and "package not installed at all" are reported as distinct,
  actionable messages instead of one opaque failure.

### Changed

- **Sheet columns removed**: `Resume Path`, `Cover Letter Path`,
  `Report Path` (local filesystem paths, useless outside your own machine)
  and `Drive Folder` (redundant once every artifact has its own direct
  link). `careeros sheets migrate` / `sheets append` remove these
  automatically from an existing Sheet — see Migration notes below.
- CI now installs the optional `[apply]` extra and runs
  `playwright install chromium --with-deps`, so the real headless-browser
  fetch path runs in automated tests instead of being skipped.
- The package version now has a single source of truth
  (`pyproject.toml`); `careeros.__version__` reads it back via
  `importlib.metadata` instead of duplicating the string.

### Fixed

- The Playwright fallback used `wait_until="networkidle"`, which never
  fires on pages with persistent background network activity (analytics
  beacons, a bot-check's own verification polling) — it could hard-timeout
  with zero text captured even though the real content rendered within a
  second or two. Switched to `wait_until="load"` plus a short fixed buffer.
- PDF rendering crashed instead of falling back to Markdown when resume,
  cover, or answers content contained a non-Latin currency symbol (₹, €, £,
  ¥) — blocking the entire Drive upload for that job. Now sanitized to a
  latin-1-safe equivalent, and any other still-unmappable character falls
  back to Markdown instead of failing the upload.
- `careeros sheets sync-status` could overwrite a just-published
  Application Answers link with a blank cell, because the local
  `drive_links.json` cache isn't refreshed by `careeros publish`. It now
  never touches a `generated`-status job's cell — only `publish` does.
- A login-wall, closed-posting, or bot-blocked page returns real,
  substantial (non-empty) text — it's just the wrong page. The batch apply
  stage now checks for these BEFORE treating any non-empty fetch as a
  readable form, so a LinkedIn login-wall page can no longer be silently
  sent to the drafting step as if it were the real application form.

## Migration notes: upgrading an existing Sheet

If you were running CareerOS before this release, your Google Sheet needs
one one-time cleanup pass:

```
careeros sheets migrate
```

This removes the four deprecated columns, adds the four new ones, applies
header/Score/Status formatting, backfills `Status` to `Not Applied` on
existing rows, and sorts your Sheet's existing rows by Date descending (a
one-time fix for history that was written bottom-up before this release).
It's safe to re-run — every step is idempotent, and a Sheet already on the
current schema is a no-op. After this one pass, every future `daily` run
keeps the Sheet current automatically; you never need to run it again
unless you skip several releases at once.

## [1.0.0] - 2026-07-09

Initial public release: profile-driven segmented discovery through the
Fantastic Jobs REST API (with a legacy Apify-actor provider available),
deterministic normalize/dedupe/constraints/two-tier threshold, the AI Gate
and Evaluate stages, resume/cover generation, a zero-cost daily report and
summary, optional Google Drive backup, and Google Sheets output.
