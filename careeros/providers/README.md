# Providers

A provider discovers jobs from one source and hands them to the pipeline in
a common shape. The pipeline never imports a provider directly — it goes
through `registry.get(name)` — so adding a new one never touches
`pipeline/`, `cli.py`, or any other stage.

## Many providers can run side by side

`.careeros/config.yaml`'s `providers:` block is the ONE model for which
sources are active — a dict keyed by provider id, each with at least
`{enabled: bool}`, run in the exact order they're listed (Python/YAML
preserve mapping order). `discover` runs every `enabled: true` entry and
merges their raw items; `normalize` maps each provider's own items with its
own `to_job_dict()` and concatenates every provider's jobs into ONE flat
list. Order matters: `pipeline/dedupe.py` keeps the FIRST occurrence of a
duplicate role, so list your primary/most-trusted source first.

**As of v1.7 only one provider ships enabled** (`fantastic-jobs`), but none
of the above changed — the machinery for running several at once is intact
and tested. See "Evaluated and removed" for why the tree is down to one.

`providers:` controls *which* sources run. It is NOT where a source's
detailed settings live — those stay in that provider's own config (Fantastic
Jobs' is the separate `api:` block; any other provider's detailed settings
live inline in its own `providers:` entry).

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
weekly-record-quota guard; a `"max_monthly_budget_usd"` key means the
rolling-month soft-spend guard; neither means unmetered/free). A provider
that's enabled but can't run a given call (failed `validate()`, or its guard
says stop) comes back as a `ProviderResult` with `skipped=True` and a
`skip_reason` — reported, never silently dropped — and `discover` continues
with whatever else is enabled.

No provider ships with the `"monthly"` capability today. It is kept because
it is keyed off a config *shape*, not a name: a future paid source gets
spend-capping without touching `discover`.

## The shipped provider

- **`fantastic-jobs`** (`fantastic_jobs.py`) — the official Fantastic Jobs
  REST API. Supports two transports via `config.api.transport` (no default —
  you must choose): `"direct"` (developer.fantastic.jobs) or `"rapidapi"`
  (RapidAPI's "Active Jobs DB"). Both proxy the identical dataset and differ
  only in base URL + auth header; which is cheaper for your volume is a
  config/commercial decision, not an architectural one. Also queries **both**
  upstream endpoints by default via `config.api.endpoint: "both"` —
  `active-ats` (career sites/ATS, 54 platforms including Workday/Greenhouse/
  Ashby/Lever) and `active-jb` (+LinkedIn/YC/Wellfound), merged, with the
  per-tier record allocation split 50/50 (not doubled). This is the P2.8
  Final Discovery Acceptance Audit's frozen default — see
  `.careeros/qa/acceptance_audit_report.md` for the evidence (full 107-job
  population: both sources score an equal ~8% ≥4.0 rate but are 92%
  disjoint, so "both" roughly doubles interview-worthy jobs at the same
  quota cost). Free per job — subscription/credit-metered, guarded by the
  weekly record quota (`careeros/budget.py`).

### How its daily limit is spent

`api.limit` is a **daily total** — jobs per day from this source, across
every search. `discover` divides it evenly across however many query tiers
your `profile.work_mode_priority` generates (see `pipeline/queryplan.py`),
then `fetch()` splits each tier's allocation 50/50 across the two endpoints.
Left null, the quota guard recommends `weekly_quota ÷ active_days_per_week`.

This was per-*search* before v1.7, which surprised people: a 3-tier profile
silently fetched 3× the number they configured. `api.tier_limits` still
overrides individual tiers explicitly — when you set those, your real daily
total is the sum of them.

## Evaluated and removed

Seven providers plus a legacy Apify actor shipped through v1.6 and were
removed in v1.7 after two weeks of live daily use. Recorded here so nobody
re-litigates them from scratch; all are recoverable from git history.

Evidence from the 2026-07-26 run (524 jobs discovered, 18 selected):

| Source | Raw | Gate pass | Avg score | Selected | Cost | Verdict |
|---|---|---|---|---|---|---|
| `fantastic-jobs` | 70 | 46 | 3.27 | **9** | $0 | **kept** |
| `glassdoor` | 100 | 21 | 3.37 | 6 | $0.73 | removed |
| `we-work-remotely` | 100 | 8 | 3.74 | 2 | $0 | removed |
| `ziprecruiter` | 154 | 72 | 2.52 | 1 | $1.12 | removed |
| `remoteok` | 100 | 2 | 3.60 | 0 | $0 | removed |

- **`ziprecruiter`** — worst value by a wide margin: 29% of raw volume, 60%
  of total spend, one selected job, lowest average score. Also **overshot
  its own `maxItems`** (154 returned against a limit of 100 — it passed the
  cap to the actor but never sliced client-side), and took 1,353s of the
  1,355s total discovery wall-clock. A 2026-07-13 run returned **0 records
  and still billed**. Its actor had a ~63% run-success rate.
- **`remoteok`** — free and fast, but 100 jobs in yielded 2 gate passes and
  0 selections. The 98 rejects still cost gate tokens.
- **`we-work-remotely`** — free, and the *highest* average score of any
  source, but only 8 gate passes from 100 raw. Removed on volume, not cost.
- **`glassdoor`** — converted acceptably (6 selected) but had the worst
  constraint-rejection rate (40 of 100 dropped on location/salary), and its
  actor exposed no per-call result-count field, so `limit` had to be applied
  as a client-side slice after paying for everything fetched.
- **`naukri`** — India-focused, relevant in testing, but superseded: an
  earlier run returned 50 records for $0.10 with heavy overlap against
  Fantastic Jobs' `active-ats` feed.
- **`indeed`** — consistently weak results for a "Product Manager"-style
  query specifically.
- **`foundit`** (= Monster India, rebranded) — irrelevant results across
  multiple search terms, at any limit.
- **`fantastic-jobs-actor`** — the same dataset as `fantastic-jobs`, reached
  via a paid Apify actor instead of the free REST API. Kept through v1.6 as
  a no-code reference; removed as pure redundancy.

**A cost lesson worth keeping:** browser/proxy-driven actors (Glassdoor,
ZipRecruiter) carry a large near-fixed cost **per run**, not per item — a
3-item trial pays almost the same overhead as a 30-item run, so its apparent
$/job is wildly pessimistic. Lightweight HTTP-style actors (Naukri, Foundit,
Indeed) show an almost flat per-run fee regardless of item count, making
$/job nearly meaningless. **Judge a metered provider's real economics from a
`limit >= 20-30` run, never a `--limit 3` trial** — the trial is for
verifying output shape and relevance, not cost.

## The contract

Copy `fantastic_jobs.py` — the reference implementation, and currently the
only one. Every provider — no exceptions, no special cases — implements
exactly three methods:

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

Every job board names its fields slightly differently (`company` vs
`company_name` vs `employer`). Don't hardcode one name — use
`_field_mapping.py`'s candidate-key lists (`pick_field(raw, COMPANY_KEYS)`)
the same way `fantastic_jobs.py` does, and extend a candidate list if your
source uses a name that isn't covered yet.

## Errors: raise ProviderError, don't let the SDK crash raw

If your source has expected, actionable failure modes (missing/expired
credentials, an unset config choice, a paid API's budget exhausted), catch
them and raise `careeros.providers.base.ProviderError` with a message
telling the user what to do — the CLI catches this in `discover` (per
provider — one failing provider never aborts the rest of a multi-provider
run) and prints it cleanly instead of an unhandled traceback.
`fantastic_jobs.py` does this for a missing/invalid `config.api.transport`
and a missing API key.

## Adding a provider

1. Write `careeros/providers/my_source.py` implementing the three-method
   contract above, ending with `PROVIDER = MyProvider()`.
2. Add one import + one `_REGISTRY` entry in `registry.py`.
3. Add its defaults to `DEFAULT_CONFIG["providers"]` in `careeros/config.py`.
   If it's a paid, metered source, include `max_monthly_budget_usd` — that
   key alone is what opts it into the rolling-month spend guard
   (`budget.guard_for`); no `discover` change is needed.
4. Trial it in isolation (see "Verify live" below) before enabling it.
5. `careeros doctor` picks up its `validate()` automatically.

## Verify live before trusting a new provider

A source's output shape is not contractually documented and can differ from
what you expect. Before wiring a provider into `daily`:

```
careeros discover --provider my-provider --dry-run --limit 3
```

This forces exactly that ONE provider (ignoring `providers:`) and prints a
preview of its raw items — inspect it directly. If a field you can see
there isn't showing up in the mapped `Job`, add its key name to the
relevant candidate list in `_field_mapping.py`. (A non-dry-run
`--provider my-provider` run also writes the full `raw.json` to
`.careeros/runs/<date>/01_discover/raw.json` under that provider's own key,
if you want to inspect more than the 3-item preview.)

Then judge its economics from a `limit >= 20-30` run, not the trial — see
the cost lesson at the end of "Evaluated and removed".

## Planned future providers

Greenhouse, Ashby, Lever, and Workday all expose fairly stable public JSON
board APIs (no Apify actor needed) — a direct-fetch provider for each is a
good first contribution, though note all three ATS platforms are already
covered indirectly via Fantastic Jobs' `active-ats` endpoint, so a dedicated
provider for one only adds value if it surfaces something that feed misses.
A generic "custom career site" provider (HTML scrape + heuristic field
extraction) is a larger, lower-priority effort.
