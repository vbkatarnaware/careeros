"""Stage: verify-live (v2.1). Deterministic. Zero AI, zero tokens.

Cross-checks an Apply-tier job's LIVE posting against the two facts the
discovery provider gets wrong most often — years-of-experience and
location/work-arrangement. Pure text analysis over an already-fetched page;
the CLI layer (careeros/cli/verify_live_cmd.py) owns fetching via
`careeros/apply/browser.py`'s existing `fetch_visible_text`.

WHY THIS EXISTS — two real misses on a single day (2026-07-31), both from
trusting provider metadata that turned out to be wrong:

  - MDOTM: the provider's `remote` field said True. The live posting said
    "Milano (Hybrid)". The candidate can only do onsite in Mumbai/Navi
    Mumbai, so this was never applicable — but it was the day's top match,
    and a full resume + cover letter were generated for it.
  - AU Small Finance Bank: the provider's `ai_experience_level` said "2-5"
    and the description it sent was 1,509 characters that never mentioned
    an experience requirement at all. The live posting asks for 7 years.
    The candidate has 3.

Neither was a reasoning failure — both times the pipeline scored honestly
against the data it was handed. That's precisely why a smarter filter or a
better prompt cannot fix this class of bug: the filter would be reading the
same wrong field. The only fix is to look at the real posting.

DESIGN RULE: this stage FLAGS, it never auto-rejects. A fetch can fail, a
page can be a login wall, and a regex can misread prose — none of which are
grounds to silently drop a job the rest of the pipeline ranked highest.
Every finding is surfaced to the candidate to decide on (see
`skills/daily.md`'s Step 7.5 and AGENT_GUIDE.md's Failure Handling
Principle). Cheap by construction: only Apply-tier jobs reach here, which
is 1-3 postings on a typical day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# "5+ years", "5-10 years", "minimum 7 years", "at least 4 yrs", "7+ yrs of
# experience". Deliberately requires the word year/yr NEAR the number so a
# stray "top 5 products" or a salary figure never reads as an experience
# requirement.
# Ranges are matched FIRST and their whole span is then excluded from the
# other patterns. Without that, "5-10 years of experience" matches the range
# form (correctly yielding 5) AND the bare "<n> years ... experience" form
# on its own tail (yielding 10) — which would report the ceiling of a range
# as if it were a second, higher requirement and over-flag the posting.
_YEARS_RANGE_PATTERN = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)", re.I)

_YEARS_PATTERNS = (
    re.compile(r"(\d{1,2})\s*(?:\+|plus)\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?:minimum|min\.?|at\s+least|over)\s*(?:of\s*)?(\d{1,2})\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|prior\s+|total\s+)?experience", re.I),
)

# Only count a years-mention that sits in a requirement-ish context. A JD
# saying "we've been building for 10 years" is company history, not a bar
# on the candidate.
_REQUIREMENT_CONTEXT = re.compile(
    r"experience|require|qualif|must have|looking for|seeking|background|you have|candidate|minimum|expertise",
    re.I,
)
_CONTEXT_WINDOW = 120

_HYBRID_MARKERS = ("hybrid", "in office", "in-office", "onsite", "on-site", "on site")
_REMOTE_MARKERS = ("fully remote", "100% remote", "remote-first", "work from anywhere", "remote (")


@dataclass
class LiveCheck:
    """One Apply-tier job's verification result. `concerns` empty ==
    nothing contradicted the pipeline's own view; it does NOT mean the job
    was positively confirmed (a thin or partially-fetched page also produces
    no concerns — see `fetched`/`fetch_reason`)."""

    job_id: str
    title: str
    company: str
    fetched: bool
    fetch_reason: Optional[str] = None  # a browser.REASON_* when the fetch didn't yield a usable page
    years_found: list[int] = field(default_factory=list)
    max_years_required: Optional[int] = None
    work_arrangement_signals: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "title": self.title, "company": self.company,
            "fetched": self.fetched, "fetch_reason": self.fetch_reason,
            "years_found": self.years_found, "max_years_required": self.max_years_required,
            "work_arrangement_signals": self.work_arrangement_signals,
            "concerns": self.concerns,
        }


def extract_required_years(text: str) -> list[int]:
    """Every years-of-experience requirement stated in the page text, as
    ints. Range forms ("5-10 years") contribute their LOWER bound — that's
    the actual bar to clear, and using the upper bound would overstate how
    exclusionary the posting is."""
    found: list[int] = []
    consumed_spans: list[tuple[int, int]] = []

    def _in_requirement_context(m: re.Match) -> bool:
        start = max(0, m.start() - _CONTEXT_WINDOW)
        end = min(len(text), m.end() + _CONTEXT_WINDOW)
        return bool(_REQUIREMENT_CONTEXT.search(text[start:end]))

    def _record(value: int) -> None:
        if 0 < value <= 30:  # sanity bound: "50 years" is never an experience bar
            found.append(value)

    # Pass 1: ranges. Take the LOWER bound (the actual bar to clear) and
    # mark the whole span consumed so pass 2 can't re-read its ceiling.
    for m in _YEARS_RANGE_PATTERN.finditer(text):
        consumed_spans.append((m.start(), m.end()))
        if not _in_requirement_context(m):
            continue
        try:
            _record(int(m.group(1)))
        except (ValueError, IndexError):
            continue

    # Pass 2: every other form, skipping anything inside a range's span.
    for pattern in _YEARS_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < c_end and m.end() > c_start for c_start, c_end in consumed_spans):
                continue
            if not _in_requirement_context(m):
                continue
            try:
                _record(int(m.group(1)))
            except (ValueError, IndexError):
                continue

    return sorted(set(found))


def detect_work_arrangement(text: str) -> list[str]:
    """Work-arrangement phrases present in the live page, lowercased. Purely
    descriptive — the caller decides whether any of them conflict with the
    candidate's own constraints, since 'hybrid' is disqualifying for one
    candidate and fine for another."""
    lowered = text.lower()
    signals = [m for m in _HYBRID_MARKERS if m in lowered]
    signals += [m for m in _REMOTE_MARKERS if m in lowered]
    return sorted(set(signals))


def check_job(
    *, job_id: str, title: str, company: str, page_text: Optional[str],
    fetch_reason: Optional[str], max_years_ok: int,
    provider_said_remote: Optional[bool], onsite_ok_cities: list[str],
    job_location: Optional[str],
) -> LiveCheck:
    """Compare one live posting against what the pipeline believed.

    `max_years_ok` is the candidate's own ceiling (profile
    `deal_breakers.min_years_ok`) — a posting asking for MORE than this is
    flagged. The comparison is deliberately one-directional: a posting
    asking for fewer years than the candidate has is never a concern."""
    check = LiveCheck(job_id=job_id, title=title, company=company,
                      fetched=page_text is not None, fetch_reason=fetch_reason)

    if page_text is None:
        # Not a concern about the JOB — a concern about our ability to
        # verify it. Named distinctly so the agent reports it honestly
        # rather than implying the posting itself is fine.
        check.concerns.append(
            f"could not read the live posting ({fetch_reason or 'unknown reason'}) — "
            "verify location and years-of-experience by hand before applying"
        )
        return check

    years = extract_required_years(page_text)
    check.years_found = years
    if years:
        check.max_years_required = max(years)
        if check.max_years_required > max_years_ok:
            check.concerns.append(
                f"live posting asks for {check.max_years_required} years of experience "
                f"(your bar is {max_years_ok}) — the job data we fetched did not say this"
            )

    signals = detect_work_arrangement(page_text)
    check.work_arrangement_signals = signals
    hybrid_or_onsite = [s for s in signals if s in _HYBRID_MARKERS]
    if provider_said_remote and hybrid_or_onsite:
        in_accepted_city = any(
            city.strip().lower() in (job_location or "").lower()
            for city in onsite_ok_cities if city.strip()
        )
        if not in_accepted_city:
            check.concerns.append(
                f"job data said REMOTE, but the live posting mentions {', '.join(hybrid_or_onsite)} "
                f"and its location ({job_location or 'unknown'}) is not one of your accepted "
                f"onsite cities ({', '.join(onsite_ok_cities) or 'none set'})"
            )
    return check
