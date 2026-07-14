"""Deterministic backstop for prompts/voice-dna.md.

voice-dna.md is a prompt-level instruction: it tells the model how to write.
Models slip. This module is the cheap, zero-token check that runs AFTER
generation on any voice-dna-governed artifact (resume, cover letter,
application answers) and flags the mechanical, unambiguous violations:
em-dashes, banned AI vocabulary, and the "negative parallelism" tell
("This isn't X. This is Y."). It does not (and can't) check taste — that's
what the Critical Review Gate in the prompt is for. This just catches the
slips a regex can catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A representative subset of voice-dna.md's banned list (section 3A). Kept
# short and high-signal rather than exhaustive — this is a fast backstop, not
# a re-implementation of the full prompt.
BANNED_WORDS = [
    "delve", "realm", "harness", "unlock", "tapestry", "paradigm",
    "cutting-edge", "revolutionize", "intricate", "showcasing", "crucial",
    "pivotal", "meticulously", "vibrant", "unparalleled", "leverage",
    "synergy", "innovative", "game-changer", "testament", "holistic",
    "seamless", "streamline", "empower", "elevate", "robust", "scalable",
    "groundbreaking", "trailblazing", "transformative", "redefine",
    "frictionless", "future-proof",
]

BANNED_PHRASES = [
    "in today's", "it's important to note", "it's worth noting",
    "in order to", "let's dive in", "let's explore", "at the end of the day",
    "moving forward", "furthermore", "additionally", "moreover",
    "that being said",
]

# The fatal negative-parallelism pattern (voice-dna.md section 3F): "Not X. Y."
# / "This isn't X. This is Y." / "X? No. Y." — matched loosely, on purpose,
# since this is the single most reliable AI tell and false positives here are
# cheaper than false negatives.
NEGATIVE_PARALLELISM_RE = re.compile(
    r"\b(not just|not only|isn'?t about|isn'?t just|isn'?t)\b.{0,60}\b(it'?s about|it'?s|this is|but)\b",
    re.IGNORECASE,
)

EM_DASH = "—"


@dataclass
class LintIssue:
    kind: str          # "em_dash" | "banned_word" | "banned_phrase" | "negative_parallelism"
    line: int
    snippet: str


def lint_text(text: str) -> list[LintIssue]:
    issues: list[LintIssue] = []
    lines = text.splitlines()

    for i, line in enumerate(lines, start=1):
        if EM_DASH in line:
            issues.append(LintIssue("em_dash", i, line.strip()))

        lower = line.lower()
        for word in BANNED_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", lower):
                issues.append(LintIssue("banned_word", i, f"'{word}' in: {line.strip()}"))

        for phrase in BANNED_PHRASES:
            if phrase in lower:
                issues.append(LintIssue("banned_phrase", i, f"'{phrase}' in: {line.strip()}"))

        if NEGATIVE_PARALLELISM_RE.search(line):
            issues.append(LintIssue("negative_parallelism", i, line.strip()))

    return issues


def lint_file(path: str) -> list[LintIssue]:
    with open(path, encoding="utf-8") as f:
        return lint_text(f.read())


def lint_resume_json_text(resume_json: dict) -> list[LintIssue]:
    """voice-dna lint over a v2 resume.json's actual prose fields (tagline,
    summary, every experience bullet) — one per line, so line numbers in the
    report are meaningful. Skipping `skills` (short keyword phrases, not
    prose the voice-dna rules meaningfully apply to)."""
    lines = [resume_json.get("tagline", ""), resume_json.get("summary", "")]
    for exp in resume_json.get("experience", []):
        lines.extend(exp.get("bullets", []))
    return lint_text("\n".join(lines))


def format_issues(issues: list[LintIssue]) -> str:
    if not issues:
        return "OK — no voice-dna violations found."
    lines = [f"{len(issues)} voice-dna issue(s) found:"]
    for issue in issues:
        lines.append(f"  line {issue.line} [{issue.kind}]: {issue.snippet}")
    return "\n".join(lines)


# ── Resume truthfulness: deterministic verbatim-bullet enforcement ────────
#
# "Selector, not writer" (prompts/resume_v1.md) is only as safe as its
# enforcement. Career Ops enforces this with plan-schema.mjs + plan-lint.mjs
# checking a Plan JSON before assembly ever happens. CareerOS skips the
# Plan-JSON step (collapsed machinery, per the architecture decision), so the
# check instead runs on the FINISHED resume: every bullet/summary line must be
# an exact (whitespace-normalized) match of a profile.yaml bullet or summary
# variant. Anything else is a truthfulness violation — the resume prompt may
# select and reorder facts, never invent new sentences.

# A leading presentational label before the actual fact text, e.g.
# "**Rizent AI**: ..." or "Rizent AI: ...". Stripped before comparison since
# the label itself isn't a profile claim — only the text after it is.
_LABEL_PREFIX_RE = re.compile(r"^(\*\*[^*]+\*\*|[^:]{1,60}):\s*")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _collect_allowed_texts(profile) -> set[str]:
    allowed: set[str] = set()
    for variant in getattr(profile, "summary_variants", []) or []:
        allowed.add(_normalize(variant["text"]))
    for exp in getattr(profile, "experience", []) or []:
        for bullet in exp.bullets:
            allowed.add(_normalize(bullet.text))
    for proj in getattr(profile, "projects", []) or []:
        for bullet in proj.get("bullets", []):
            allowed.add(_normalize(bullet["text"]))
    return allowed


def _extract_bullet_lines(resume_md: str) -> list[str]:
    """Every markdown bullet line ('- ...') in the document, raw (no label
    stripped yet — see _matches_allowed for the label-stripped fallback)."""
    bullets = []
    for line in resume_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _matches_allowed(text: str, allowed: set[str]) -> bool:
    """True if `text` verbatim-matches an allowed fact, either as-is or after
    stripping a leading presentational label (e.g. '**Rizent AI**: ' or
    'Rizent AI: ') — the label itself isn't a profile claim, only what
    follows it is. Only falls back to the stripped form when the raw text
    doesn't already match, so a bullet whose OWN verbatim text happens to
    contain an early colon is still checked correctly."""
    if _normalize(text) in allowed:
        return True
    stripped = _LABEL_PREFIX_RE.sub("", text, count=1)
    return stripped != text and _normalize(stripped) in allowed


def _extract_summary(resume_md: str) -> str | None:
    lines = resume_md.splitlines()
    in_summary = False
    collected = []
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.strip().startswith("##"):
                break
            if line.strip():
                collected.append(line.strip())
    return " ".join(collected) if collected else None


def verify_resume_bullets(resume_md: str, profile) -> list[str]:
    """Returns a list of truthfulness-violation descriptions (empty = clean).
    Every bullet and the summary paragraph must be a verbatim match (modulo
    whitespace) of something actually in profile.yaml."""
    allowed = _collect_allowed_texts(profile)
    issues: list[str] = []

    summary = _extract_summary(resume_md)
    if summary and _normalize(summary) not in allowed:
        issues.append(f"Summary does not verbatim-match any profile.yaml summary_variants: \"{summary[:100]}...\"")

    for bullet in _extract_bullet_lines(resume_md):
        if not _matches_allowed(bullet, allowed):
            issues.append(f"Bullet not found verbatim in profile.yaml: \"{bullet[:100]}\"")

    return issues


# ── Resume truthfulness v2: fact-preserving reword enforcement ────────────
#
# v2 (resume_v2.md, prompts/typst_render.py) loosens "verbatim copy" to
# "reword the language, preserve every fact" — the LLM may mirror a JD's
# keywords in its own phrasing, but every hard NUMBER (a metric, percentage,
# dollar amount, headcount) in a profile bullet must survive into its
# reworded form, and the reword must never introduce a number that isn't
# anywhere in that company's canonical bullets. This is the deterministic
# backstop for that contract — it does not (and structurally cannot) verify
# that non-numeric entities/technologies are preserved with equal rigor;
# that remains the prompt's + a human spot-check's job. It also enforces
# job-agent's company-name-leak rule: the resume must never name the
# specific employer being applied to.

_NUMBER_RE = re.compile(
    r"[$₹€£¥]?\s?\d[\d,]*\.?\d*\s?(?:%|\+|k|K|L|Cr|crore|lakh)?"
)


def _extract_numbers(text: str) -> set[str]:
    """Numeric-fact tokens (metrics, percentages, currency amounts, counts),
    normalized by stripping whitespace so '55' and '55 ' compare equal."""
    return {re.sub(r"\s+", "", m.group()) for m in _NUMBER_RE.finditer(text)}


def _company_name_tokens(company: str) -> list[str]:
    company = (company or "").strip()
    if not company:
        return []
    tokens = [company]
    first = company.split()[0] if company.split() else ""
    if len(first) >= 4 and first.lower() != company.lower():
        tokens.append(first)
    return tokens


def _contains_company_name(text: str, company: str) -> bool:
    return any(
        re.search(rf"\b{re.escape(tok)}\b", text, re.IGNORECASE)
        for tok in _company_name_tokens(company)
    )


def verify_resume_facts(
    resume_json: dict, profile, target_company: str | None = None
) -> list[str]:
    """Returns a list of truthfulness-violation descriptions (empty = clean)
    for the v2 (reword-preserving-facts) content model. `resume_json` is the
    tailoring payload (see schemas/resume.schema.json): {tagline, summary,
    companies: [...], experience: [{company, bullets}], skills: [...],
    projects: [{name}]}.

    Checks, per experience entry:
    - every number in a reworded bullet must appear somewhere in that
      company's canonical profile.yaml bullets (no invented metric);
    - no reworded bullet, the tagline, or the summary may contain the
      target company's name (job-agent's transferable-language rule).

    Also checks that every name in `companies` and `projects` is a real
    profile.yaml entry — a typo here doesn't fail schema validation (it's
    still valid JSON matching the shape), but silently drops content: a
    misspelled `companies` entry means the matching profile.yaml company
    never gets included (careeros/typst_render.py's `build_render_data`
    filters profile.experience down to exact-string matches in `companies`),
    and a misspelled `projects[].name` means that project is filtered out of
    `selected_projects` the same way. Both fail-soft to "content vanishes",
    not to an error — so this check is the only thing that catches it.
    """
    issues: list[str] = []
    profile_bullets_by_company = {
        exp.company: [b.text for b in exp.bullets]
        for exp in getattr(profile, "experience", []) or []
    }
    known_companies = set(profile_bullets_by_company)
    known_projects = {p.get("name") for p in getattr(profile, "projects", []) or []}

    for name in resume_json.get("companies", []):
        if name not in known_companies:
            issues.append(
                f"companies: \"{name}\" does not match any profile.yaml experience[].company "
                f"— this company will be silently dropped from the resume, not included"
            )

    for proj in resume_json.get("projects", []):
        name = proj.get("name", "")
        if name not in known_projects:
            issues.append(
                f"projects: \"{name}\" does not match any profile.yaml projects[].name "
                f"— this project will be silently dropped from the resume, not included"
            )

    for entry in resume_json.get("experience", []):
        company = entry.get("company", "")
        canonical_bullets = profile_bullets_by_company.get(company)
        if canonical_bullets is None:
            issues.append(f"Unknown company in resume.json (not in profile.yaml): \"{company}\"")
            continue
        canonical_numbers: set[str] = set()
        for b in canonical_bullets:
            canonical_numbers |= _extract_numbers(b)

        for bullet in entry.get("bullets", []):
            invented = _extract_numbers(bullet) - canonical_numbers
            if invented:
                issues.append(
                    f"{company}: reworded bullet introduces number(s) not present in any "
                    f"profile.yaml bullet for this company {sorted(invented)}: \"{bullet[:100]}\""
                )
            if target_company and _contains_company_name(bullet, target_company):
                issues.append(
                    f"{company}: reworded bullet names the target company "
                    f"(\"{target_company}\"), violating the transferable-language rule: "
                    f"\"{bullet[:100]}\""
                )

    if target_company:
        for field in ("tagline", "summary"):
            value = resume_json.get(field, "")
            if value and _contains_company_name(value, target_company):
                issues.append(
                    f"{field} names the target company (\"{target_company}\"), "
                    f"violating the transferable-language rule: \"{value[:100]}\""
                )

    return issues
