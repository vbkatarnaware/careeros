"""Stage: outcomes (v2.0). Deterministic. Zero AI, zero tokens.

Derives WHY a job landed where it did, for the learning ledger
(careeros/pipeline/ledger.py), WITHOUT adding a field to the eval schema —
schemas/eval.schema.json is `additionalProperties: false` and `Eval.from_dict`
splats the dict into the dataclass constructor, so a new field there needs
three lockstep edits (schema + dataclass + prompt) and invalidates every
cached eval unless the prompt version bumps too. Everything here is instead
derived, after the fact, purely from a job's own already-written rubric —
which also means it's fully regenerable and safe to backfill onto historical
runs.

`primary_gap` is WEIGHTED CONTRIBUTION SHORTFALL, not raw argmin over the
rubric's raw values. Raw argmin is the wrong choice: on a day where most
Mumbai-onsite jobs sit around `logistics ≈ 2.5`, argmin would report
"logistics" as the primary gap across nearly the whole tier, even though
`logistics` is only 10% of the weighted score — the ACTIONABLE failure is
usually `role_fit` (30%) or `skills_match` (25%) sitting even a little low.
Weighting by `RUBRIC_WEIGHTS` before ranking fixes that; see
`shortfall_vector`/`primary_gap` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from careeros.calibration import GAP_ORDER, RUBRIC_WEIGHTS
from careeros.models import Eval

# Below this weighted-shortfall value, no dimension is meaningfully
# "the reason" — every dimension sits close enough to its ceiling
# (5.0) that reporting a primary gap would be noise, not signal.
NO_MEANINGFUL_GAP_THRESHOLD = 0.15


def shortfall_vector(rubric: dict[str, float]) -> dict[str, float]:
    """weight * (5.0 - value) per dimension — how much of the MISSING score
    each dimension is responsible for. `role_fit`'s 0.30 weight means a
    modest gap there costs more than an even larger gap in `logistics`
    (0.10 weight), which is exactly the correction raw argmin is missing."""
    return {d: round(RUBRIC_WEIGHTS[d] * (5.0 - float(rubric[d])), 4) for d in GAP_ORDER}


@dataclass(frozen=True)
class JobOutcome:
    id: str
    tier: str  # "apply" | "consider" | "omit"
    score: float
    recommendation: str
    shortfall: dict[str, float]
    primary_gap: Optional[str]  # a GAP_ORDER dimension name, "deal_breaker", or None
    secondary_gap: Optional[str]
    tiers: Optional[list[str]]  # discovery query tier(s) that surfaced this job

    def to_dict(self) -> dict:
        return {
            "id": self.id, "tier": self.tier, "score": self.score,
            "recommendation": self.recommendation, "shortfall": self.shortfall,
            "primary_gap": self.primary_gap, "secondary_gap": self.secondary_gap,
            "tiers": self.tiers,
        }


def primary_gap(rubric: dict[str, float], recommendation: str) -> tuple[Optional[str], Optional[str], dict[str, float]]:
    """Returns (primary_gap, secondary_gap, shortfall_vector).

    `recommendation == "skip"` (a JD-stated deal-breaker the eval prompt
    caught, or the score-clamp's own backstop) returns `primary_gap =
    "deal_breaker"` rather than a rubric dimension — the rubric can look
    strong on a job that was skipped for a visa bar or an explicit
    no-sponsorship line, and reporting e.g. "domain" as the reason would
    send a query tuner chasing the wrong fix entirely.

    Below `NO_MEANINGFUL_GAP_THRESHOLD`, returns `(None, None, shortfall)` —
    a 4.8-scoring Apply job has no "primary gap" worth recording, and
    forcing one would just add noise to the ledger's aggregation.

    Ties broken by the fixed `GAP_ORDER`, never dict iteration order, so
    the result is byte-stable across runs (this feeds a ledger a tuner
    reads — stability matters more than which of two near-equal
    dimensions technically "wins")."""
    shortfall = shortfall_vector(rubric)
    if recommendation == "skip":
        return "deal_breaker", None, shortfall
    ranked = sorted(GAP_ORDER, key=lambda d: (-shortfall[d], GAP_ORDER.index(d)))
    if shortfall[ranked[0]] < NO_MEANINGFUL_GAP_THRESHOLD:
        return None, None, shortfall
    return ranked[0], ranked[1], shortfall


def build_outcomes(
    apply_: list[Eval], consider_: list[Eval], omit_: list[Eval],
    *, tiers_by_id: dict[str, Optional[list[str]]],
) -> list[JobOutcome]:
    """Builds one JobOutcome per evaluated job across all three tiers —
    including `omit_`, which `careeros/cli/pipeline.py`'s `threshold`
    command previously computed and discarded entirely (bound to `_omit`
    and never used). `tiers_by_id` comes from `02_normalize/jobs.json`'s
    `Job.tiers` (see careeros/pipeline/dedupe.py's tier-union-on-collapse)."""
    outcomes: list[JobOutcome] = []
    for tier_name, evals in (("apply", apply_), ("consider", consider_), ("omit", omit_)):
        for e in evals:
            gap, secondary, shortfall = primary_gap(
                {d: getattr(e.rubric, d) for d in GAP_ORDER}, e.recommendation,
            )
            outcomes.append(JobOutcome(
                id=e.id, tier=tier_name, score=e.score, recommendation=e.recommendation,
                shortfall=shortfall, primary_gap=gap, secondary_gap=secondary,
                tiers=tiers_by_id.get(e.id),
            ))
    return outcomes
