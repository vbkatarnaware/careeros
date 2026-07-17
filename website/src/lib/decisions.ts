export interface Decision {
  id: string;
  title: string;
  question: string;
  decision: string;
  why: string;
  consequence: string;
}

/**
 * Product decisions, each framed as reasoning rather than a feature blurb.
 * Every claim is grounded in the CareerOS README, AGENT_GUIDE, prompt headers,
 * and schema docstrings.
 */
export const decisions: Decision[] = [
  {
    id: "two-model-calls",
    title: "Gate before evaluate",
    question: "Why run a cheap gate before the real evaluation?",
    decision:
      "A single batched keep/drop triage runs against your targets before any job gets a full evaluation.",
    why: "Full evaluation is the expensive call. Most discovered jobs are not worth it. A wrongly-dropped job at the gate never gets a second look, so the gate is deliberately biased to keep — recall over precision. A wrongly-kept job just costs one extra, cheap evaluation.",
    consequence:
      "The expensive reasoning step only ever sees jobs that already passed deterministic constraints and a cheap plausibility check. Spend concentrates where judgment actually changes the outcome.",
  },
  {
    id: "deterministic-filtering",
    title: "Deterministic hard constraints",
    question: "Why filter location and salary in plain code, not with the model?",
    decision:
      "Location and salary deal-breakers are enforced by deterministic Python, before the AI gate, and re-checked after evaluation.",
    why: "A binary deal-breaker is not a scoring problem. In QA, a hard onsite constraint once got diluted into a passing weighted score. Weighted scoring is the wrong tool for a yes/no rule. Unknown or missing data never rejects — only a confidently-known violation does.",
    consequence:
      "A job you objectively cannot take never reaches a model, so no tokens are spent on it. The post-evaluation re-check is a guaranteed backstop against the model mislabeling a hard reject as 'apply'.",
  },
  {
    id: "json-first",
    title: "JSON-first evaluation",
    question: "Why write structured JSON instead of a long report per job?",
    decision:
      "The evaluate stage writes a compact JSON judgment — score, recommendation, three strengths, two weaknesses, a fit paragraph — and nothing longer.",
    why: "This is CareerOS's deliberate departure from long-form evaluation reports. The JSON is the entire output. The daily report renders from it with zero additional AI cost, and the fit paragraph doubles as the spine of the cover letter.",
    consequence:
      "One reasoning call produces a judgment that four downstream artifacts reuse. The daily pipeline stays cheap because nothing re-reads a job to write prose about it.",
  },
  {
    id: "two-sources-of-truth",
    title: "Two sources of truth",
    question: "Why does everything trace back to exactly two files?",
    decision:
      "Your profile facts and each job's evaluation JSON are the only sources of truth. Every other artifact is a derivation.",
    why: "Facts and judgments should be generated once and reused, not recomputed. This is 'selector, not writer' applied everywhere — a resume selects from your facts, a cover letter builds on the evaluation's fit paragraph, a report renders the JSON.",
    consequence:
      "No artifact can quietly contradict another, because they all inherit from the same two files. Changing a fact once propagates everywhere through cache invalidation, not manual edits.",
  },
  {
    id: "caching",
    title: "Fingerprinted caching",
    question: "Why cache every AI output, and key it the way it's keyed?",
    decision:
      "Each AI output is cached on a fingerprint of everything that could change the answer: the job's content hash, your profile version, and the active prompt version.",
    why: "The KPI is interview-worthy jobs per dollar. Re-running a day where nothing changed should cost nothing. Because the prompt version is inside the cache key, editing one prompt busts only that stage's cache — not the whole pipeline.",
    consequence:
      "A re-run of the daily pipeline with unchanged inputs costs zero AI calls. Enrichment that doesn't affect judgment (like a later-found salary) is deliberately excluded from the hash so it doesn't bust the cache.",
  },
  {
    id: "prompt-versioning",
    title: "Prompt versioning",
    question: "Why keep old prompt versions live instead of overwriting them?",
    decision:
      "Prompts are versioned files, selected per stage in config. Superseded versions stay in the repo and still run.",
    why: "The prompt version is part of every cache fingerprint, so versioning is what makes targeted cache invalidation possible. Keeping old versions live means a run pinned to an earlier prompt still reproduces exactly, which matters for auditing why a past evaluation scored the way it did.",
    consequence:
      "Cache invalidation, auditability, and backward compatibility all fall out of one mechanism. Improving a prompt is a one-line config change that regenerates only the affected stage.",
  },
  {
    id: "profile-versioning",
    title: "Profile versioning",
    question: "Why does the profile carry a version number?",
    decision:
      "The profile has an integer version. Bumping it invalidates cached evaluations, resumes, and cover letters that depended on the changed facts.",
    why: "When your facts change, every derivation that depended on them should recompute against the new facts. The version flows into every cache key, so a bump is how stale artifacts get regenerated. Logistics answers are deliberately excluded — no scoring or writing stage reads them, so they don't need to bust anything.",
    consequence:
      "Editing your profile can't leave behind a resume built from outdated facts. The one exception (logistics) is carved out precisely because it can never affect a score or a written line.",
  },
  {
    id: "verify-resume",
    title: "verify-resume",
    question: "Why a mechanical check that resume lines match your profile?",
    decision:
      "A deterministic step confirms every resume bullet and the summary verbatim-match a fact in your profile. The finalize step refuses to cache a resume that fails it.",
    why: "The resume stage is a selector, not a writer. A guarantee is stronger than an instruction — the model is told to select verbatim, and then a mechanical check enforces it, rather than trusting that it did.",
    consequence:
      "A resume physically cannot contain a sentence you didn't write. A gap in your profile surfaces as a prompt to add a real fact, never as license to invent one.",
  },
  {
    id: "lint",
    title: "Voice lint",
    question: "Why lint every AI-written artifact deterministically?",
    decision:
      "A deterministic lint flags em-dashes, a banned-vocabulary list, and negative-parallelism constructions across every generated artifact.",
    why: "Style guidance in a prompt is a suggestion the model can drift from. A mechanical check is a backstop that catches the common AI tells regardless of which host model wrote the text.",
    consequence:
      "Artifacts read like something a person wrote, consistently, across any coding CLI. A lint failure blocks caching until the file is fixed.",
  },
  {
    id: "opt-in-prep",
    title: "Opt-in interview prep",
    question: "Why isn't the deep interview report part of the daily run?",
    decision:
      "The long-form deep report is generated only when you explicitly ask for it, per job, with a separate command.",
    why: "It is the one deliberately long-form artifact, and the only one, precisely because it's opt-in. Most discovered jobs never become an actual application, so generating an expensive report for every one would be exactly the wasted spend the architecture exists to avoid.",
    consequence:
      "The daily run stays cheap and fast. The expensive report exists, but you pay for it only on the handful of jobs you're actually pursuing — and it expands the existing evaluation rather than re-computing it.",
  },
];
