"""Stage: constraints. Deterministic. Zero AI, zero tokens.

Hard deal-breakers, checked objectively before any AI is spent. This exists
because QA found a real correctness bug: an onsite-Bengaluru role scored
"apply" because strong role/skills scores outweighed a 10%-weighted logistics
penalty, diluting a hard constraint into a passing recommendation. Weighted
scoring is the wrong tool for a binary constraint. So location and salary are
enforced here as pass/fail, NOT as rubric weights.

Only OBJECTIVE constraints live here. Role fit stays an AI reasoning task
(gate + evaluate) — this module never inspects the title or judges seniority.

Three objective rules:
- Location: an onsite/hybrid role in a KNOWN city outside the profile's
  accepted onsite cities is a hard reject. Remote (any geography) always
  passes. Unknown work arrangement, or onsite with an unknown/missing city,
  passes (we don't reject on missing data — let the AI gate decide).
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
_WORK_AUTH_EXCLUSION_RE = re.compile(
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


def _work_authorization_excludes(job: Job, profile: Profile) -> bool:
    """True only for an EXPLICIT work-authorization exclusion, and only when
    the candidate actually needs visa sponsorship (`profile.location.
    visa_sponsorship_required`) — a candidate who doesn't need sponsorship
    is never affected by this rule regardless of what a posting says.
    See `_WORK_AUTH_EXCLUSION_RE`'s comment for the measurement behind the
    pattern set's scope."""
    if not (profile.location or {}).get("visa_sponsorship_required"):
        return False
    haystack = f"{job.location or ''} {job.description or ''}"
    return bool(_WORK_AUTH_EXCLUSION_RE.search(haystack))


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
