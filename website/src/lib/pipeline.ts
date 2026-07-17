export type StageKind = "deterministic" | "reasoning" | "output";

export interface Stage {
  id: string;
  name: string;
  kind: StageKind;
  purpose: string;
  inputs: string;
  outputs: string;
  why: string;
}

/**
 * The canonical daily pipeline, in execution order. Labels and rationale are
 * drawn verbatim-in-spirit from the CareerOS README, AGENT_GUIDE, and prompt
 * headers — no invented stages.
 */
export const stages: Stage[] = [
  {
    id: "discover",
    name: "Discover",
    kind: "deterministic",
    purpose:
      "Query every enabled provider with one segmented search per work-mode tier, then merge the results.",
    inputs: "Your profile targets · provider config · weekly/monthly quota",
    outputs: "raw.json — every job the providers returned",
    why: "Sourcing is plain code. It costs zero tokens, so no model call is spent on a job that hasn't even been deduplicated yet.",
  },
  {
    id: "normalize",
    name: "Normalize",
    kind: "deterministic",
    purpose:
      "Map every provider's payload onto one universal Job schema. The rest of the pipeline never knows which source a job came from.",
    inputs: "raw.json",
    outputs: "jobs.json — provider-agnostic Job records",
    why: "A single schema is what makes deduplication and every downstream stage possible without per-source special-casing.",
  },
  {
    id: "dedupe",
    name: "Dedupe",
    kind: "deterministic",
    purpose:
      "Drop jobs already seen this run, in a prior run, or already sitting in your Sheet. First occurrence wins.",
    inputs: "jobs.json · run history · existing Sheet IDs",
    outputs: "unique.json",
    why: "Deterministic IDs (a hash of source, company, title, location) make duplicates exact and cheap to remove before any spend.",
  },
  {
    id: "constraints",
    name: "Constraints",
    kind: "deterministic",
    purpose:
      "Hard-reject on the two objective deal-breakers — location and salary — before a single token is spent.",
    inputs: "unique.json · your location and salary floor",
    outputs: "eligible.json · rejected.json",
    why: "A binary deal-breaker is not a scoring problem. Weighted scoring can dilute a hard 'no' into a passing number; deterministic code cannot.",
  },
  {
    id: "gate",
    name: "AI Gate",
    kind: "reasoning",
    purpose:
      "A cheap, batched keep/drop triage against your headline and targets. Biased to keep — recall over precision.",
    inputs: "eligible.json · profile headline and targets",
    outputs: "gated.json — a tiny keep/drop verdict per job",
    why: "A job you wrongly drop here never gets a second look. A job you wrongly keep just costs one extra, cheap evaluation. So the gate keeps generously.",
  },
  {
    id: "evaluate",
    name: "Evaluate",
    kind: "reasoning",
    purpose:
      "The one real reasoning step. Scores each surviving job against the rubric and writes structured JSON only — no long report.",
    inputs: "gated.json · full profile · cache",
    outputs: "eval/<job-id>.json — the fit judgment, a source of truth",
    why: "This file is written once and reused everywhere downstream. Nothing re-scores a job — that would be exactly the duplicated spend the architecture exists to avoid.",
  },
  {
    id: "threshold",
    name: "Threshold",
    kind: "deterministic",
    purpose:
      "Partition evaluated jobs into two tiers and re-check the hard constraints as a backstop.",
    inputs: "eval JSON files",
    outputs: "selected.json (Apply) · consider.json (Consider)",
    why: "Apply-tier jobs (score ≥ 4.0) get the full treatment; Consider-tier (3.5–4.0) get a Sheet row only. Below 3.5 is omitted. Cheap partitioning, no tokens.",
  },
  {
    id: "artifacts",
    name: "Resume + Cover",
    kind: "reasoning",
    purpose:
      "For Apply-tier jobs only, select resume bullets and a cover-letter spine from your profile. Select — never invent.",
    inputs: "profile facts · eval JSON · cache",
    outputs: "resume.md · cover.md",
    why: "Every line is a verbatim copy of a fact you wrote. A deterministic verify step refuses to cache a resume that fails the match.",
  },
  {
    id: "apply",
    name: "Application Package",
    kind: "output",
    purpose:
      "Draft answers to a form's real questions, render a zero-cost daily report from the eval JSON, and append a row per job to your Google Sheet.",
    inputs: "eval JSON · profile logistics · the form's own questions",
    outputs: "answers.md · daily report · a Sheet row you open and act on",
    why: "The report is a pure template render — it costs no AI call. You open the Sheet every morning and start applying.",
  },
];
