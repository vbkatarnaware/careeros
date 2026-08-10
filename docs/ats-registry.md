# ATS discovery: reference registry, watchlist, and Layer 2A

> This doc replaces a v1.8 registry design (hand-written greenhouse/lever/
> ashby scrapers + a SQLite board registry) removed on 2026-08-10 — every
> seeded board was permanently `status='unverified'` on this project's own
> checkout, and those three providers only ever read `status='live'`
> boards, so they were structurally skip-only and had never returned a
> single job. Nothing below reuses that code; `ats-scrapers`' own 50+
> maintained adapters replace it.

## Two registries, two different questions

**Reference registry** (`careeros/registry.py`, optional) — "do we already
know this company, and which ATS is it on?" A local cache of the public
`ats-scrapers` company/tenant directory (~80K rows, `ats, name, slug, url`
only — no country or domain column, so it's a name/slug lookup, not a
filterable index). Nothing in `careeros discover` reads this; it exists
purely for `careeros registry sync` / `careeros registry find <name>`.

**Target watchlist** (`.careeros/watchlist.yaml`, see
`templates/watchlist.example.yaml`) — the small, hand-curated list of
companies actually scraped live. This is Layer 2A: targeted, not
automated discovery. See `careeros/providers/ats_watchlist.py`'s module
docstring for the full design, the adapter-contract details, and why
generic automated company discovery (Layer 2B) is deliberately not built.

## Commands

- `careeros registry sync` / `find <name>` / `stats` — the reference cache.
- `careeros watchlist list` — the watchlist's current scrape status
  (`.careeros/watchlist_state.json`, updated as a side effect of
  `careeros discover` when the `ats-watchlist` provider is enabled).

## What this doesn't do

No automated company discovery, no browser automation, no AI-proposed ATS
mappings — see `ats_watchlist.py`'s docstring for the measured evidence
behind that scope line.

## Investigated 2026-08-10: 11 companies missing from the dataset

The `ats-dataset` hosted snapshot doesn't carry Zerodha, Swiggy, Nykaa, Zoho,
Blinkit, Urban Company, Policybazaar, Chargebee, Pristyn Care, Wakefit, or
Khatabook. Each was probed against 11 ATS platforms and validated against
real job records (not just HTTP 200 — see the false-positive note below).

**Resolved: 1 of 11.**

| Company | ATS | Watchlist entry | Status |
|---|---|---|---|
| Swiggy | SmartRecruiters | `{ats: smartrecruiters, slug: swiggy}` | **19 real jobs**, confirmed absent from the dataset's 243,308-row smartrecruiters slice — a genuine tenant-level gap within an *enabled* platform |

Add it with:
```yaml
companies:
  - name: "Swiggy"
    ats: "smartrecruiters"
    slug: "swiggy"
```
(Its Product roles were Bengaluru-only as of this investigation — check current
listings before assuming MMR relevance.)

**Unreachable: 10 of 11.** Zerodha, Nykaa, Zoho, Blinkit, Policybazaar,
Chargebee, Khatabook have no detectable ATS via any of the 11 probed
platforms. Three — **Urban Company, Pristyn Care, Wakefit** — are confirmed
genuine Darwinbox tenants (see the discriminator below) but **Darwinbox has no
scraper in `ats-scrapers`**, and browser-based extraction was tested and
closed (next section). The remaining seven show no signal at all against any
probed platform; likely custom ATS or a platform not in this project's probe
set.

### A validated Darwinbox tenant discriminator (deterministic, no AI)

`GET https://{slug}.darwinbox.in/ms/candidate/careers` — a genuine tenant
returns ~890 bytes with a real `<title>` and an S3 tenant-logo URL; a
non-customer returns a **generic 561-byte shell**, byte-identical regardless
of slug (verified against the negative control `definitelynotarealcompany123`
→ 561 B). Useful for confirming/ruling out a Darwinbox hypothesis before
adding a watchlist entry that can never resolve.

**Caution — plain HTTP 200 is not validation.** An earlier naive probe of
these same 11 companies against `zohorecruit`/`darwinbox`/etc. URL patterns
"resolved" 11/11 — every `zohorecruit` hit was actually a Zoho *login page*
(identical 132,854 bytes across all 11 slugs), and every `darwinbox` hit not
using the discriminator above was silently a generic shell. Always require a
real job-record count (not merely HTTP 200) before trusting a probe.

### Playwright/browser extraction — CLOSED, do not reopen without one of the conditions below

Tested with the project's existing Playwright install (no new dependency)
against confirmed-real Darwinbox tenants with known open roles
(`atherenergy`, `bigbasket`). Findings:

1. **The rendered DOM and a plain `httpx.get()` return identical bytes** — the
   data lives in static `og:title` meta tags, not client-side JS. Playwright
   is ~15× slower (3.8 s vs. 0.25 s) for no additional data. If this is ever
   revisited, it must not use a browser.
2. **The real blocker is enumeration, and it's circular.** Every Darwinbox
   *listing* page (`/ms/candidate/careers`) renders an empty shell — zero
   `job`-related text, zero JSON/XHR calls, on every tenant tested. Individual
   job pages exist at `{tenant}.darwinbox.in/ms/candidate/careers/{opaque-id}`,
   but **the only known source of those IDs is the `ats-scrapers` dataset
   itself** — which, for a Darwinbox tenant, means the dataset would need to
   already have it. There is no independent enumeration path.
3. Even a resolved job page yields title/company/location only — no
   `description`, no `posted_at` — so `row_is_fresh()` would drop it and the
   AI Gate would receive nothing to reason about.

**Reopen only if:**
- `ats-scrapers` ships a Darwinbox or Keka adapter, **or**
- Darwinbox exposes a genuine listing/enumeration endpoint, **or**
- a new data source supplies job IDs for these tenants that we don't already
  have (which would make step 2 above moot).

None of these were true as of this investigation.

### Reopened 2026-08-10: a real listing endpoint exists after all

The `/ms/candidate/careers` empty-shell finding above is still correct — but
it's the wrong endpoint. `ats-scrapers`' `main` branch (unreleased, not in
any published version — see below) added a `darwinbox.py` scraper that uses
`POST /ms/candidateapi/job/alljobs`, a real paginated JSON listing API,
independent of any pre-known job ID. This satisfies the second reopen
condition above.

That upstream scraper itself is **not installed** — `ats-scrapers` 0.2.0 is
still the latest published release (confirmed via PyPI), and `darwinbox.py`
exists only past `## [Unreleased]` in the repo's own `CHANGELOG.md`, with no
version bump even on `main`. It also hardcodes `fetch_engine="cloak"`
(the library's Cloudflare-bypass transport), which would need a new
`httpcloak` dependency this project doesn't otherwise need.

Live-tested the real API contract directly with plain `httpx` against the 10
Darwinbox tenants from the earlier Layer 2A watchlist experiment
(curefit, rapido, purplle, mpl, pinelabs, juspay, classplus, stanzaliving,
bewakoof, rupeek) — every one returned HTTP 200, no Cloudflare block
encountered. Real job data: 11 jobs total across 4 tenants, only 1
Product-titled (MPL's "Associate Product Manager – GG3", `location: India`
with no city, `is_remote: false` — not currently MMR-eligible under this
profile's strict city matching).

Given the contract works over plain httpx with zero new dependencies,
`careeros/providers/darwinbox.py` ships a small bespoke replica of the
verified request/response shape (title/location/description/posted_at/
country mapping), wired into `ats_watchlist._scrape_entry` for
`ats: darwinbox` entries. Not a dependency on the unreleased upstream code —
if the real API contract ever changes, this fails the same way any other
adapter does (`ScraperError`), not silently.

## `global_remote` does not mean "confirmed India-eligible"

Measured 2026-08-10 across 517 real jobs tagged `global_remote` by
`careeros/providers/ats_dataset.py`'s `matched_geo_tier`:

| Bucket | Count | % |
|---|---:|---:|
| Ambiguous — no geographic signal at all | 387 | 75% |
| Explicitly India or worldwide-open | 91 | 18% |
| Explicitly excludes a specific country ("must be authorized to work in the US") | 38 | 7% |

`global_remote` means **"remote, with no detected geographic restriction"** —
nothing more. It is not evidence the role is actually open to a candidate in
India; it's the absence of evidence that it isn't. Two things act on this
distinction, deliberately at different layers:

- **`careeros/pipeline/constraints.py`'s `_work_authorization_excludes`**
  deterministically rejects the unambiguous 7% before they reach the AI Gate
  or (worse) get a full Apply-tier resume/cover generated for a role that was
  never reachable. It uses narrow, explicit phrase matching only — no
  timezone or region-name heuristics, which were tested in this same
  measurement and produced false positives on genuinely-open roles.
- **The AI Gate** (`prompts/gate_v1.md`) reads the full profile, including
  geography, and is what actually reasons about the remaining 75% ambiguous
  cases. That's intentional — the ambiguous bucket is real work `constraints.py`
  cannot safely automate without either rejecting good roles or accepting bad
  ones, and the Gate already does this reasoning for every job regardless, so
  a second, separate AI pass over just this bucket would be pure duplicate
  cost. See `matched_geo_tier`'s own docstring for the same figures.

## Architectural principle for any future agentic discovery work

CareerOS is an agentic terminal tool (run by Claude Code / Codex / similar),
so future company/ATS discovery work MAY use AI-agent investigation — but only
where deterministic methods have measurably failed AND the incremental recall
justifies the added complexity/cost (see the closed POC above for what "measure
first" looks like in practice: the naive probe's 11/11 false-positive rate is
itself the argument for what comes next). Deterministic code stays the default
for anything repeatable; AI is for ambiguity, investigation, verification, and
recovery — never a blind pass over every company. The non-negotiable shape,
if this is ever built:

```
AI proposes/investigates → deterministic validation (real job records,
not HTTP 200) → persisted mapping/state
```

AI must never be allowed to write a trusted `(company, ats, slug)` mapping
without a deterministic check against real returned job data — this is not a
style preference, it's what the false-positive discriminator above found in
practice. **Not implemented** — this section exists so the next attempt starts
from where this one stopped, not from zero.
