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


def select_above_threshold(evals: list[Eval], threshold: float) -> tuple[list[Eval], list[Eval]]:
    """Score-only selection. Kept for the standalone dev command / tests.
    The daily pipeline uses select_final(), which also enforces the
    recommendation and deterministic-constraint guards."""
    ranked = sorted(evals, key=lambda e: e.score, reverse=True)
    selected = [e for e in ranked if e.score >= threshold]
    held_back = [e for e in ranked if e.score < threshold]
    return selected, held_back


def select_final(
    evals: list[Eval],
    threshold: float,
    jobs_by_id: dict[str, Job],
    profile: Profile,
    fx_rates: dict[str, float],
) -> tuple[list[Eval], list[Eval]]:
    """Returns (selected, held_back), sorted by score descending.

    Selected = score>=threshold AND recommendation=="apply" AND constraints
    pass. Everything else is held back (still reported/sheeted, just no
    artifacts). The constraint re-check is the deterministic backstop against
    an AI "apply" on a hard-rejected job."""
    ranked = sorted(evals, key=lambda e: e.score, reverse=True)
    selected: list[Eval] = []
    held_back: list[Eval] = []
    for e in ranked:
        job = jobs_by_id.get(e.id)
        constraints_ok = True
        if job is not None:
            constraints_ok = evaluate_constraints(job, profile, fx_rates).passed
        if e.score >= threshold and e.recommendation == "apply" and constraints_ok:
            selected.append(e)
        else:
            held_back.append(e)
    return selected, held_back
