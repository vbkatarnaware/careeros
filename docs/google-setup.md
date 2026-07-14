# Google setup — step by step

CareerOS can optionally write your daily results into a **Google Sheet** and
back artifacts up to **Google Drive** — both off by default. Skip this file
entirely if you'd rather stay local: every `daily` run already writes a
readable digest to `.careeros/results/latest/summary.md`, linking straight to
each job's rendered resume/cover PDF, no Google account needed. Come back to
this whenever you want a shareable tracker instead. This is the one part of
setup that isn't a single command, so here is the whole thing, click by
click. No prior Google Cloud experience needed — just follow along.

You do this **once**. After that, `careeros daily` runs on its own.

---

## Part 1 — Google Sheets (optional)

CareerOS talks to your Sheet through a **service account** — a robot Google
account that belongs to a project you own. You create it, download its key
file, and then *share your Sheet with the robot's email* so it's allowed to
write. That last step is the one people miss — don't skip it.

### 1. Create a Google Cloud project
1. Go to <https://console.cloud.google.com/>.
2. Top bar → project dropdown → **New Project**. Name it anything (e.g.
   `careeros`). Click **Create**, then make sure it's the selected project.

### 2. Turn on the two APIs
1. Go to **APIs & Services → Library**.
2. Search **Google Sheets API** → open it → **Enable**.
3. Search **Google Drive API** → open it → **Enable**. (Sheets needs Drive's
   API to open spreadsheets by ID, even if you never use Drive backup.)

### 3. Create the service account + download its key
1. Go to **APIs & Services → Credentials**.
2. **Create Credentials → Service account**. Give it a name (e.g.
   `careeros-writer`) → **Create and continue** → skip the optional roles →
   **Done**.
3. Click the service account you just made → **Keys** tab → **Add key →
   Create new key → JSON → Create**. A `.json` file downloads. **This file is
   a password — keep it private, never commit it.**
4. Open that JSON in a text editor and find the **`client_email`** field. It
   looks like `careeros-writer@your-project.iam.gserviceaccount.com`. **Copy
   it — you need it in step 5.**

### 4. Point CareerOS at the key file
In `.careeros/config.yaml`, under `sheets:`, set the path to where you saved
the JSON:
```yaml
sheets:
  enabled: true
  credentials_path: "/full/path/to/careeros-writer-key.json"
  spreadsheet_id: null   # filled in the next step
  worksheet: "Jobs"
```
Tip: keep the JSON **outside** the repo folder (e.g. `~/Credentials/`) so it
can never be committed by accident. (`.careeros/` and `*credentials*.json`
are already gitignored, but outside-the-repo is safest.)

### 5. Create the Sheet and SHARE it with the robot (the step people miss)
1. Go to <https://sheets.google.com/> → blank spreadsheet. Name it anything.
2. Look at the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_LONG_ID`**`/edit`.
   Copy the long id between `/d/` and `/edit`.
3. Put it in `.careeros/config.yaml`:
   ```yaml
   sheets:
     spreadsheet_id: "THIS_LONG_ID"
   ```
4. In the Sheet, click **Share** (top-right) → paste the service account's
   **`client_email`** from step 3 → give it **Editor** → **Send**. (You can
   untick "notify" — it's a robot.)

> **If you skip the Share step**, CareerOS will fail with a permission error
> even though everything else is correct. The robot can only write to sheets
> you've explicitly shared with it.

You don't need to add headers — CareerOS creates the `Jobs` worksheet and its
column headers automatically on the first run.

### 6. Check it
```
careeros config        # prints your resolved config, including sheets: values
careeros daily         # run inside your host coding CLI
```
If Sheets is misconfigured you'll get a clear message naming exactly what's
missing.

---

## Part 2 — Google Drive backup (optional, off by default)

Skip this unless you want every Apply-tier job's Resume and Cover Letter (as
**PDF**), Evaluation, and Deep Report saved to one Drive folder automatically
— no per-company or per-job subfolders, just
`Company - Role - Resume.pdf` sitting directly in the folder you choose.

Drive uses a **different** credential from Sheets — an **OAuth "Desktop app"**
client (not a service account), because files land in *your own* Drive, owned
by you.

1. Install the extra deps: `pip install -e ".[drive]"` — this alone
   installs both the Google API/OAuth deps (required for any upload at all)
   and `typst` + `pypdf` for Resume/Cover Letter PDF rendering — one extra
   gets you everything, nothing else to install separately. `typst` bundles
   its own compiler binary (pure pip install, no LaTeX/pango/browser system
   dependency) and renders locally at `careeros artifacts --finalize` time,
   so `resume.pdf` exists on disk whether or not Drive is even enabled. If
   PDF rendering is ever unavailable anyway, Drive backup still works, it
   just uploads Resume/Cover Letter as Markdown instead and prints a
   warning — run `careeros doctor` to catch this proactively.
2. Google Cloud Console (same project) → **APIs & Services → Credentials →
   Create Credentials → OAuth client ID**. If prompted, configure the consent
   screen (User type: **External**, add yourself as a **Test user**).
   Application type: **Desktop app**. Create → **Download JSON**.
3. Make (or reuse) the Drive folder you want everything saved into (in your
   own Drive), open it, and copy the id from its URL
   (`https://drive.google.com/drive/folders/`**`FOLDER_ID`**).
4. In `.careeros/config.yaml`:
   ```yaml
   drive:
     enabled: true
     client_secret_path: "/full/path/to/oauth-desktop-client.json"
     root_folder_id: "FOLDER_ID"
     token_path: ".careeros/drive_token.json"   # auto-created; gitignored
     date_subfolder: false   # true = group each day's uploads under a YYYY-MM-DD/ subfolder
   ```
5. The **first** run opens a browser once to approve access; after that a
   saved token (`drive_token.json`) makes every later run silent.

Any Drive problem (auth, network, quota) only prints a warning — it never
blocks discovery, evaluation, or the Sheet. Drive is purely additive backup.
Re-uploading the same job (a re-run of `daily`, or `backfill-drive`) updates
its existing files in place rather than duplicating them.

**Already have Apply-tier jobs in your Sheet from before Drive was on?** Run
`careeros backfill-drive` (defaults to a dry-run preview; add `--no-dry-run`
to actually upload and add the clickable links to those existing rows).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PermissionError` / 403 writing the Sheet | Sheet not shared with the service account | Part 1, step 5 — share with the `client_email`, Editor |
| `sheets append` says "disabled" and does nothing | `sheets.enabled: false` (the default) | Set `sheets.enabled: true`, Part 1, steps 4–5 |
| `Sheets not configured` | `sheets.enabled: true` but `spreadsheet_id` or `credentials_path` missing | Part 1, steps 4–5 |
| `SpreadsheetNotFound` | wrong `spreadsheet_id` | Re-copy the id from the Sheet URL (between `/d/` and `/edit`) |
| Drive: `needs the optional [drive] extra` | extra not installed | `pip install -e ".[drive]"` |
| Drive: browser consent every run | token not being saved | check `drive.token_path` is writable and gitignored |
| Resume/Cover uploaded as `.md` instead of `.pdf` | `typst`/`pypdf` not installed (should ship with `[drive]` — v1.4.0+) | `pip install -e ".[drive]"`; `careeros doctor` (with `drive.enabled: true`) flags this proactively as "Resume PDF rendering (Typst)" |
| Old Apply-tier Sheet rows have no Drive links | they predate Drive being enabled | `careeros backfill-drive --no-dry-run` |
