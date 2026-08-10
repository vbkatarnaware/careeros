"""Provider: ats-dataset — the free, open `ats-scrapers` hosted dataset
(https://github.com/kalil0321/ats-scrapers, MIT). Queries real company ATS
platforms directly (Greenhouse, Lever, Ashby, Darwinbox, Keka by default)
instead of a paid aggregator — `apply_url` is the company's own application
page, never LinkedIn.

Why this exists: an audit found Fantastic Jobs' `apply_url` for this query
profile is 100% LinkedIn, which structurally blocks the `apply` stage. This
provider replaces it: 4.85M jobs / 79,906 companies / 65 ATS platforms,
regenerated daily, no API key, no scraping infrastructure of our own —
including India-specific platforms (Darwinbox, Keka) Fantastic Jobs never
surfaced. See docs/ats-registry.md and the project plan for the full
rationale, including why building our own web crawler was rejected (weeks
of work re-doing what this project already maintains for free).

Ships ENABLED by default (templates/config.example.yaml, v1.9) — a fresh OSS
clone works with zero signup. `fantastic-jobs` remains in the repo,
disabled, for instant rollback (flip two booleans in config.yaml).

Optional dependency: `pip install -e ".[ats-dataset]"` (pandas + pyarrow +
httpx + ats-scrapers). `validate()` reports the exact command when missing
rather than crashing `discover`/`doctor` — a fresh clone with no extras
installed still runs, it just skips this provider with a clear message.

Three deterministic (zero-AI) filters replace what Fantastic Jobs' server-side
`experience_levels` param used to do, since this dataset has no experience
field — see the project plan's "AI cost strategy" for the measurement behind
each:
  1. Seniority title filter — drop Principal/Director/VP/Head of/Chief
     (deliberately KEEPS Senior/Lead/Staff — see `_SENIOR_TITLE_RE`'s
     docstring for the recall/precision measurement behind that split).
  2. JD years-of-experience filter — a job clearly stating it needs
     materially more experience than `profile.deal_breakers.min_years_ok`
     is dropped; anything ambiguous is KEPT (never reject on uncertainty).
  3. Freshness filter — `max_age_days` (default 30). NOT a mirror of
     Fantastic Jobs' `time_range: 24h`: measured live 2026-08-08, this
     dataset's `posted_at` is a still-open posting's ORIGINAL listing date,
     not a "new today" signal, so a 1-day window returned zero matches
     across every default slice. 30 days is the smallest window that
     produced healthy volume against the real profile; `limit` (not this)
     is what actually bounds daily volume.

Testability seam: everything except `_load_slice` and `_title_mask` is a
pure function over plain dicts, so the filtering/mapping logic is fully
testable on a machine with no pandas installed — see
careeros/tests/test_provider_ats_dataset.py.
"""

from __future__ import annotations

import importlib.util
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from functools import lru_cache, reduce
from operator import or_
from pathlib import Path
from typing import Any

import yaml

from careeros.config import Config
from careeros.models import Profile
from careeros.providers.base import ProviderResult

# ── defaults — module constants, NOT careeros/config.py's DEFAULT_CONFIG.
# greenhouse/lever/ashby already set this precedent (they live only in
# templates/config.example.yaml): DEFAULT_CONFIG["providers"] has exactly
# one key (test_config_providers.py:36 asserts this), and adding one here
# would break that test for zero benefit — a provider's own config block
# reads its extras via `.get(key, _DEFAULT)` instead. ──────────────────────
#
# The OSS default (2026-08-09): every genuine multi-tenant ATS platform in
# the upstream dataset — 33 of the 65 available `by_ats` sources. Excluded
# deliberately, by category, not by omission:
#   - single-company scrapers (amazon, apple, google, meta, tesla, uber,
#     bytedance, tiktok) — each returns exactly one employer's postings,
#     not a general ATS platform; add individually if you want one.
#   - government/public employment portals (eures, bundesagentur,
#     arbetsformedlingen, jobbankca) — not company ATS.
#   - regional non-English-market platforms (beisen[_legacy], hrmos, wanted,
#     gupy, programathor, manfred, getonbrd, join_com, thehub, jobsch,
#     moka, herp, welcometothejungle) — narrow geographic/language fit for
#     a global default; add if your search targets that market.
#   - job-board aggregators (remoteok, weworkremotely — this project's own
#     providers/README.md already documents both "evaluated and removed" in
#     v1.7 for poor conversion; wellfound, ycombinator, builtin, mercor —
#     thin/unreliable coverage per the upstream project's own docs).
#
# REAL COST of this default: ~2GB+ downloaded and several minutes of
# discovery time on every run (Workday alone, the largest single platform,
# took 380s / 1.3GB RSS in live testing — see providers/README.md). This is
# a deliberate "maximum coverage out of the box, then narrow to taste"
# choice, not an oversight — override `providers.ats-dataset.ats` in your
# own config.yaml to use a smaller/faster set (see providers/README.md for
# a worked example).
_DEFAULT_SLICES = [
    "ashby", "avature", "bamboohr", "breezy", "cornerstone", "darwinbox",
    "dayforce", "eightfold", "gem", "greenhouse", "icims", "jazzhr",
    "jobvite", "keka", "lever", "oracle", "pageup", "paycom", "paylocity",
    "personio", "phenom", "pinpoint", "recruitee", "recruiterbox",
    "rippling", "smartrecruiters", "softgarden", "successfactors", "taleo",
    "teamtailor", "ukg", "workable", "workday",
]
# Measured live 2026-08-08 against the real dataset: `posted_at` is the
# ORIGINAL listing date of a still-open posting, not a "new today" signal
# like Fantastic Jobs' time_range — this is a snapshot of currently-open
# jobs, not an incremental feed. A 1-day window (the naive mirror of
# Fantastic Jobs' `time_range: 24h`) returned ZERO matches across every
# default slice; 30 days returned 620 with the current real profile +
# global_remote. `limit` (below) is what bounds daily volume — this window
# just excludes postings stale enough to likely be dead/forgotten.
_DEFAULT_MAX_AGE_DAYS = 30
_DEFAULT_LIMIT = 100
_DEFAULT_TITLE_EXCLUSIONS = ["intern", "internship", "trainee", "marketing", "social media", "assistant"]

# Layer 1: seniority levels genuinely out of reach for a ~3-year-experience
# candidate. Deliberately does NOT include Senior/Lead/Staff — this project's
# own measurement (.careeros/config.yaml, 2026-08-04, 510 real India jobs)
# found those titles convert at 26.7% and excluding them costs 17 points of
# recall for zero precision gain. Only titles further up the ladder are cut.
_SENIOR_TITLE_RE = re.compile(
    r"\b(principal|director|head of|chief|vice president|vp|svp|evp|cxo)\b", re.IGNORECASE
)

# Place name -> ISO 3166-1 alpha-2, for `<place>_remote` work_mode_priority
# tiers. A country not listed here still works: write its ISO code directly
# in the tier name (`de_remote`) — no code change needed, see
# `_place_to_iso`.
_COUNTRY_ISO = {
    "india": "IN", "united states": "US", "united kingdom": "GB", "germany": "DE",
    "canada": "CA", "australia": "AU", "singapore": "SG", "france": "FR",
    "netherlands": "NL", "ireland": "IE", "spain": "ES", "italy": "IT",
    "japan": "JP", "brazil": "BR", "mexico": "MX", "united arab emirates": "AE",
    "poland": "PL", "sweden": "SE",
}

_EMPLOYMENT_MAP = {
    "fulltime": "full_time", "parttime": "part_time", "contract": "contract",
    "contractor": "contract", "temporary": "contract", "freelance": "contract",
    "intern": "internship", "internship": "internship",
}

# The dataset's `salary_period` string -> Job.Salary.unit vocabulary. min/max
# are stored in their NATIVE period, never pre-annualized here —
# pipeline/constraints.py's `_PERIODS_PER_YEAR` is the one place that
# annualizes, uniformly across every provider (same convention as
# providers/ats/ashby.py's salary mapping).
_SALARY_PERIOD_MAP = {
    "year": "year", "yearly": "year", "annual": "year", "annually": "year",
    "month": "month", "monthly": "month", "week": "week", "weekly": "week",
    "day": "day", "daily": "day", "hour": "hour", "hourly": "hour",
}

_APOS_RE = re.compile(r"[’']")

# Layer 2: a stated years-of-experience floor, tied explicitly to the word
# "experience"/"exp" so "founded 5 years ago" or "5 years of runway" never
# false-positives. Conservative on purpose — an unmatched JD returns None
# (never rejected), only a clearly stated number counts as evidence.
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+\s*years?\s*(?:of\s+)?(?:relevant\s+|professional\s+|work(?:ing)?\s+)?experience", re.IGNORECASE),
    re.compile(r"minimum\s+(?:of\s+)?(\d{1,2})\s*years?\s*(?:of\s+)?experience", re.IGNORECASE),
    re.compile(r"at\s+least\s+(\d{1,2})\s*years?\s*(?:of\s+)?experience", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*\d{1,2}\s*years?\s*(?:of\s+)?experience", re.IGNORECASE),
    re.compile(r"(\d{1,2})\+?\s*years?\s*(?:of\s+)?experience\s+(?:required|preferred|needed)", re.IGNORECASE),
]


# ── geography — mirrors pipeline/queryplan.py's tier-name parsing exactly,
# but produces a row predicate instead of a query dict ────────────────────

@dataclass
class GeoSpec:
    remote_anywhere: bool = False
    remote_places: list[str] = dc_field(default_factory=list)
    onsite_cities: list[str] = dc_field(default_factory=list)
    unrestricted: bool = False  # no parseable work_mode_priority tiers at all


def build_geo_spec(profile: Profile) -> GeoSpec:
    """Reads the SAME three profile fields `queryplan.build_query_plan` reads
    (`work_mode_priority`, `location.onsite_ok`), parsed by the SAME
    suffix rules (`global_remote` / `<place>_remote` / `*_onsite`, all
    onsite tiers collapsed into one set of cities) — see that module's
    docstring for why this shape was chosen. A profile with no parseable
    tiers falls back to `unrestricted=True` (accept everything), the same
    fail-open spirit as `build_query_plan`'s own fallback."""
    work_modes = list(getattr(profile, "work_mode_priority", []) or [])
    location = getattr(profile, "location", {}) or {}
    onsite_ok = [c for c in (location.get("onsite_ok") or []) if isinstance(c, str) and c.strip()]

    spec = GeoSpec()
    has_onsite_tier = False
    for tier in work_modes:
        if tier == "global_remote":
            spec.remote_anywhere = True
        elif tier.endswith("_remote"):
            place = tier[: -len("_remote")].replace("_", " ").strip()
            if place:
                spec.remote_places.append(place)
        elif tier.endswith("_onsite"):
            has_onsite_tier = True
        # unrecognized tier shape: skip rather than guess, same as queryplan.py

    if has_onsite_tier:
        spec.onsite_cities = onsite_ok

    if not spec.remote_anywhere and not spec.remote_places and not spec.onsite_cities:
        spec.unrestricted = True
    return spec


def _place_to_iso(place: str) -> str | None:
    stripped = place.strip()
    if len(stripped) == 2:
        return stripped.upper()
    return _COUNTRY_ISO.get(stripped.lower())


def _row_matches_place(row: dict[str, Any], place: str) -> bool:
    iso = _place_to_iso(place)
    country = (row.get("country_iso") or "").strip().upper()
    if iso and country:
        return country == iso
    haystack = f"{row.get('location') or ''} {row.get('region') or ''}".lower()
    return place.strip().lower() in haystack


# `is_remote` is unpopulated (None) on a large share of rows — measured
# 100% on greenhouse's fresh Product-titled rows. Without a fallback, every
# remote tier (india_remote, global_remote) is structurally unreachable for
# those rows regardless of profile config: they'd only ever be evaluated
# against onsite_cities. This regex recovers a remote signal from the
# fields most/least likely to false-positive it — LOCATION/REGION/TITLE
# only, deliberately never `description`, where "remote" shows up in
# unrelated boilerplate ("remote team collaboration tools") far more often
# than as a genuine posting attribute.
_REMOTE_TEXT_RE = re.compile(r"\bremote\b|\bwork[\s-]from[\s-]home\b|\bwfh\b", re.IGNORECASE)


def _row_looks_remote_by_text(row: dict[str, Any]) -> bool:
    haystack = f"{row.get('location') or ''} {row.get('region') or ''} {row.get('title') or ''}"
    return bool(_REMOTE_TEXT_RE.search(haystack))


def row_matches_geo(row: dict[str, Any], spec: GeoSpec) -> bool:
    """`is_remote` is True / False / None (unknown — common in this dataset,
    verified live, ~53% of rows and 100% of greenhouse's). An explicitly
    True or False row trusts that flag as-is. A None row gets one fallback
    chance: `_row_looks_remote_by_text` on location/region/title (never
    description, see its docstring) — if that also finds nothing, the row
    is evaluated against `onsite_cities` only, same as before this fallback
    existed. This is an INCLUDE filter, not `constraints.py`'s reject
    filter, so "never reject on missing data" doesn't apply here the same
    way.

    `global_remote` (`spec.remote_anywhere`) is DELIBERATELY conservative,
    not "match every remote row" — live-checked 2026-08-08 across ~1,240
    real "Product Manager"-titled remote rows (lever+ashby): `region` is
    empty on 100% of them (unusable), and a "worldwide/global/anywhere"
    text match produced false positives from company names and boilerplate
    ("3pillarglobal" matched on its own name; "airapps" roles in Madrid/
    London/Rome/Berlin matched on "global clients" copy despite being
    single-city roles). Neither signal reliably distinguishes a genuinely
    open role from one locked to some OTHER single country — exactly the
    failure mode `.careeros/profile.yaml`'s own 2026-08-04 note already
    measured (a blanket "anywhere" tier converted at 1.8%, mostly killed by
    invisible visa/residency restrictions). So `remote_anywhere` only
    matches a row with NO country_iso set at all — strictly narrower than
    "any remote job" — and country-specific `<place>_remote` tiers (e.g.
    `india_remote`) remain the reliable path, since `country_iso` itself is
    a clean field."""
    return matched_geo_tier(row, spec) is not None


def matched_geo_tier(row: dict[str, Any], spec: GeoSpec) -> str | None:
    """Which `work_mode_priority` tier `row_matches_geo` actually matched —
    real per-job provenance instead of the `["default"]` every row got
    before (see `discover.py`'s `_fetch_provider`, which only defaults when
    a provider hasn't already populated `ProviderResult.tiers`). Reconstructs
    the tier name the SAME way `build_geo_spec` derived it from profile.yaml
    (place -> f"{place}_remote"), so `gate_evaluate.py`'s `_gate_rank_key`
    can match it against real `work_mode_priority` entries.

    Onsite tiers report as one `"onsite"` bucket rather than the specific
    city-tier (`mumbai_onsite` vs `navi_mumbai_onsite` vs `thane_onsite`) —
    `build_geo_spec` already collapses all onsite tiers into one
    `onsite_cities` list by design (see that function's docstring), so this
    matches an existing architectural choice rather than losing information
    that was there to keep.

    Exact same matching logic as the bool predicate above, which now just
    delegates here — this function's docstring above (`row_matches_geo`'s)
    still documents the real matching rules, since this is the same logic
    with the return value kept instead of thrown away.

    **`"global_remote"` means "remote, no detected geographic restriction"
    — it is NOT a claim that the role is confirmed open to the candidate's
    own country.** Measured 2026-08-10 across 517 real `global_remote`-tagged
    jobs: 75% (387) had no geographic signal at all (genuinely ambiguous,
    not silently excluded — they still flow to the AI Gate); 18% (91) were
    explicitly India/worldwide-open; 7% (38) explicitly excluded a specific
    country. `constraints.py`'s `_work_authorization_excludes` catches the
    unambiguous 7% deterministically before the AI Gate; the Gate itself
    (which reads the full profile including geography) is what actually
    reasons about the ambiguous 75% — this function's tier label was never
    meant to answer that question on its own."""
    if spec.unrestricted:
        return "unrestricted"
    is_remote = row.get("is_remote")
    if is_remote is None and _row_looks_remote_by_text(row):
        is_remote = True
    if is_remote is True:
        if spec.remote_anywhere and not (row.get("country_iso") or "").strip():
            return "global_remote"
        for place in spec.remote_places:
            if _row_matches_place(row, place):
                return f"{place.replace(' ', '_')}_remote"
        return None
    if spec.onsite_cities:
        haystack = (row.get("location") or "").lower()
        if any(city.lower() in haystack for city in spec.onsite_cities):
            return "onsite"
    return None


# ── other pure filters ─────────────────────────────────────────────────

def row_is_fresh(row: dict[str, Any], cutoff: datetime) -> bool:
    """An undated row is dropped (`posted_at` missing/unparseable) — see the
    project plan's noted risk: if this proves too aggressive on the real
    dataset, `max_age_days` is a config-only fix, not a code change."""
    value = row.get("posted_at")
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _title_excluded(title: str | None, exclusions: list[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(x.lower() in t for x in exclusions if x)


def seniority_excluded(title: str | None) -> bool:
    if not title:
        return False
    return bool(_SENIOR_TITLE_RE.search(title))


def years_required(description: str | None) -> int | None:
    """Best-effort extraction of a stated minimum years-of-experience
    requirement. Returns the LOWEST plausible bound mentioned, or None when
    nothing matches — an unparseable/ambiguous JD must never silently
    reject a job, only a clearly stated number does."""
    if not description:
        return None
    candidates: list[int] = []
    for pattern in _YEARS_PATTERNS:
        for m in pattern.finditer(description):
            try:
                candidates.append(int(m.group(1)))
            except (ValueError, IndexError):
                continue
    return min(candidates) if candidates else None


def years_exceed(description: str | None, min_years_ok: Any, tolerance: float = 1.0) -> bool:
    """True only when the JD clearly states a requirement that materially
    exceeds the candidate's `deal_breakers.min_years_ok` (+ tolerance).
    False (never reject) when either side is unparseable/unset."""
    required = years_required(description)
    if required is None:
        return False
    try:
        floor = float(min_years_ok)
    except (TypeError, ValueError):
        return False
    return required > floor + tolerance


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalizes one pandas-derived row dict into plain JSON-safe Python
    types: NaN/NaT -> None, numpy scalars -> their `.item()`, pandas
    Timestamp -> ISO 8601 string. `models.dumps` is plain `json.dumps`,
    which raises on `numpy.int64` and emits invalid bare `NaN` — every row
    from `df.to_dict("records")` MUST pass through this or `discover`
    writes an unparseable `raw.json`. Duck-typed (no pandas/numpy import),
    so this stays testable on a machine without either installed."""
    cleaned: dict[str, Any] = {}
    for k, v in row.items():
        if v is None:
            cleaned[k] = None
        elif type(v).__name__ in ("NaTType", "NAType"):
            cleaned[k] = None
        elif isinstance(v, float) and v != v:  # the classic float-NaN self-inequality check
            cleaned[k] = None
        elif hasattr(v, "isoformat"):  # pandas.Timestamp / datetime
            cleaned[k] = v.isoformat()
        elif hasattr(v, "item"):  # numpy scalar (int64, float64, bool_, ...)
            cleaned[k] = v.item()
        else:
            cleaned[k] = v
    return cleaned


def _employment_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    squashed = re.sub(r"[^a-z]", "", value.lower())
    return _EMPLOYMENT_MAP.get(squashed)


def _salary(row: dict[str, Any]) -> dict[str, Any] | None:
    """None unless BOTH an amount AND a mapped period exist —
    `constraints.annual_inr` defaults `unit or "year"`, so a monthly figure
    with an unmapped period would read as annual, 12x overstated, and
    wrongly clear a salary floor. Ambiguous is the safe direction: the gate
    still sees the number inside the description text either way."""
    min_v = row.get("salary_min")
    max_v = row.get("salary_max")
    if min_v is None and max_v is None:
        return None
    period = row.get("salary_period")
    unit = _SALARY_PERIOD_MAP.get(str(period).strip().lower()) if period else None
    if unit is None:
        return None
    return {"min": min_v, "max": max_v, "currency": row.get("salary_currency"), "unit": unit}


def to_job_dict(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = raw.get("title")
    company = raw.get("company")
    apply_url = raw.get("apply_url") or raw.get("url")
    if not title or not company or not apply_url or not str(apply_url).startswith("http"):
        return None
    is_remote = raw.get("is_remote")
    return {
        "title": title,
        "company": company,
        "apply_url": apply_url,
        "location": raw.get("location") or raw.get("region"),
        "description": raw.get("description"),
        "remote": bool(is_remote) if is_remote is not None else None,
        "employment_type": _employment_type(raw.get("employment_type")),
        "posted_at": raw.get("posted_at"),
        "salary": _salary(raw),
    }


# ── the one pandas seam (besides _load_slice) ──────────────────────────

def _title_mask(df: Any, includes: list[str]) -> Any:
    """Vectorized case-insensitive substring OR across `includes`,
    apostrophe-normalized on both sides so "Founder's Office" matches
    "Founders Office" / "Founder Office" alike. `regex=False` is mandatory
    — a role term containing "(" or "+" (e.g. "C++") would otherwise raise
    on an invalid pattern."""
    import pandas as pd

    terms = [t for t in includes if t and t.strip()]
    if not terms:
        return pd.Series(True, index=df.index)
    norm_titles = df["title"].fillna("").astype(str).str.replace(_APOS_RE, "", regex=True)
    masks = [
        norm_titles.str.contains(_APOS_RE.sub("", t), case=False, regex=False, na=False)
        for t in terms
    ]
    return reduce(or_, masks)


@lru_cache(maxsize=1)
def _manifest_generated_at() -> str:
    """One manifest fetch per PROCESS (not per slice) — `fetch()` calls
    `_load_slice` once per configured ATS, and without this every one of
    those calls would separately re-fetch the small manifest.json just to
    read `generated_at`. `lru_cache` is process-lifetime, so a fresh
    `careeros discover` invocation always re-checks (correct — that's the
    whole point), it just doesn't re-check 33 times within one run."""
    from ats_scrapers.manifest import DEFAULT_MANIFEST_URL, Manifest

    manifest = Manifest.fetch(DEFAULT_MANIFEST_URL)
    return manifest.generated_at.strftime("%Y%m%dT%H%M%S")


def _dataset_cache_dir() -> Path:
    d = Path(".careeros") / "dataset"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_slice(ats: str) -> Any:
    """The ONLY function that touches `ats_scrapers` directly for reading
    data — tests patch this so nothing else in this module needs pandas
    installed to be tested.

    Caches each downloaded slice to `.careeros/dataset/<ats>-<generated_at>
    .parquet`, keyed by the manifest's own `generated_at` — a new snapshot
    changes the key, the old file for that `ats` is pruned, nothing else.
    Measured: every `careeros discover` re-downloaded ~2GB across 33 slices
    even on days the upstream snapshot hadn't changed at all (it publishes
    every few days, not continuously). This doesn't change what `discover`
    returns — normalize/dedupe/constraints/gate/evaluate all still run on
    whatever's current — it only decides network vs. local disk for
    identical bytes. Any cache failure (corrupt file, write error, manifest
    fetch failure) falls through to a normal live download rather than
    masking a real problem."""
    import pandas as pd
    from ats_scrapers import Client

    try:
        generated_at = _manifest_generated_at()
    except Exception:
        return Client().load(ats=ats)

    cache_path = _dataset_cache_dir() / f"{ats}-{generated_at}.parquet"
    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            pass  # corrupt cache file — fall through to a fresh download

    df = Client().load(ats=ats)
    try:
        df.to_parquet(cache_path)
        for old in _dataset_cache_dir().glob(f"{ats}-*.parquet"):
            if old != cache_path:
                old.unlink()
    except Exception:
        pass  # caching is an optimization, never fatal to discovery
    return df


def _load_profile(config: Config) -> Profile:
    """Deliberately duplicated from `careeros/cli/_shared.py`'s
    `_load_profile` rather than imported — a provider importing from `cli`
    would invert the intended dependency direction (cli depends on
    providers, never the reverse)."""
    with open(config.profile_path) as f:
        return Profile.from_dict(yaml.safe_load(f))


class AtsDatasetProvider:
    id = "ats-dataset"

    def validate(self, config: Config) -> list[str]:
        """Local, no-network checks — safe for `doctor` to call every run."""
        problems: list[str] = []
        if importlib.util.find_spec("ats_scrapers") is None:
            problems.append('ats-scrapers not installed — run: pip install -e ".[ats-dataset]"')
        if not config.profile_path.exists():
            problems.append(
                "no .careeros/profile.yaml — this provider derives its title and "
                "geography filters from your profile (run `careeros init` / `/careeros start`)"
            )
        return problems

    def fetch(
        self, config: Config, *, limit: int = _DEFAULT_LIMIT, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`search`/`query` accepted for Provider Protocol parity, unused —
        this provider derives its filters from `profile.yaml`, not a
        free-text override (same as providers/ats/greenhouse.py)."""
        if importlib.util.find_spec("ats_scrapers") is None:
            return ProviderResult.skip(self.id, 'ats-scrapers not installed — run: pip install -e ".[ats-dataset]"')

        block = config.providers.get(self.id, {}) or {}
        slices: list[str] = block.get("ats", _DEFAULT_SLICES)
        max_age_days = block.get("max_age_days", _DEFAULT_MAX_AGE_DAYS)
        title_exclusions = block.get("title_exclusions", _DEFAULT_TITLE_EXCLUSIONS)

        try:
            profile = _load_profile(config)
        except (OSError, TypeError, KeyError) as e:
            return ProviderResult.skip(self.id, f"could not read .careeros/profile.yaml: {e}")

        includes = list(getattr(profile, "role_priorities", []) or [])
        min_years_ok = (getattr(profile, "deal_breakers", {}) or {}).get("min_years_ok")
        geo = build_geo_spec(profile)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        start = time.time()
        warnings: list[str] = []
        matched: list[dict[str, Any]] = []
        slices_loaded = 0

        for ats in slices:
            try:
                df = _load_slice(ats)
            except Exception as e:
                warnings.append(f"{ats}: failed to load — {e}")
                continue
            slices_loaded += 1

            df = df.drop(columns=["raw"], errors="ignore")
            if includes:
                df = df[_title_mask(df, includes)]
            rows = [clean_row(r) for r in df.to_dict("records")]
            del df

            for row in rows:
                if not row_is_fresh(row, cutoff):
                    continue
                if _title_excluded(row.get("title"), title_exclusions):
                    continue
                if seniority_excluded(row.get("title")):
                    continue
                if min_years_ok is not None and years_exceed(row.get("description"), min_years_ok):
                    continue
                tier = matched_geo_tier(row, geo)
                if tier is None:
                    continue
                row["_geo_tier"] = tier
                matched.append(row)

        matched.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
        items = matched[:limit]
        if len(matched) > limit:
            warnings.append(
                f"matched {len(matched)} jobs, kept only the {limit} most recent "
                f"(limit={limit}) — raise providers.ats-dataset.limit in config.yaml "
                "if this is dropping real candidates, not noise"
            )
        # Real per-job tier provenance (v2.0) — `discover.py` only falls back
        # to `[["default"]]` when a provider hasn't populated this itself.
        tiers = [[item.pop("_geo_tier")] for item in items]

        return ProviderResult(
            provider=self.id, items=items, tiers=tiers, cost_usd=0.0,
            requests=slices_loaded, records=len(items),
            seconds=time.time() - start, warnings=warnings,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        return to_job_dict(raw)


PROVIDER = AtsDatasetProvider()
