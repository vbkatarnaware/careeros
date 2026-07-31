"""Anti-inflation calibration for the `evaluate` stage (v2.0).

Forensic background (2026-07-31): comparing each stored eval `score` against
its own rubric's unrounded weighted average showed two clean days (07-28,
07-31) sitting at the 0.050 float-rounding ceiling with zero violations, and
two suspect days (07-29, 07-30) blowing past it with 12-13 violations each
and a 3x margin. Those two suspect days also showed a rubric dimension
holding an identical value across 11 unrelated jobs, and a weakness string
that named a seniority mismatch on a record whose seniority_fit didn't move.
None of that is query quality — it's an AI grading its own reasoning without
actually deriving the number from the rubric it wrote.

This module is a set of MECHANICAL, deterministic checks over already-written
eval records. It never edits a record and never assigns a "calibration health
score" (a single scalar becomes the next thing to game the moment it exists).
Every check returns a `Finding`, always, whether it fired or not — durable
data for the ledger, not just a gate.

Block vs warn is a deliberate split:
  - BLOCK only on checks that are per-record and locally fixable (arithmetic,
    the seniority prose/number contradiction) or that name specific colliding
    records (the rubric-vector clone). Blocking on a batch STATISTIC invites
    an agent to nudge numbers until the statistic passes — a Goodhart trap
    inside the harness itself.
  - WARN on batch-level dispersion/repetition signals. In particular: NEVER
    block on pass rate. 07-28 was a genuinely honest day at 89% — a >=90%
    trigger misses it by half a job, and pass rate is exactly the quantity
    a query tuner is trying to raise, so blocking on it would teach an agent
    to deflate scores to keep finalize passing. Score DISPERSION (stdev)
    cleanly separates the four known days and isn't the tuner's target.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

RUBRIC_WEIGHTS: dict[str, float] = {
    "role_fit": 0.30,
    "seniority_fit": 0.20,
    "skills_match": 0.25,
    "domain": 0.15,
    "logistics": 0.10,
}

# Fixed order for tie-breaking (never dict iteration order) so any derived
# label (see careeros/pipeline/outcomes.py) is byte-stable across runs.
GAP_ORDER = ("role_fit", "skills_match", "seniority_fit", "domain", "logistics")

# Rounding ceiling: two float32-ish roundings (rubric input -> 1 decimal,
# then the weighted sum -> 1 decimal) can legitimately disagree with the raw
# unrounded weighted sum by up to 0.05. 0.051 gives a hair of margin without
# opening the door to a real miss. Verified empirically: honest days peak at
# exactly 0.050; suspect days start at 0.140.
DEFAULT_ARITHMETIC_TOLERANCE = 0.051

DEFAULT_SENIORITY_MARKERS = (
    "senior for",
    "seniority stretch",
    "is a stretch",
    "stretch for",
    "over-senior",
    "more senior than",
    "significant stretch",
    "years experience which",
    "years of experience required",
)


@dataclass(frozen=True)
class Finding:
    """One calibration check's result. Always produced, fired or not —
    `_calibration.json` records the full set so a clean run is provable,
    not just an absence of complaints."""

    code: str
    severity: str  # "block" | "warn"
    fired: bool
    headline: str
    detail: list[str] = field(default_factory=list)
    stat: dict[str, Any] = field(default_factory=dict)


def weighted_score(rubric: dict[str, float]) -> float:
    """The rubric's own contract: score = weighted average, rounded to 1
    decimal. This is intentionally the ONLY place that formula lives outside
    the prompt — every check below compares against it, never redefines it."""
    return round(sum(RUBRIC_WEIGHTS[d] * float(rubric[d]) for d in RUBRIC_WEIGHTS), 1)


def _unrounded_weighted_score(rubric: dict[str, float]) -> float:
    return sum(RUBRIC_WEIGHTS[d] * float(rubric[d]) for d in RUBRIC_WEIGHTS)


def check_arithmetic(
    records: list[dict], *, tol: float = DEFAULT_ARITHMETIC_TOLERANCE
) -> Finding:
    """BLOCK. Non-gameable: the cheapest way to satisfy this is to actually
    compute the weighted average, and doing so tells the agent nothing about
    what values to write — it just re-derives the score from the rubric it
    already wrote. Compares against the UNROUNDED weighted sum (not
    `weighted_score`'s own rounding) so a legitimately-rounded score is never
    penalized twice."""
    bad = []
    for r in records:
        rubric = r.get("rubric") or {}
        if not all(d in rubric for d in RUBRIC_WEIGHTS):
            continue  # schema validation already guarantees this in practice
        expected = _unrounded_weighted_score(rubric)
        score = float(r.get("score", 0.0))
        dev = abs(score - expected)
        if dev > tol:
            bad.append(
                f"{r.get('id', '?')}: score={score} but rubric implies "
                f"{round(expected, 1)} (deviation={dev:.3f})"
            )
    return Finding(
        code="arithmetic",
        severity="block",
        fired=bool(bad),
        headline="score does not match its own rubric's weighted average",
        detail=bad,
        stat={"n": len(records), "violations": len(bad), "tolerance": tol},
    )


def check_prose_contradiction(
    records: list[dict],
    *,
    markers: tuple[str, ...] = DEFAULT_SENIORITY_MARKERS,
    seniority_fit_max: float = 3.5,
) -> Finding:
    """BLOCK. Seniority only, `weaknesses` only (never `fit_paragraph`, which
    the cover letter reuses and legitimately hedges). Seniority is uniquely
    binary — "this role wants more years than the candidate has" is either
    true or false. Generalizing this pattern to the other 4 dimensions
    produces noise (e.g. "domain is new to candidate" is a legitimate
    adjacent-domain read, not a contradiction) and teaches the agent to write
    blander weaknesses, destroying the one field this whole pipeline relies
    on for auditability."""
    bad = []
    for r in records:
        rubric = r.get("rubric") or {}
        seniority_fit = rubric.get("seniority_fit")
        if seniority_fit is None or float(seniority_fit) < seniority_fit_max:
            continue
        weaknesses = " ".join(r.get("weaknesses") or []).lower()
        for marker in markers:
            if marker in weaknesses:
                bad.append(
                    f"{r.get('id', '?')}: weaknesses names a seniority "
                    f"mismatch ('{marker}') but seniority_fit={seniority_fit}"
                )
                break
    return Finding(
        code="prose_contradiction",
        severity="block",
        fired=bool(bad),
        headline="weaknesses names a seniority mismatch the rubric doesn't price in",
        detail=bad,
        stat={"n": len(records), "violations": len(bad)},
    )


def check_rubric_vector_clones(
    records: list[dict],
    jobs_by_id: dict[str, dict],
    *,
    min_records: int = 4,
    min_companies: int = 3,
) -> Finding:
    """BLOCK. Fires when one exact 5-tuple rubric is shared by >=min_records
    records spanning >=min_companies distinct companies. The company-span
    requirement is what keeps this from false-positiving on genuine
    near-duplicate postings (same company, two listings that differ only in
    city) — those legitimately share a rubric and shouldn't block. Names the
    exact colliding ids, which is what makes a batch-level finding locally
    fixable rather than a vague "something in this 25 is wrong"."""
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        rubric = r.get("rubric") or {}
        if not all(d in rubric for d in RUBRIC_WEIGHTS):
            continue
        key = tuple(round(float(rubric[d]), 2) for d in GAP_ORDER)
        groups.setdefault(key, []).append(r)

    bad_groups = []
    for key, group in groups.items():
        companies = {jobs_by_id.get(r.get("id"), {}).get("company", "?") for r in group}
        if len(group) >= min_records and len(companies) >= min_companies:
            ids = ", ".join(r.get("id", "?")[:10] for r in group)
            bad_groups.append(
                f"rubric {key} shared by {len(group)} records across "
                f"{len(companies)} companies: {ids}"
            )
    return Finding(
        code="rubric_vector_clone",
        severity="block",
        fired=bool(bad_groups),
        headline="identical rubric vector repeated across unrelated companies",
        detail=bad_groups,
        stat={"n": len(records), "groups_flagged": len(bad_groups)},
    )


def check_uniformity(
    records: list[dict],
    threshold: float,
    *,
    min_n: int = 10,
    max_stdev: float = 0.25,
    min_pass_rate: float = 0.90,
) -> Finding:
    """WARN, never block. Deliberately NOT a pass-rate check — see module
    docstring. Fires only when a batch is BOTH tight (low score dispersion)
    AND mostly-passing; either alone is a legitimately good day. Verified:
    honest days sigma=0.529/0.671 (07-28/07-31 as fetched), suspect days
    sigma=0.187/0.135 (07-29/07-30)."""
    n = len(records)
    if n < min_n:
        return Finding(
            code="uniformity", severity="warn", fired=False,
            headline="batch too small to assess dispersion",
            stat={"n": n, "min_n": min_n},
        )
    scores = [float(r.get("score", 0.0)) for r in records]
    stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0
    pass_rate = sum(1 for s in scores if s >= threshold) / n
    fired = stdev < max_stdev and pass_rate >= min_pass_rate
    return Finding(
        code="uniformity",
        severity="warn",
        fired=fired,
        headline="score spread is unusually tight for a mostly-passing batch",
        stat={"n": n, "stdev": round(stdev, 3), "pass_rate": round(pass_rate, 3),
              "max_stdev": max_stdev, "min_pass_rate": min_pass_rate},
    )


def check_dimension_repetition(
    records: list[dict],
    *,
    min_n: int = 10,
    max_mode_share: float = 0.60,
    max_distinct_values: int = 3,
    exempt: tuple[str, ...] = ("logistics",),
) -> Finding:
    """WARN, never block. `logistics` is exempt by default: eval_v2.md gives
    it an explicit anchor scale (~5.0 top remote tier down to ~1.0 outside
    onsite_ok), so it is quasi-categorical BY DESIGN — a day of mostly
    India-remote jobs should repeat it. Honest 07-28 hit 0.56 mode share on
    logistics alone, one job away from a naive all-dimension rule firing on
    a clean day."""
    n = len(records)
    if n < min_n:
        return Finding(
            code="dimension_repetition", severity="warn", fired=False,
            headline="batch too small to assess repetition",
            stat={"n": n, "min_n": min_n},
        )
    flagged = []
    per_dim_stat = {}
    for dim in RUBRIC_WEIGHTS:
        if dim in exempt:
            continue
        values = [round(float((r.get("rubric") or {}).get(dim, 0.0)), 2) for r in records]
        counts: dict[float, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        mode_value, mode_count = max(counts.items(), key=lambda kv: kv[1])
        mode_share = mode_count / n
        per_dim_stat[dim] = {"mode_value": mode_value, "mode_share": round(mode_share, 3),
                              "distinct_values": len(counts)}
        if mode_share >= max_mode_share and len(counts) <= max_distinct_values:
            flagged.append(
                f"{dim}: {mode_count}/{n} records ({mode_share:.0%}) share value "
                f"{mode_value} across only {len(counts)} distinct value(s)"
            )
    return Finding(
        code="dimension_repetition",
        severity="warn",
        fired=bool(flagged),
        headline="a rubric dimension repeats the same value across most of the batch",
        detail=flagged,
        stat={"n": n, "dimensions": per_dim_stat, "exempt": list(exempt)},
    )


def run_calibration(
    records: list[dict],
    jobs_by_id: dict[str, dict],
    *,
    threshold: float,
    cfg: Optional[dict[str, Any]] = None,
) -> list[Finding]:
    """Run every check and return all Findings, fired or not. `cfg` is the
    optional `calibration:` config block (see careeros/config.py) — every
    threshold below has a code default so calibration works with zero
    config, and `cfg` only overrides what's explicitly set."""
    cfg = cfg or {}
    return [
        check_arithmetic(records, tol=cfg.get("arithmetic_tolerance", DEFAULT_ARITHMETIC_TOLERANCE)),
        check_prose_contradiction(
            records,
            markers=tuple(cfg.get("seniority_markers", DEFAULT_SENIORITY_MARKERS)),
            seniority_fit_max=cfg.get("seniority_fit_max_with_marker", 3.5),
        ),
        check_rubric_vector_clones(
            records, jobs_by_id,
            min_records=cfg.get("clone_min_records", 4),
            min_companies=cfg.get("clone_min_companies", 3),
        ),
        check_uniformity(
            records, threshold,
            min_n=cfg.get("min_batch_n", 10),
            max_stdev=cfg.get("uniformity_max_stdev", 0.25),
            min_pass_rate=cfg.get("uniformity_min_pass_rate", 0.90),
        ),
        check_dimension_repetition(
            records,
            min_n=cfg.get("min_batch_n", 10),
            max_mode_share=cfg.get("dimension_mode_share_warn", 0.60),
            exempt=tuple(cfg.get("dimension_exempt", ("logistics",))),
        ),
    ]


def blocking_findings(findings: list[Finding]) -> list[Finding]:
    """The subset that should fail `evaluate --finalize`."""
    return [f for f in findings if f.fired and f.severity == "block"]


def findings_to_dicts(findings: list[Finding]) -> list[dict[str, Any]]:
    return [
        {"code": f.code, "severity": f.severity, "fired": f.fired,
         "headline": f.headline, "detail": f.detail, "stat": f.stat}
        for f in findings
    ]
