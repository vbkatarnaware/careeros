"""Stage: constraints. Deterministic. Zero AI, zero tokens.

Hard deal-breakers, checked objectively before any AI is spent. This exists
because QA found a real correctness bug: an onsite-Bengaluru role scored
"apply" because strong role/skills scores outweighed a 10%-weighted logistics
penalty, diluting a hard constraint into a passing recommendation. Weighted
scoring is the wrong tool for a binary constraint. So location and salary are
enforced here as pass/fail, NOT as rubric weights.

Only OBJECTIVE constraints live here. Role fit stays an AI reasoning task
(gate + evaluate) — this module never inspects the title or judges seniority.

Two objective rules:
- Location: an onsite/hybrid role in a KNOWN city outside the profile's
  accepted onsite cities is a hard reject. Remote (any geography) always
  passes. Unknown work arrangement, or onsite with an unknown/missing city,
  passes (we don't reject on missing data — let the AI gate decide).
- Salary: reject ONLY when a confidently-computed annual-INR equivalent is
  below the profile's floor. Unknown/unparseable salary NEVER rejects — most
  postings omit salary, and rejecting on absence would nuke the pipeline.

Used in two places for a belt-and-suspenders guarantee: as its own pipeline
stage (so hard-rejects never reach the AI gate, saving tokens), and re-checked
inside threshold.select (so even if the AI mislabels a job "apply", the
deterministic rule still removes it).
"""

from __future__ import annotations

from dataclasses import dataclass

from careeros.models import Job, Profile

# Fraction of the floor a computed salary must fall UNDER to trigger a reject.
# 0.9 = only reject when clearly below floor, absorbing FX approximation so a
# borderline conversion is given the benefit of the doubt (passes to AI).
SALARY_REJECT_MARGIN = 0.9

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
            if annual < floor_inr * SALARY_REJECT_MARGIN:
                reasons.append(
                    f"salary ~INR {annual/100_000:.1f} LPA is below floor {floor_lpa} LPA"
                )

    return ConstraintResult(passed=not reasons, reasons=reasons)
