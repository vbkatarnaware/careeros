"""Golden-set drift detection (v2.0). Pure logic, no I/O beyond what's
handed in — the CLI layer (careeros/cli/calibrate.py) owns reading/writing
.careeros/golden/*.json.

The golden set is ~20 human-verified jobs (schemas/golden.schema.json) used
to catch a scorer drifting between runs, agents, or models — a check the
calibration module (careeros/calibration.py) can't do, since that module
only detects NON-derivation (a score that doesn't match its own rubric),
never WRONGNESS (a score that's honestly derived from a wrong rubric).

Golden MAE must never become a tuning input (see the v2.0 plan's Goodhart
guard) — this module only ever produces a report, never a score.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

RUBRIC_DIMS = ("role_fit", "seniority_fit", "skills_match", "domain", "logistics")

DEFAULT_TOLERANCES = {
    "per_dimension_warn": 1.0,
    "per_dimension_block": 1.5,
    # Signed, not absolute -- a POSITIVE mean error is the inflation
    # signature; a negative one means the scorer turned harsh, which starves
    # the pipeline and is a different (also real) problem.
    "mean_signed_warn_high": 0.3,
    "mean_signed_block_high": 0.5,
    "mean_signed_warn_low": -0.5,
    "band_agreement_warn_below": 5,  # out of `n` probed
    "band_agreement_block_below": 4,
    # The band-agreement thresholds above are absolute counts calibrated for
    # a typical n=6 probe. Below this sample size they're meaningless (e.g.
    # n=2 with both correct would otherwise "block" for being under 4) --
    # skip the band-agreement severity checks entirely under this floor.
    "band_agreement_min_n": 4,
}


def _band(score: float, threshold: float, consider_threshold: float) -> str:
    if score >= threshold:
        return "apply"
    if score >= consider_threshold:
        return "consider"
    return "omit"


def deterministic_sample(
    entries: list[dict], *, n: int, seed: str, exclude_holdout: bool = True
) -> list[dict]:
    """A deterministic, seed-keyed sample — seeded by run date, so the agent
    being probed can't pick which items it sees, and a rerun of the same
    date reproduces the same sample. Stratifies round-robin across bands
    from the deterministic order, so a probe of n=6 tends to cover
    apply/consider/omit rather than clumping in whichever band sorts first.
    `holdout: true` entries are never included — they exist specifically so
    memorization of the probed subset can't fully game the set."""
    pool = [e for e in entries if not (exclude_holdout and e.get("holdout"))]
    pool_sorted = sorted(
        pool, key=lambda e: hashlib.sha1(f"{seed}|{e['job_hash']}".encode()).hexdigest()
    )
    by_band: dict[str, list[dict]] = {}
    for e in pool_sorted:
        by_band.setdefault(e["band"], []).append(e)
    bands = list(by_band.keys())
    order: list[dict] = []
    i = 0
    while len(order) < len(pool_sorted) and bands:
        band = bands[i % len(bands)]
        if by_band[band]:
            order.append(by_band[band].pop(0))
        i += 1
        if all(not v for v in by_band.values()):
            break
    return order[:n]


@dataclass(frozen=True)
class DriftReport:
    n: int
    severity: str  # "clean" | "warn" | "block"
    per_dimension_max_deviation: dict[str, float]
    mean_signed_error: float
    band_agreement: int
    band_total: int
    mismatches: list[dict] = field(default_factory=list)


def score_probe(
    entries_by_hash: dict[str, dict],
    actual_by_hash: dict[str, dict],
    *,
    threshold: float = 4.0,
    consider_threshold: float = 3.5,
    tolerances: Optional[dict[str, Any]] = None,
) -> DriftReport:
    """Compare probed actual evals (freshly written, unlabelled, by the
    agent under test) against the golden `expected` values. Three separate
    metrics, on purpose -- each answers a different question, and a single
    scalar here would immediately become the next thing to game:

      - per-dimension max deviation: catches ONE dimension being
        systematically wrong (e.g. seniority_fit always +1.5) even when the
        aggregate looks fine.
      - mean SIGNED score error: the inflation detector specifically. Signed,
        not absolute -- MAE tells you "noisy", signed tells you "biased up".
      - band agreement: the operationally meaningful one -- did apply/
        consider/omit come out the same.
    """
    tol = dict(DEFAULT_TOLERANCES)
    tol.update(tolerances or {})

    per_dim_devs: dict[str, list[float]] = {d: [] for d in RUBRIC_DIMS}
    signed_errors: list[float] = []
    band_matches = 0
    mismatches: list[dict] = []
    n = 0

    for job_hash, expected_entry in entries_by_hash.items():
        actual = actual_by_hash.get(job_hash)
        if actual is None:
            continue
        n += 1
        expected = expected_entry["expected"]
        exp_rubric, act_rubric = expected["rubric"], actual["rubric"]
        for d in RUBRIC_DIMS:
            per_dim_devs[d].append(abs(float(act_rubric[d]) - float(exp_rubric[d])))
        signed_errors.append(float(actual["score"]) - float(expected["score"]))

        exp_band = expected_entry["band"]
        act_band = _band(float(actual["score"]), threshold, consider_threshold)
        if exp_band == act_band:
            band_matches += 1
        else:
            mismatches.append({
                "job_hash": job_hash, "label": expected_entry.get("label", job_hash),
                "expected_band": exp_band, "actual_band": act_band,
                "expected_score": expected["score"], "actual_score": actual["score"],
            })

    per_dim_max = {d: (max(vals) if vals else 0.0) for d, vals in per_dim_devs.items()}
    mean_signed = statistics.fmean(signed_errors) if signed_errors else 0.0

    band_check_active = n >= tol["band_agreement_min_n"]

    severity = "clean"
    if max(per_dim_max.values(), default=0.0) > tol["per_dimension_block"]:
        severity = "block"
    elif mean_signed > tol["mean_signed_block_high"]:
        severity = "block"
    elif band_check_active and band_matches < tol["band_agreement_block_below"]:
        severity = "block"
    elif (
        max(per_dim_max.values(), default=0.0) > tol["per_dimension_warn"]
        or mean_signed > tol["mean_signed_warn_high"]
        or mean_signed < tol["mean_signed_warn_low"]
        or (band_check_active and band_matches < tol["band_agreement_warn_below"])
    ):
        severity = "warn"

    return DriftReport(
        n=n, severity=severity,
        per_dimension_max_deviation={k: round(v, 3) for k, v in per_dim_max.items()},
        mean_signed_error=round(mean_signed, 3),
        band_agreement=band_matches, band_total=n,
        mismatches=mismatches,
    )
