# Providers

A provider discovers jobs from one source and hands them to the pipeline in
a common shape. The pipeline never imports a provider directly — it goes
through `registry.get(name)` — so adding a new one never touches
`pipeline/`, `cli.py`, or any other stage.

## Fantastic Jobs: two registered providers, one dataset

CareerOS ships two providers on the SAME Fantastic Jobs dataset (see the
P2.6/P2.7 architecture review for the full reasoning):

- **`fantastic-jobs`** (`fantastic_jobs.py`) — the official REST API. **Default
  and actively maintained.** Supports two transports via `config.api.transport`
  (no default — you must choose): `"direct"` (developer.fantastic.jobs) or
  `"rapidapi"` (RapidAPI's "Active Jobs DB"). Both proxy the identical
  dataset and differ only in base URL + auth header; which is cheaper for
  your volume is a config/commercial decision, not an architectural one.
  Also queries **both** upstream endpoints by default via `config.api.endpoint:
  "both"` — `active-ats` (career sites/ATS) and `active-jb` (+LinkedIn/YC/
  Wellfound), merged, with the per-tier record allocation split 50/50 (not
  doubled). This is the P2.8 Final Discovery Acceptance Audit's frozen
  default — see `.careeros/qa/acceptance_audit_report.md` for the evidence
  (full 107-job population: both sources score an equal ~8% ≥4.0 rate but are
  92% disjoint, so "both" roughly doubles interview-worthy jobs at the same
  quota cost). Set `endpoint` to `active-ats` or `active-jb` for a single
  source, and `endpoint_allocation` to change the split ratio on a paid plan.
- **`fantastic-jobs-actor`** (`legacy/fantastic_jobs_actor.py`) — the Apify
  actor. **Legacy/reference**, kept for no-code/Zapier/n8n/MCP-style setups.
  Not the actively maintained path; new discovery features land in the REST
  provider only.

Both share `to_job_dict()` verbatim (same field names, byte-for-byte —
enforced by `careeros/tests/test_provider_fantastic_jobs_parity.py`), so
switching `provider:` between them changes nothing downstream of `discover`.

## The contract

Copy `fantastic_jobs.py` (or `legacy/fantastic_jobs_actor.py` if you want a
no-code/actor-style reference instead). You need exactly two methods:

```python
class MyProvider:
    id = "my-provider"

    def fetch(self, config: Config, **kwargs) -> list[dict]:
        """Call your source. Return raw records, untouched."""
        ...

    def to_job_dict(self, raw: dict) -> dict | None:
        """Map one raw record to {title, company, location, apply_url,
        description, remote, seniority, employment_type, posted_at}.
        Return None to skip a record missing a title or a usable URL."""
        ...
```

Then register it in `registry.py`:

```python
from careeros.providers.my_provider import PROVIDER as MY_PROVIDER
_REGISTRY = {..., MY_PROVIDER.id: MY_PROVIDER}
```

That's the whole integration. `pipeline/normalize.py` calls `to_job_dict()`
for every raw record and turns the result into a `Job` (assigning `id`,
deriving `ats` from the apply URL's domain, truncating `description`).

## Field names are never guaranteed

Every job-board/Apify actor names its fields slightly differently
(`company` vs `company_name` vs `employer`). Don't hardcode one name — use
`_apify_common.py`'s candidate-key lists (`pick_field(raw, COMPANY_KEYS)`)
the same way `fantastic_jobs.py` does, and extend a candidate list if your
source uses a name that isn't covered yet.

## Errors: raise ProviderError, don't let the SDK crash raw

If your source has expected, actionable failure modes (missing/expired
credentials, an unset config choice, a paid API's budget exhausted), catch
them and raise `careeros.providers.base.ProviderError` with a message
telling the user what to do — the CLI catches this in `discover` and prints
it cleanly instead of an unhandled traceback. `fantastic_jobs.py` does this
for a missing/invalid `config.api.transport` and a missing API key;
`legacy/fantastic_jobs_actor.py` does it for a missing/exhausted Apify
token, also rotating through a comma-separated token pool
(`config.apify.tokens_env`) before giving up, since a single paid-API token
running out mid-`daily` shouldn't be a hard stop if a spare is configured.

## Verify live before trusting a new provider

Apify actor output shapes are not contractually documented and can differ
from what you expect. Before wiring a provider into `daily`:

```
careeros discover --provider my-provider --dry-run --limit 3
```

Inspect `.careeros/runs/<date>/01_discover/raw.json` directly. If a field
you can see in the raw JSON isn't showing up in the mapped `Job`, add its key
name to the relevant candidate list in `_apify_common.py`.

## Planned future providers

Greenhouse, Ashby, Lever, and Workday all expose fairly stable public JSON
board APIs (no Apify actor needed) — a direct-fetch provider for each is a
good first contribution. A generic "custom career site" provider (HTML
scrape + heuristic field extraction) is a larger, lower-priority effort.
