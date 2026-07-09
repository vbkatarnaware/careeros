"""Level-1 Daily Report: a pure, deterministic render of Eval JSON.

This is CareerOS's core cost differentiator versus Career Ops-style
pipelines: the daily report costs ZERO extra AI tokens. Every field it needs
(company_summary, fit_paragraph, strengths, weaknesses, ats_keywords) is
already on the Eval object, written once by `evaluate`. This function is a
string template, nothing more — if it ever needs an AI call to fill a gap,
that's a signal the Eval schema is missing a field, not a signal to call the
model here.

Target: 150-250 words, decision support, not analysis. The Level-2 deep
report (see skills/prep.md) is where actual reasoning/research happens.
"""

from __future__ import annotations

from careeros.models import Eval, Job


def render_daily_report(job: Job, evaluation: Eval, resume_path: str, cover_path: str) -> str:
    strengths = "\n".join(f"- {s}" for s in evaluation.strengths)
    weaknesses = "\n".join(f"- {w}" for w in evaluation.weaknesses)
    keywords = ", ".join(evaluation.ats_keywords)

    return f"""# {job.title} at {job.company}

**Score:** {evaluation.score:.1f}/5 · **Confidence:** {evaluation.confidence:.0%} · **{evaluation.recommendation.upper()}**

## Company
{evaluation.company_summary}

## Why this fits
{evaluation.fit_paragraph}

## Top strengths
{strengths}

## Top concerns
{weaknesses}

## ATS keywords
{keywords}

## Artifacts
- Resume: {resume_path}
- Cover letter: {cover_path}

---
*Generated deterministically from evaluation data — no additional AI cost.*
*Want a full interview-prep report? Run `careeros prep {job.id}`.*
"""


def render_summary(
    date: str, manifest: dict,
    apply_evals: list[Eval], consider_evals: list[Eval],
    jobs_by_id: dict[str, Job],
    threshold: float = 4.0, consider_threshold: float = 3.5,
) -> str:
    """Day-level executive summary (P2.6). Same zero-AI philosophy as
    render_daily_report — a pure render of run.json + the day's ALREADY-
    PARTITIONED eval lists, no additional AI cost.

    `apply_evals`/`consider_evals` must be the SAME lists `threshold`
    (`pipeline/threshold.py:partition_evals`) already computed and persisted
    to `07_select/selected.json`/`consider.json` — this function must never
    re-derive the apply/consider split from raw evals itself (score+
    recommendation only, no hard-constraints check), or the summary can
    disagree with what actually got artifacts/Sheet rows (a real bug: a job
    with a hard deal-breaker but a high AI score would show here as Apply/
    Consider while `partition_evals` correctly omitted it everywhere else).

    Exists because the per-job daily_report.md has no day-at-a-glance view,
    and because the P2.6 KPI (cost per interview-worthy job, supply-aware —
    never a fixed daily quota) needs to be visible somewhere every run, not
    computed by hand from run.json.
    """
    totals = manifest.get("totals", {})

    funnel_rows = [
        ("Discovered", "discovered"), ("Deduped", "deduped"), ("Eligible", "eligible"),
        ("Gated", "gated"), ("Evaluated", "evaluated"), ("Selected", "selected"),
    ]
    funnel = "\n".join(f"- {label}: {totals[key]}" for label, key in funnel_rows if key in totals) or "_No stages recorded yet._"

    selected = sorted(apply_evals, key=lambda e: -e.score)
    near_miss = sorted(consider_evals, key=lambda e: -e.score)

    def _label(e: Eval) -> str:
        job = jobs_by_id.get(e.id)
        return f"{job.company} — {job.title}" if job else e.id

    apply_section = "\n".join(
        f"- **{e.score:.1f}** {_label(e)}: {e.strengths[0] if e.strengths else ''}" for e in selected
    ) or "_None today — the market simply didn't have one; see Cost below, this doesn't mean the run failed._"

    near_miss_section = "\n".join(f"- {e.score:.1f} {_label(e)}" for e in near_miss) or "_None._"

    # apify_cost_usd_total is a LOWER BOUND, not the settled final spend —
    # found live (2026-07-08) that the actor's own reported usageTotalUsd can
    # undercount the real monthly-usage delta (some charges settle async,
    # after discover already returns). Directionally useful for comparing
    # runs/configs; check your Apify console for the authoritative total.
    cost_total = totals.get("apify_cost_usd_total", 0.0)
    cost_per_job = totals.get("cost_per_selected_job_usd")
    cost_line = f"${cost_total:.4f} Apify spend today (lower bound, not settled final total — see below)"
    if cost_per_job is not None:
        cost_line += f" → **${cost_per_job:.4f} per selected (≥{threshold:.1f}) job**"
    else:
        cost_line += " → 0 selected today, so no cost-per-job to report (supply-limited, not a run failure)"

    return f"""# CareerOS Daily Summary — {date}

## Funnel
{funnel}

## Apply — score ≥ {threshold:.1f} ({len(selected)})
{apply_section}

## Consider — near miss, {consider_threshold:.1f}–{threshold - 0.1:.1f} ({len(near_miss)})
{near_miss_section}

## Cost
{cost_line}
_Apify's own reported cost can settle asynchronously after a query returns, so
this figure may undercount your actual monthly usage — check the Apify
console for the authoritative total; treat this as a directional signal for
comparing runs/configs, not an exact bill._

---
*Generated deterministically from run.json + evaluation data — no additional AI cost.*
*KPI: maximize interview-worthy (≥{threshold:.1f}) jobs per dollar — never a fixed daily quota.*
"""
