"""Stage: constraints. Deterministic. Zero AI, zero tokens.

Hard deal-breakers, checked objectively before any AI is spent. This exists
because QA found a real correctness bug: an onsite-Bengaluru role scored
"apply" because strong role/skills scores outweighed a 10%-weighted logistics
penalty, diluting a hard constraint into a passing recommendation. Weighted
scoring is the wrong tool for a binary constraint. So location and salary are
enforced here as pass/fail, NOT as rubric weights.

Only OBJECTIVE constraints live here. Role fit stays an AI reasoning task
(gate + evaluate) — this module never inspects the title or judges seniority.

Four objective rules:
- Location: an onsite/hybrid role in a KNOWN city outside the profile's
  accepted onsite cities is a hard reject. Remote (any geography) always
  passes. Unknown work arrangement, or onsite with an unknown/missing city,
  passes (we don't reject on missing data — let the AI gate decide).
- Region-restricted remote (v2.1, structured field only): reject a
  "remote" role whose STRUCTURED `location` string names exactly one
  non-India region ("Remote-EMEA", "Latin America - Remote", etc.) and
  never mentions India/worldwide/global/anywhere. Added after a real token
  audit (2026-08-12) found this the single largest AI-Gate drop reason
  across several batches, at zero AI cost when the signal is already in
  the structured field. Deliberately narrow — see
  `_region_restricted_remote`'s docstring for why this does NOT read the
  JD body: that's exactly the class of case `AGENT_GUIDE.md`'s
  never-scripted-reasoning rule protects, and where this same audit found
  the gate/evaluate stages catching real nuance a regex can't (an
  ambiguous "Senior/Staff" title, a JD stating a restriction the location
  field itself doesn't show).
- Salary: reject ONLY when a confidently-computed annual-INR equivalent is
  below the profile's floor. Unknown/unparseable salary NEVER rejects — most
  postings omit salary, and rejecting on absence would nuke the pipeline.
- Work authorization (v2.0, P0.7 "Level 0"): reject ONLY an EXPLICIT
  work-authorization exclusion ("must be authorized to work in the US") when
  the candidate needs visa sponsorship. Deliberately narrow — see
  `_work_authorization_excludes`'s docstring for the measurement behind the
  scope line. Everything ambiguous fails open to the AI Gate, which already
  reasons about geography as part of the full profile; this rule exists
  only to catch the unambiguous cases before they reach (and waste) that
  AI call, and especially before they'd waste a full Apply-tier artifact
  generation. `matched_geo_tier`'s `global_remote` tag on a job means
  "remote, no detected geographic restriction" — it is NOT a claim that the
  role is confirmed open to India; this rule and the Gate together are what
  actually enforce that.

  v2.2 (2026-08-14): this check now also reaches `job.eligibility_note` —
  sentence(s) `normalize.py` recovered from the FULL, pre-truncation
  description via `extract_eligibility_note` below, since a real audit
  found eligibility language sits at a median 84% depth in a JD while
  `description_max_chars` (4000) discards 72% of postings' tails. Widening
  the reach to free text (as opposed to the short, ATS-curated `location`
  field) surfaced 2 real false positives in that same audit — "Must be
  able to support USA business hours" (a timezone line, on an actual
  `India (Remote)` posting) and "(U.S. only)" on a benefits/insurance
  line — so the free-text pattern set (`_WORK_AUTH_TEXT_RE`) is narrower
  than the structured-field one (`_WORK_AUTH_LOCATION_RE`): it drops the
  bare "___ only"/"within the United States" phrasings (too fragile
  outside a short curated field) and requires an explicit residency verb
  for the "must be/reside/live" phrasing, rather than "any words within 30
  chars of a country name".

Used in two places for a belt-and-suspenders guarantee: as its own pipeline
stage (so hard-rejects never reach the AI gate, saving tokens), and re-checked
inside threshold.partition_evals (so even if the AI mislabels a job "apply",
the deterministic rule still removes it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from careeros.models import Job, Profile

# Explicit work-authorization exclusions only — no timezone/region-name
# heuristics ("Americas", "EST hours"). Measured 2026-08-10 across 517
# `global_remote`-tier jobs from the real dataset: this exact pattern set
# matched 38 (7%); 91 (18%) were explicitly India/worldwide-open; the
# remaining 387 (75%) had no geographic signal either way and are
# deliberately left for the AI Gate rather than guessed at here — a looser
# pattern (region names, timezone mentions) was tried during that
# measurement and produced false positives on genuinely-open roles.
#
# Applies to the STRUCTURED `location` field only — short and ATS-curated,
# so a bare "US only"/"within the United States" phrase is a reliable,
# deliberate restriction tag (same trust level `_region_restricted_remote`
# below already gives this field). Unchanged since v2.0.
_WORK_AUTH_LOCATION_RE = re.compile(
    r"authorized to work in the (?:US|U\.S\.|United States)"
    r"|must (?:be|reside|live) .{0,30}(?:United States|USA|US\b|UK\b|Canada|EU\b|Europe)"
    r"|\bUS[- ]only\b|\bU\.S\.[- ]only\b|\bUnited States only\b"
    r"|\bEU only\b|\bUK only\b|\bCanada only\b"
    r"|within the United States"
    r"|eligible to work in the (?:US|UK|EU|United States|Canada)"
    r"|work authorization in the (?:US|United States)"
    r"|legally authorized to work in the United States",
    re.IGNORECASE,
)

# Applies to free text (`job.description` and the `eligibility_note`
# `extract_eligibility_note` recovers from the truncated tail) — narrower
# than `_WORK_AUTH_LOCATION_RE` above. v2.2 (2026-08-14): free text is
# noisier than the structured field, so this set drops the bare "___
# only"/"within the United States" phrasings entirely (measured false
# positives: "(U.S. only)" on a benefits/insurance line; "apply across all
# geographic locations within the United States" on pay-benchmarking
# boilerplate) and requires an explicit residency verb for the "must
# be/reside/live" phrasing rather than "any words within 30 chars of a
# country name" (measured false positive: "Must be able to support USA
# business hours" — a timezone line, matched on an actual `India (Remote)`
# posting). The "authorized/eligible to work in"/"legally authorized"/"work
# authorization in" phrasings are unchanged from the location-field
# pattern — measured 0 false positives and covered every real true
# positive found in that same audit.
_WORK_AUTH_TEXT_RE = re.compile(
    r"authorized to work in the (?:US|U\.S\.|United States)"
    r"|eligible to work in the (?:US|UK|EU|United States|Canada)"
    r"|work authorization in the (?:US|United States)"
    r"|legally authorized to work in the United States"
    r"|must (?:be\s+(?:currently\s+)?(?:based|located|residing|a resident)\s+(?:in|of)"
    r"|reside in|live in)\s+(?:the\s+)?(?:United States|USA|US\b|UK\b|Canada|EU\b|Europe)",
    re.IGNORECASE,
)

# Fraction of the floor a CONVERTED salary must fall UNDER to trigger a
# reject. 0.9 = only reject when clearly below floor, absorbing FX
# approximation so a borderline conversion is given the benefit of the doubt
# (passes to AI).
SALARY_REJECT_MARGIN = 0.9

# v2.1: the margin above exists purely to absorb FX approximation error (see
# `fx_rates` in config.yaml — those are hand-maintained approximate rates).
# When a posting is ALREADY denominated in the candidate's own comp currency,
# no conversion happened and there is no approximation to absorb, so applying
# the margin there just silently lowers the floor the candidate actually set:
# a stated floor of 12 LPA was rejecting only below 10.8 LPA, letting an 11
# LPA posting through as if it cleared the bar. Same-currency salaries are
# therefore compared against the floor EXACTLY.
SALARY_EXACT_MARGIN = 1.0

_PERIODS_PER_YEAR = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}


@dataclass
class ConstraintResult:
    passed: bool
    reasons: list[str]  # human-readable reasons a job was rejected (empty if passed)


def annual_inr(salary, fx_rates: dict[str, float]) -> float | None:
    """Best-effort annual-INR equivalent of a Job.salary, or None if it can't
    be computed confidently. Uses the lower of min/max (conservative: we only
    reject when even the low end is below floor). Returns None on missing
    amount, unknown currency, or unknown unit — the caller treats None as
    'do not reject on salary'."""
    if salary is None:
        return None
    amount = salary.min if salary.min is not None else salary.max
    if amount is None or amount <= 0:
        return None
    currency = (salary.currency or "INR").upper()
    rate = fx_rates.get(currency)
    if rate is None:
        return None
    unit = salary.unit or "year"
    periods = _PERIODS_PER_YEAR.get(unit)
    if periods is None:
        return None
    return amount * periods * rate


def _accepted_onsite_cities(profile: Profile) -> list[str]:
    return [c.lower() for c in (profile.location or {}).get("onsite_ok", [])]


# v2.1: structured `location` field only — narrow, explicit token list,
# same discipline as _WORK_AUTH_EXCLUSION_RE above (no broad heuristics,
# no timezone-name guessing). Requires "remote" and a named region to
# co-occur within a short span, matching the real observed shapes
# ("Remote-EMEA", "Latin America - Remote", "Remote (Colombia)", "Remote:
# United States") rather than a bare country name anywhere in the string.
_REGION_TOKEN = (
    r"(?:usa?|united states|canada|uk|emea|europe|latam|latin america|apac"
    r"|iberia|colombia|argentina|brazil|mexico|philippines|poland|spain"
    r"|romania|serbia|russia)"
)
_REGION_RESTRICTED_REMOTE_RE = re.compile(
    rf"\bremote\b.{{0,20}}\b{_REGION_TOKEN}\b|\b{_REGION_TOKEN}\b.{{0,20}}\bremote\b",
    re.IGNORECASE,
)
# Any of these anywhere in the location string means the restriction above
# doesn't actually exclude the candidate (India is explicitly an option, or
# the role is open beyond the named region) — checked FIRST, so a string
# like "Remote within India, Canada or US" is never rejected.
_INDIA_OR_GLOBAL_RE = re.compile(r"\bindia\b|\bworldwide\b|\bglobal\b|\banywhere\b", re.IGNORECASE)


def _region_restricted_remote(job: Job) -> str | None:
    """The matched region phrase if `job.location` (the structured field
    only — never `description`) names exactly one non-India region
    alongside "remote", else None. See this module's docstring and the
    rule comment above for the scope line and why JD body text is
    deliberately out of reach here."""
    if job.remote is False:
        return None
    loc = job.location or ""
    if not loc or _INDIA_OR_GLOBAL_RE.search(loc):
        return None
    m = _REGION_RESTRICTED_REMOTE_RE.search(loc)
    return m.group(0) if m else None


def extract_eligibility_note(full_description: str | None, max_chars: int) -> str | None:
    """Pull any work-authorization-exclusion phrase(s) out of the part of a
    FULL, pre-truncation job description that `normalize.py`'s
    `description_max_chars` cut is about to discard, so they still reach
    `_work_authorization_excludes` below. Deliberately reuses
    `_WORK_AUTH_TEXT_RE` — the exact pattern that later decides whether to
    reject — rather than a separate "looks eligibility-y" detector, so
    extraction and enforcement can never drift apart.

    Returns None when there's nothing past the cut (the description isn't
    actually truncated) or nothing in the discarded tail matches — most
    jobs, since this is a targeted recovery, not a second copy of the
    description."""
    if not full_description or len(full_description) <= max_chars:
        return None
    tail = full_description[max_chars:]
    matches = [m.group(0) for m in _WORK_AUTH_TEXT_RE.finditer(tail)]
    if not matches:
        return None
    return " | ".join(dict.fromkeys(matches))  # de-dup, preserve first-seen order


def _work_authorization_excludes(job: Job, profile: Profile) -> bool:
    """True only for an EXPLICIT work-authorization exclusion, and only when
    the candidate actually needs visa sponsorship (`profile.location.
    visa_sponsorship_required`) — a candidate who doesn't need sponsorship
    is never affected by this rule regardless of what a posting says.
    See `_WORK_AUTH_LOCATION_RE`/`_WORK_AUTH_TEXT_RE`'s comments for the
    measurement behind each pattern set's scope, and why the structured
    `location` field and free text (`description` + `eligibility_note`)
    are checked with different-width patterns."""
    if not (profile.location or {}).get("visa_sponsorship_required"):
        return False
    if _WORK_AUTH_LOCATION_RE.search(job.location or ""):
        return True
    text_haystack = f"{job.description or ''} {job.eligibility_note or ''}"
    return bool(_WORK_AUTH_TEXT_RE.search(text_haystack))


def evaluate_constraints(job: Job, profile: Profile, fx_rates: dict[str, float]) -> ConstraintResult:
    reasons: list[str] = []

    # ---- Location ----
    # job.remote: True = remote (always ok), False = onsite/hybrid, None = unknown.
    # An onsite job is hard-rejected ONLY when its location is KNOWN and outside
    # the accepted cities. Unknown location (empty/missing) is passed to the AI
    # gate, not rejected here — consistent with this module's "never reject on
    # missing data" rule (a rare onsite posting with no stated city shouldn't be
    # silently dropped when it might actually be in an accepted city).
    if job.remote is False:
        accepted = _accepted_onsite_cities(profile)
        loc = (job.location or "").strip().lower()
        if accepted and loc and not any(city in loc for city in accepted):
            reasons.append(
                f"onsite/hybrid in '{job.location}', "
                f"outside accepted onsite location(s): {', '.join(profile.location.get('onsite_ok', []))}"
            )

    # ---- Region-restricted remote (structured field only) ----
    # Same gating as work-authorization below: only meaningful for a
    # candidate who actually needs sponsorship/isn't already a local hire
    # in the named region — a candidate who doesn't need it is unaffected
    # by what a posting's location field restricts, same reasoning as
    # `_work_authorization_excludes`.
    if (profile.location or {}).get("visa_sponsorship_required"):
        region = _region_restricted_remote(job)
        if region:
            reasons.append(f"remote restricted to a single non-India region per location field: '{region}'")

    # ---- Salary ----
    floor_lpa = (profile.comp or {}).get("floor_lpa")
    if floor_lpa is not None:
        annual = annual_inr(job.salary, fx_rates)
        if annual is not None:
            floor_inr = float(floor_lpa) * 100_000  # LPA -> absolute INR
            base_currency = ((profile.comp or {}).get("currency") or "INR").upper()
            job_currency = ((job.salary.currency if job.salary else None) or base_currency).upper()
            # No conversion happened -> no FX error to absorb -> exact compare.
            margin = SALARY_EXACT_MARGIN if job_currency == base_currency else SALARY_REJECT_MARGIN
            if annual < floor_inr * margin:
                reasons.append(
                    f"salary ~INR {annual/100_000:.1f} LPA is below floor {floor_lpa} LPA"
                )

    # ---- Work authorization ----
    if _work_authorization_excludes(job, profile):
        reasons.append(
            "explicit work-authorization exclusion incompatible with required visa sponsorship"
        )

    return ConstraintResult(passed=not reasons, reasons=reasons)
