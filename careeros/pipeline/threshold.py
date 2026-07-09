"""Stage: threshold. Deterministic. Zero AI, zero tokens.

The one gate between "evaluated" and "gets artifacts generated." Everything
evaluated still appears in the Sheet (so the candidate sees the full
picture); only jobs scoring >= threshold get resume/cover/report generation,
because that's the expensive-ish step (an AI call per artifact) and most of
a day's discovered jobs never become a real application.

A job is SELECTED only if all three hold:
1. score >= threshold,
2. the eval's recommendation is "apply", and
3. the deterministic hard constraints still pass (re-checked here, so even if
   the AI mislabels a hard-reject as "apply", it is removed anyway).
This is the fix for the QA bug where a hard deal-breaker got diluted by
weighted scoring into a passing recommendation.
"""

from __future__ import annotations

from careeros.models import Eval, Job, Profile
from careeros.pipeline.constraints import evaluate_constraints


def partition_evals(
    evals: list[Eval],
    apply_threshold: float,
    consider_threshold: float,
    jobs_by_id: dict[str, Job],
    profile: Profile,
    fx_rates: dict[str, float],
) -> tuple[list[Eval], list[Eval], list[Eval]]:
    """Two-tier selection (P2.8). Returns (apply, consider, omit), each sorted
    by score descending.

    - APPLY  = score >= apply_threshold AND recommendation=="apply" AND hard
      constraints pass → full pipeline (artifacts + Drive + Sheet).
    - CONSIDER = constraints pass, score >= consider_threshold, and NOT apply
      (a near-miss, or a high score the AI flagged non-apply) → Sheet row only,
      no AI artifacts, no Drive — cheap visibility into near-misses.
    - OMIT   = hard-constraint failure (a deterministic deal-breaker, dropped
      regardless of score) OR score < consider_threshold → not in the Sheet.

    Constraints remain a hard gate — the deterministic backstop against the AI
    mislabeling a hard-rejected job — so a location/salary deal-breaker is
    omitted entirely, never surfaced as a 'Consider'."""
    ranked = sorted(evals, key=lambda e: e.score, reverse=True)
    apply_: list[Eval] = []
    consider_: list[Eval] = []
    omit_: list[Eval] = []
    for e in ranked:
        job = jobs_by_id.get(e.id)
        constraints_ok = evaluate_constraints(job, profile, fx_rates).passed if job is not None else True
        if not constraints_ok:
            omit_.append(e)
        elif e.score >= apply_threshold and e.recommendation == "apply":
            apply_.append(e)
        elif e.score >= consider_threshold:
            consider_.append(e)
        else:
            omit_.append(e)
    return apply_, consider_, omit_
