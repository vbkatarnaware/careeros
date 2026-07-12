# Providers

A provider discovers jobs from one source and hands them to the pipeline in
a common shape. The pipeline never imports a provider directly — it goes
through `registry.get(name)` — so adding a new one never touches
`pipeline/`, `cli.py`, or any other stage.

## v1.2: many providers can run side by side

`.careeros/config.yaml`'s `providers:` block is the ONE model for which
sources are active — a dict keyed by provider id, each with at least
`{enabled: bool}`, run in the exact order they're listed (Python/YAML
preserve mapping order). `discover` runs every `enabled: true` entry and
merges their raw items; `normalize` maps each provider's own items with its
own `to_job_dict()` and concatenates every provider's jobs into ONE flat
list. Order matters: `pipeline/dedupe.py` keeps the FIRST occurrence of a
duplicate role, so list your primary/most-trusted source first.

`providers:` controls *which* sources run. It is NOT where a source's
detailed settings live — those stay in that provider's own config (Fantastic
Jobs' is the separate `api:` block, unmoved, mature and frozen; every other
provider's detailed settings live inline in its own `providers:` entry, e.g.
`providers.naukri.location`).

`--provider NAME` on `discover` forces exactly ONE provider, ignoring
`providers:` entirely — this is the manual dry-run/trial workflow (see
"Verify live before trusting a new provider" below), not how `daily` runs
day to day.

## The provider lifecycle

```
register (registry.py)
      │
      ▼
validate(config) ──problems──▶ doctor surfaces them / discover marks the
      │ []                      provider "skipped" and moves on to the rest
      ▼
fetch(config, **kwargs) ──▶ ProviderResult{ items, cost_usd, requests,
      │                                      records, seconds, warnings,
      │                                      errors, skipped, skip_reason }
      │ .items
      ▼
to_job_dict(raw) ──▶ common job shape ──▶ normalize ──▶ merged jobs.json
                                                          ──▶ [pipeline]
```

Budget/quota enforcement in `discover` is CAPABILITY-driven, never a check
on a provider's name (`budget.guard_for` inspects which KEYS are present in
the provider's own resolved config — a `"plan"` key means Fantastic Jobs'
existing weekly-record-quota guard; a `"max_monthly_budget_usd"` key means
the Apify-actor rolling-month soft-budget guard; neither means unmetered,
free). A provider that's enabled but can't run a given call (failed
`validate()`, or its guard says stop) comes back as a `ProviderResult` with
`skipped=True` and a `skip_reason` — reported, never silently dropped — and
`discover` continues with whatever else is enabled.

## Shipped providers

Every provider below is classified **Core** (enabled by default, zero
setup), **Optional** (disabled by default, opt in deliberately), **Experimental**
(works, but has a real caveat worth knowing before you enable it), or **Not
Recommended** (built and tested, evidence says leave it off) — see "Cost:
don't trust a `--limit 3` trial" and the per-provider evidence below for how
each verdict was reached. This is about the underlying job source, not the
plumbing that fetches it (three of the free/default sources hit a public
API/feed directly; the five optional/experimental/not-recommended ones
happen to run as Apify actors) — pick providers by source name, not by
fetch mechanism.

**Core — on by default, no signup required:**

**Fantastic Jobs — two providers, one dataset** (see the P2.6/P2.7
architecture review for the full reasoning):

- **`fantastic-jobs`** (`fantastic_jobs.py`) — the official REST API.
  **Default and actively maintained.** Supports two transports via
  `config.api.transport` (no default — you must choose): `"direct"`
  (developer.fantastic.jobs) or `"rapidapi"` (RapidAPI's "Active Jobs DB").
  Both proxy the identical dataset and differ only in base URL + auth
  header; which is cheaper for your volume is a config/commercial decision,
  not an architectural one. Also queries **both** upstream endpoints by
  default via `config.api.endpoint: "both"` — `active-ats` (career
  sites/ATS, 54 platforms including Workday/Greenhouse/Ashby/Lever) and
  `active-jb` (+LinkedIn/YC/Wellfound), merged, with the per-tier record
  allocation split 50/50 (not doubled). This is the P2.8 Final Discovery
  Acceptance Audit's frozen default — see
  `.careeros/qa/acceptance_audit_report.md` for the evidence (full 107-job
  population: both sources score an equal ~8% ≥4.0 rate but are 92%
  disjoint, so "both" roughly doubles interview-worthy jobs at the same
  quota cost). Free, no per-job cost — subscription/credit-metered.
- **`fantastic-jobs-actor`** (`legacy/fantastic_jobs_actor.py`) — the Apify
  actor. **Legacy/reference**, kept for no-code/Zapier/n8n/MCP-style setups.
  Not the actively maintained path; new discovery features land in the REST
  provider only. Shares `to_job_dict()` verbatim with `fantastic-jobs`
  (enforced by `test_provider_fantastic_jobs_parity.py`).

- **`remoteok`** (`remoteok.py`) — RemoteOK's free public JSON API
  (`remoteok.com/api`), no signup, $0/job. Remote-only by definition
  (`remote: true` on every job).
- **`we-work-remotely`** (`we_work_remotely.py`) — We Work Remotely's free
  public RSS feed, no signup, $0/job, parsed with Python's stdlib XML tools
  (no new dependency). Remote-only by definition.

**Apify-actor sources (v1.2, off by default — see "Turning on a paid
provider" below).** All five share token auth and monthly budget with
`fantastic-jobs-actor` (one Apify account, one balance) via the shared
`apify:` config block, and their run mechanics (token-pool rotation,
per-call `max_total_charge_usd` cap, cost read-back) via
`_apify_actor_common.py`. Every one has been **live-verified** (real Apify
calls, real captured output, real cost) — first at small trial batches
(`--limit 3`), then again at production-realistic batch sizes (`limit
20-30`) during a 2026-07 evidence pass, which corrected several assumptions
the small trials got wrong (see "Cost: don't trust a `--limit 3` trial"
below). `to_job_dict()` still uses the defensive `pick_field`/candidate-key
pattern (below) rather than hardcoded field names, since Apify actor
schemas aren't contractually guaranteed to stay stable across updates.

**Token rotation (v1.3).** Set `APIFY_TOKENS` (comma-separated) instead of
a single `APIFY_TOKEN` to configure more than one Apify account/key — every
Apify-actor provider shares this same pool. Rotation to the next token on a
budget/consent failure is **silent** (no per-token "failed, trying next…"
noise) — it's expected, recoverable behavior when you've deliberately
configured more than one key, not something worth alarming you about. A
token that fails is cached by a short, non-reversible fingerprint (never the
raw key) in `.careeros/apify_tokens.json` — but only for the SAME day it
failed, so it's skipped outright on later calls *that day* instead of being
re-tried and re-earning the same rejection. Any other day (including a
same-token top-up mid-month), it gets one fresh live retry before being
trusted as exhausted again — never a silent skip for the rest of the
billing cycle from a single earlier failure. If every configured token is exhausted, the
whole provider call raises a single clear error naming the fix path (add a
fresh key to `APIFY_TOKENS`, raise your Apify plan's limit, or wait for
reset) — recommended for anyone running CareerOS regularly: either a paid
Apify plan with real headroom, or several free/lower-tier accounts' tokens
in the same `APIFY_TOKENS` pool.

### Optional — relevant, reasonably priced, worth enabling deliberately

- **`naukri`** (`naukri.py`) — `memo23/naukri-scraper`. **Relevance:**
  10/10 on-target Product Manager roles in the largest live sample tested.
  **Overlap:** low — India-focused, largely disjoint from Fantastic Jobs'
  ATS/LinkedIn coverage. **Reliability:** no failed runs observed across
  every live call this project has made. **Cost:** a flat per-run fee
  (~$0.0005-0.005 regardless of item count 1-20) — cost/job trends toward
  negligible at any real batch size. The strongest single recommendation of
  the five paid providers.
- **`glassdoor`** (`glassdoor.py`) — `memo23/glassdoor-scraper-ppr`. NOT
  the same actor as an earlier-evaluated `orgupdate/glassdoor-jobs-scraper`
  (~$0.20/job — deliberately avoided). **Relevance:** high, verified live
  at both n=3 and n=30 — consistently on-target PM/Associate-PM titles.
  **Overlap:** moderate — general job board. **Reliability:** succeeded on
  every live call this project has made, but runs are slow (50-250s) and
  its output shape is more deeply nested than the others; a real bug
  (relative, not absolute, apply URLs — see the module docstring) was
  found and fixed via a 30-item production-scale validation run, after
  passing every earlier 3-item trial silently. **Cost:** dominated by a
  near-fixed per-run overhead, not a per-item rate (see below) — use
  `limit >= 20-30`, not `3`, to judge its economics.
- **`ziprecruiter`** (`ziprecruiter.py`) —
  `crawlerbros/ziprecruiter-scraper-pro`. **Relevance:** the BEST of all
  five Apify providers tested — 5/5, 5/5, 15/15, and 30/30 on-target
  Product Manager roles across four independent live runs. **Overlap:**
  moderate — general job board. **Reliability:** the one real caveat —
  its documented ~63% actor run-success rate reproduced live once (a run
  returned 0 items for a small charge). This is a run-completion problem,
  not a data-quality one, and CareerOS's per-provider skip/continue
  behavior already absorbs a failed run without aborting the rest of a
  `discover` call — the downside of a failure is capped at that one run's
  small cost. **Cost:** same fixed-per-run-overhead story as Glassdoor.
  The original audit's ~$0.053/job figure came from a `--limit 3` trial and
  does not hold at real batch sizes — at `limit 30`, live cost was
  ~$0.004-0.006/job, comparable to Glassdoor, not meaningfully pricier.
  Ships off by default (a fresh clone has no Apify token, and the
  reliability caveat deserves a deliberate opt-in) but is not the
  cost/relevance liability the original small-sample estimate suggested.

### Experimental — works, but not well-suited to this project's default query

- **`indeed`** (`indeed.py`) — `valig/indeed-jobs-scraper`. **Relevance:**
  query-dependent. Genuinely good for a single-concept title (verified live
  with "Software Engineer" — Azure/Cloud/Sr. Developer roles, all on
  target), but poor (~10% hit rate at n=20) for CareerOS's own default
  search term, "Product Manager" — the actor broad-matches on the word
  "Manager" across Sales/Marketing/unrelated-domain roles. This is a
  genuine site-side relevance limitation for compound/ambiguous titles, not
  a broken integration or a CareerOS query bug (the differential behavior
  between the two search terms proves the actor does honor the query).
  **Overlap:** high — a general aggregator, likely the most overlap with
  Fantastic Jobs of any provider here. **Reliability:** no failures
  observed. **Cost:** negligible, flat ~$0.0001-0.001/run. Worth enabling
  only if you retarget `search_keyword` to a less ambiguous title than
  "Product Manager", or re-check after an actor update.

### Not Recommended

- **`foundit`** (`foundit.py`) — `shahidirfan/Foundit-Jobs-Scraper`
  (Foundit = Monster India, rebranded, same company). **Relevance:** poor
  — irrelevant across two independently tested, unrelated queries ("Product
  Manager" AND "Software Engineer"), which rules out a query-construction
  bug on CareerOS's side; the raw fixture's `keyword` field correctly
  echoes the sent query, confirming this is a genuine site/actor targeting
  defect. **Overlap:** low (India-focused) — attractive on overlap grounds
  alone, but not enough to offset the relevance problem. **Reliability:**
  runs complete without failing; they just return low-value data.
  **Cost:** negligible (~$0.001/run flat), but cheap-and-irrelevant doesn't
  save you anything net. Leave disabled; revisit only if the underlying
  actor's search quality changes.

### Cost: don't trust a `--limit 3` trial

Glassdoor and ZipRecruiter (both browser/proxy-driven actors) have a large,
near-fixed cost **per run**, not a cost that scales linearly with item
count — a 3-item trial pays almost the same fixed overhead as a 30-item
run, so its apparent $/job is wildly pessimistic. Naukri/Foundit/Indeed
(lightweight HTTP-style actors) show the opposite: an almost flat per-run
fee regardless of item count, so $/job is nearly meaningless for them —
just budget the flat run cost. **Always judge an Apify provider's real
economics from a `limit >= 20-30` run, never a `--limit 3` trial** — the
trial is for verifying output shape and relevance, not for estimating cost.

## The contract

Copy `fantastic_jobs.py` (REST-style reference) or one of the v1.2 Apify
providers, e.g. `naukri.py` (Apify-actor-style reference, using
`_apify_actor_common.py`'s shared `run_actor`/`validate_apify_token`). Every
provider — no exceptions, no special cases — implements exactly three
methods:

```python
from careeros.providers.base import ProviderResult

class MyProvider:
    id = "my-provider"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems, empty = OK. Pure — no network call;
        `doctor` and `discover` both call this on every run."""
        ...

    def fetch(self, config: Config, **kwargs) -> ProviderResult:
        """Call your source. Return a ProviderResult: items (raw records,
        untouched) plus cost_usd/requests/records/seconds/warnings/errors.
        A free source just leaves cost_usd at 0.0."""
        ...

    def to_job_dict(self, raw: dict) -> dict | None:
        """Map one raw record to {title, company, location, apply_url,
        description, remote, seniority, employment_type, posted_at, salary,
        contact, company_linkedin}. Return None to skip a record missing a
        title or a usable URL."""
        ...

PROVIDER = MyProvider()
```

Then register it in `registry.py`:

```python
from careeros.providers.my_provider import PROVIDER as MY_PROVIDER
_REGISTRY = {..., MY_PROVIDER.id: MY_PROVIDER}
```

And add its own block to `providers:` in your `config.yaml` (see any of the
v1.2 entries in `templates/config.example.yaml` for the shape — at minimum
`{enabled: false}`; add whatever config keys your own `fetch()` reads).

That's the whole integration: **one provider file, one registry line, one
config block, one test file — no downstream pipeline changes.**
`pipeline/normalize.py` calls `to_job_dict()` for every raw record and turns
the result into a `Job` (assigning `id`, deriving `ats` from the apply URL's
domain, truncating `description`); `dedupe`/`constraints`/`gate`/`evaluate`/
`threshold`/`artifacts`/`apply`/`drive`/`sheets` never know or care which
provider(s) supplied a job.

## Field names are never guaranteed

Every job-board/Apify actor names its fields slightly differently
(`company` vs `company_name` vs `employer`). Don't hardcode one name — use
`_apify_common.py`'s candidate-key lists (`pick_field(raw, COMPANY_KEYS)`)
the same way `fantastic_jobs.py` and every v1.2 provider does, and extend a
candidate list if your source uses a name that isn't covered yet.

## Errors: raise ProviderError, don't let the SDK crash raw

If your source has expected, actionable failure modes (missing/expired
credentials, an unset config choice, a paid API's budget exhausted), catch
them and raise `careeros.providers.base.ProviderError` with a message
telling the user what to do — the CLI catches this in `discover` (per
provider — one failing provider never aborts the rest of a multi-provider
run) and prints it cleanly instead of an unhandled traceback.
`fantastic_jobs.py` does this for a missing/invalid `config.api.transport`
and a missing API key; `_apify_actor_common.py`'s `run_actor` (shared by
every Apify-actor provider) does it for a missing/exhausted Apify token,
also rotating through a comma-separated token pool (`config.apify.tokens_env`)
before giving up, since a single paid-API token running out mid-`daily`
shouldn't be a hard stop if a spare is configured.

## Turning on a paid provider

Every Apify-actor provider ships `enabled: false`. To turn one on for real:

1. Trial it in isolation first (see "Verify live" below).
2. Set `enabled: true` in its `providers:` block in `.careeros/config.yaml`.
3. Optionally set its own `max_monthly_budget_usd` to override the shared
   `apify.max_monthly_budget_usd` account default.
4. `careeros doctor` shows every enabled provider's credential status and,
   for any Apify-actor one, its budget-vs-spend this month.

## Verify live before trusting a new provider

Apify actor output shapes are not contractually documented and can differ
from what you expect. Before wiring a provider into `daily`:

```
careeros discover --provider my-provider --dry-run --limit 3
```

This forces exactly that ONE provider (ignoring `providers:`) and prints a
preview of its raw items — inspect it directly. If a field you can see
there isn't showing up in the mapped `Job`, add its key name to the
relevant candidate list in `_apify_common.py`. (A non-dry-run
`--provider my-provider` run also writes the full `raw.json` to
`.careeros/runs/<date>/01_discover/raw.json` under that provider's own key,
if you want to inspect more than the 3-item preview.)

## Planned future providers

Greenhouse, Ashby, Lever, and Workday all expose fairly stable public JSON
board APIs (no Apify actor needed) — a direct-fetch provider for each is a
good first contribution, though note all three ATS platforms are already
covered indirectly via Fantastic Jobs' `active-ats` endpoint, so a dedicated
provider for one only adds value if it surfaces something that feed misses.
A generic "custom career site" provider (HTML scrape + heuristic field
extraction) is a larger, lower-priority effort.
