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
