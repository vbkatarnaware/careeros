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
`templates/watchlist.example.yaml`) — the list of companies actually
scraped live, either hand-added (`watchlist add`) or auto-discovered
(`watchlist discover`, see "Automated discovery (v2.2)" below). This is
Layer 2A: the PROVIDER itself (`fetch()`) stays targeted — it only ever
scrapes what's already in `watchlist.yaml`, never crawls on its own. See
`careeros/providers/ats_watchlist.py`'s module docstring for the full
design and the adapter-contract details.

## Commands

- `careeros registry sync` / `find <name>` / `stats` — the reference cache.
- `careeros watchlist list` — the watchlist's current scrape status
  (`.careeros/watchlist_state.json`, updated as a side effect of
  `careeros discover` when the `ats-watchlist` provider is enabled).

## What this doesn't do

No browser automation, no AI-proposed `(ats, slug)` mapping trusted without
a deterministic check against real job data. Automated, profile-driven
company discovery (`careeros watchlist discover`) DOES exist as of v2.2 —
see "Automated discovery (v2.2)" below and `ats_watchlist.py`'s docstring;
the provider that actually scrapes `watchlist.yaml` (`fetch()`) remains
targeted-only exactly as before, discovery is a separate, bounded seam on
top of it, not a change to the provider itself.

## ATS platform coverage audit (2026-08-11)

Layer 1 (`ats-dataset`) enables 35 of the 65 `by_ats` sources the upstream
dataset offers. Measured directly against a real Product-Manager-titled
search rather than ranked by registry company count, which turned out to be
the wrong signal in both directions:

**Correctly excluded — confirmed low-yield, not just omitted.** The six
largest excluded regional/non-English platforms by registry company count
(`join_com` — 23,547 companies, 29.5% of the entire 79,906-row registry
alone — plus `herp`, `hrmos`, `gupy`, `beisen`, `moka`) combined yield **252
PM titles and zero India rows**. `join_com` alone: 87 PM titles, 100%
German-speaking Europe. `eures` (government portal): 3.7GB / 8.4 min
download for 457 PM titles, zero India rows — cost with no reach. `adp` has
342 companies in the reference registry but **no loadable `by_ats` slice at
all** — unreachable dead weight, company-count presence notwithstanding.

**Added — measured gain, not obvious from company count.** Neither of these
looks meaningful by registry company count (both are ~zero/near-zero there,
since neither is a multi-tenant platform in the reference-registry sense);
both were found by measuring PM-title yield per slice directly instead:

| slice | PM titles | India rows | India PM | cost |
|---|---:|---:|---:|---|
| `welcometothejungle` | 1,274 | 195 | 3 | 189MB / 25s |
| `amazon` | 632 | 2,937 | 67 | 246MB / 9.5s |

`welcometothejungle` carries more PM titles than any enabled slice except
workday/greenhouse (mostly FR/US/GB) — a real gain for the global/remote
half of a mixed India+global search. `amazon` is a single-employer scraper,
normally excluded by the same-category rule as apple/google/meta/etc — but
its 67 India PM roles are a measured ~9% lift to this dataset's total India
PM reach (759 titles across the 33 platforms enabled before this change),
enough to justify it as an explicit exception rather than removing the
category rule itself. See `providers/ats_dataset.py`'s `_DEFAULT_SLICES`
comment block for the enable/exclude list this measurement produced.

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

## Zoho Recruit — an India-priority ATS gap, closed the same way as Darwinbox

Investigated 2026-08-11 as a dedicated research pass (no code changes until
the evidence was in): does a ready-made, reusable Zoho Recruit job scraper
already exist? **No.** GitHub/PyPI/npm search turned up one candidate
actually targeting it — `github.com/Himaanshuuuu04/Job_Scraper` (MIT,
last pushed 2025-09-21, 3 stars, 0 issues/PRs) — but it uses browser
automation against generic/unverified CSS selectors, which cannot work:
Zoho Recruit's v2 career sites are client-rendered SPAs whose initial HTML
carries **zero job data** (verified directly — a real tenant's `/jobs/
Careers` page is 1.7MB of CSS/JS bundles, no job content). Every other
Zoho-Recruit-adjacent OSS project found (`humantech/zoho-recruit-api`,
`@parthikhm/n8n-nodes-zoho-recruit`, `snovart/zoho_recruit`) is an
OAuth-authenticated client for a company managing its *own* Recruit
account, not a mechanism for discovering jobs at an arbitrary company —
excluded per the same rule this project already applies: an authenticated
customer API is never evidence that public scraping is supported. The two
commercial products found (JobsPipe, jobo.world) both confirm "scrape each
public career page" is the actual state of the art; neither publishes how.

**The real mechanism**, found by pulling and reading Zoho's own
career-site JavaScript bundle directly:

```
GET https://{tenant}.zohorecruit.{in,com}/recruit/v2/public/Job_Openings?pagename=Careers
```

No auth, no cookies — the exact request an anonymous visitor's browser
makes to render the page. Live-tested with plain `httpx` against 6
independent real tenants across both regional domains:

| tenant | domain | jobs | `Date_Opened`/`Job_Description` exposed? |
|---|---|---:|---|
| Hannah Solar | `.zohorecruit.com` (US) | 6 | No |
| BruntWork | `.zohorecruit.com` | 9 | No |
| OTSI Global | `.zohorecruit.com` | 27 | **Yes** |
| WorkBetterNow | `.zohorecruit.com` | 15 | **Yes** |
| Talenture | `.zohorecruit.com` (Nigeria ops) | 7 | No |
| APCER Life Sciences | `.zohorecruit.in` (**India**) | 2 | No |

Every tenant returned real, correctly-typed job data (title, city/state/
country, employment type, direct apply URL) with zero auth. **Measured
limitation, not a bug**: `Job_Description` and `Date_Opened` are
per-tenant career-site display settings, not always public — only 2 of 6
tenants exposed both. A tenant that doesn't expose `Date_Opened` gets
`posted_at: None` on every job, which `row_is_fresh()` correctly drops —
the same "no description, no posted_at → filtered out" outcome recorded
above for Darwinbox's own (worse) case, except here it's tenant-configurable
rather than universal. Title/location/company/apply-url/employment_type
stay reliable regardless; whether a specific tenant clears the freshness
filter is real per-tenant variance this module can't control.

Given the contract works over plain httpx with zero new dependencies (same
justification as Darwinbox), `careeros/providers/zoho_recruit.py` ships a
small bespoke client, wired into `ats_watchlist._scrape_entry` for `ats:
zoho_recruit` entries and into `ats_resolve.py`'s embedded-link supplement
(alongside the darwinbox pattern) so a company whose own careers page links
to a `*.zohorecruit.{in,com}` board resolves automatically. Only the
default career-site page name ("Careers") is tried — a tenant that renamed
its page is out of scope for now, same as any other unhandled edge case:
it fails the same way any other adapter does (`ScraperError`/
`CompanyNotFoundError`), never silently.

## Resolver recovery — three real bugs found by measuring, not assuming (2026-08-11)

A 96-company real-world Layer 2A run (a candidate list supplied for a Senior
PM / India+global profile) resolved only 15 and left 61 "unresolved". Before
building an AI research fallback for that gap — the original ask — every
failure class was reproduced against live endpoints first. Three deterministic
bugs, not a capability gap, explained most of it:

**The reference registry's own mapping was discarded.** `_resolve_via_registry`
already returns `(canonical_name, ats, slug, url)` — `watchlist discover` only
ever used `[0]`. When the careers-page resolver found no embedded ATS link
(common: `chargebee.com/careers`, `postman.com/careers`, `whatfix.com/careers`
all return HTTP 200 with zero ATS links in the raw HTML — client-rendered
SPAs), the candidate was marked UNRESOLVED without ever trying the mapping
already sitting in the synced registry. Verified live: 10/10 sampled registry
mappings scraped real jobs through the existing adapter (PhonePe 79 jobs, CRED
4, Meesho 52, Postman 109, Zepto 3, Slice 42, CloudSEK 24, Observe.AI 19,
MobiKwik 3, Whatfix 30). Fixed: the careers-page miss now falls through to the
registry mapping if one exists, validated through the exact same live-scrape +
quality-bar gate as any other candidate — never trusted on the registry's
say-so alone.

**Greenhouse's own embed-widget link was misread.** A careers page that only
links `boards.greenhouse.io/embed/job_board/js?for=cloudsek` (a real, working
board) had its tenant misread as `"embed"` — `resolve_careers_url`'s upstream
logic takes the first URL path segment, and `embed` is a valid-looking slug
that just happens to be wrong; the real one is in the `?for=` query param.
Both CloudSEK and Observe.AI resolved this way. Fixed in `ats_resolve.py`: the
`embed` case is now specifically corrected by reading `?for=`, falling back to
scanning the rest of the page if that param is absent.

**Darwinbox's own marketing subdomains were mistaken for a tenant board.**
`darwinbox.com`'s own `/careers` page links `explore.darwinbox.com`,
`blog.darwinbox.com`, and `academy.darwinbox.com` — none of them tenant
boards — before any real tenant link in document order, and the old regex
took the first `*.darwinbox.*` host match unconditionally. Verified every real
tenant board (LeadSquared, and Darwinbox's own actual `dbx.darwinbox.in`
tenant) has `/ms/candidate` in its path, while no marketing subdomain ever
does. Fixed: `_resolve_from_html` now requires that discriminator before
accepting a darwinbox match.

**A transient failure was recorded as a permanent, factual claim.** Perfios
resolved cleanly (`darwinbox/perfios.in`) on one live attempt and read-timed-out
minutes later on a second — `resolve_company_ats` returns `None` identically
for "fetched fine, no ATS link" and "every request failed at the network
level," and the old code wrote both as `"reason": "no detectable ATS"` with
the normal 30-day retry TTL. Fixed: `resolve_company_ats_or_fetch_failure`
(a new function alongside the unchanged `resolve_company_ats`, so every other
caller — `watchlist add`, ATS-change recovery — is unaffected) reports whether
at least one page was genuinely reached. A total network failure is now
recorded as `fetch_failed` with a 1-day retry TTL instead of the 30-day one.

**Re-running the identical 96 candidates after the fix, isolated scratch
environment, same synced registry, same profile:**

| | Added | Remaining unresolved/pending |
|---|---:|---:|
| Before | 15 | 81 |
| After | **34** | 62 (58 genuinely unresolved + 4 correctly `fetch_failed`, not poisoned for 30d) |

More than double the recovery, purely from fixing bugs in the existing
deterministic path — no AI, no new dependency, no loosened filter. Of the
62 still-unresolved records, 22 had a registry mapping that was tried and
genuinely failed validation (a real rejection, not a guess) and 23 had no
evidence at all — companies whose careers page is a JS SPA with no registry
entry, the residual gap an AI/web-research fallback (deferred, not built
this pass — see the plan) would actually need to close.

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

## Automated discovery (v2.2) — the principle below, implemented

CareerOS is an agentic terminal tool (run by Claude Code / Codex / Antigravity
/ Gemini CLI / similar). The shape this doc called for above is now built,
exactly as specified — AI proposes, deterministic code validates:

```
Agent proposes candidate names (profile preferences + host web search if
available, world knowledge otherwise — never required)
  -> careeros/ats_resolve.py: fetch the company's own careers page over
     plain httpx, look for an embedded ATS link (measurably better than
     URL-shape matching alone — resolve_careers_url() alone was 1/12
     against real missing-company pages; this technique resolved Fi Money,
     Sarvam AI, Clevertap, and Perfios in the same session, ~5/9)
  -> `careeros watchlist discover`: live-scrapes the resolved (ats, slug)
     via the SAME _scrape_entry seam `watchlist add` and `careeros discover`
     use, requires >=1 role-matching job posted within ~90 days (a
     freshness bar distinct from the 30-day job-eligibility window)
  -> only a company that clears ALL of the above is appended to
     watchlist.yaml — an agent-proposed name carries no more authority
     than one a human typed by hand into `watchlist add`
```

No browser, no search engine as a CareerOS dependency — `ats_resolve.py`
uses only `httpx` (already installed) and a regex over the fetched HTML,
reusing `ats_scrapers.resolve.resolve_careers_url`'s own host table. A host
agent's web search, if available, only widens which candidate NAMES get
proposed; it never substitutes for the deterministic careers-page/ATS/job
validation above, and its absence degrades discovery quietly (fewer/less-
current proposals), never into an error.

**Two failure modes this also had to solve, not just the happy path:**
- **ATS-change drift.** A company's own ATS mapping is treated as mutable
  state, not a one-time fact: after 3 consecutive not-found scrapes, an
  entry with a stored `website` is re-resolved (same `ats_resolve.py`) before
  being marked stale — a Lever→Ashby migration self-heals, appends one
  `history[]` record, and keeps scraping through the new adapter the same
  run. `Job.make_id` keys on provider id + company + title + location, not
  the ATS platform, so a migration produces byte-identical job ids and the
  existing dedupe suppresses re-surfacing with zero new code.
- **Currently-unsupported ATS.** A company whose ATS this install has no
  adapter for is never silently dropped — it's persisted in
  `discovery_candidates.json` as `pending_unsupported_ats` with its
  resolved ats/slug/website retained. Every later `watchlist discover` run
  cheaply rechecks (one local `ScraperRegistry.has_scraper` call, zero HTTP)
  and auto-promotes it the moment support exists, using the mapping already
  on file. Still unsupported after 30 days re-triggers a fresh careers-page
  resolve too, in case the company itself changed ATS.

AI is never allowed to write a trusted `(company, ats, slug)` mapping without
the deterministic check above — the same rule the closed Playwright POC
above established in practice, now enforced in code (`_passes_quality_bar`,
`_scrape_entry`), not just doc prose.
