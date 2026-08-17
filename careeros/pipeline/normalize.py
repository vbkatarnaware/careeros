"""Stage: normalize. Deterministic. Zero AI, zero tokens.

Converts every provider's raw record (via that provider's `to_job_dict`)
into the one common `Job` shape every later stage depends on. This is where
`ats` gets derived from the apply URL's domain (so `apply` never has to
re-detect it), and where `description` gets truncated to a token-conscious
length before any AI stage ever sees it — and, v2.2, where any work-auth-
exclusion sentence living in the part about to be truncated away is
recovered into `Job.eligibility_note` first, via
`careeros.pipeline.constraints.extract_eligibility_note`, so
`constraints.py`'s deterministic hard-reject can still see it.
"""

from __future__ import annotations

from typing import Any

from careeros.models import Contact, Job, Salary
from careeros.pipeline.constraints import extract_eligibility_note

# Domain -> ATS id. Checked against the apply_url's host. Extend this map as
# new ATS platforms are encountered; unmatched domains fall back to "custom".
ATS_DOMAIN_MAP = {
    "greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "jobs.lever.co": "lever",
    "ashbyhq.com": "ashby",
    "jobs.ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
    "workday.com": "workday",
}


def detect_ats(apply_url: str) -> str:
    for domain, ats in ATS_DOMAIN_MAP.items():
        if domain in apply_url:
            return ats
    return "custom"


def normalize_one(
    raw: dict[str, Any],
    provider,
    *,
    source: str,
    description_max_chars: int = 4000,
    raw_index: int | None = None,
    tiers: list[str] | None = None,
) -> Job | None:
    """Normalize one raw provider record into a Job. Returns None if the
    provider's own mapper rejects the record (missing title/URL).

    `tiers` (v2.0): the query tier(s) that surfaced this raw item — see
    raw.json's `provenance` (careeros/providers/base.py's ProviderResult.tiers
    docstring). Purely descriptive metadata for the learning ledger; never
    affects `Job.content_hash()` or anything gate/evaluate reads."""
    mapped = provider.to_job_dict(raw)
    if mapped is None:
        return None

    description = mapped.get("description")
    # v2.2: recover any work-auth-exclusion sentence(s) from the part of the
    # FULL description about to be discarded below, BEFORE truncating — see
    # extract_eligibility_note's docstring (careeros/pipeline/constraints.py)
    # for why: a real audit found eligibility language sits at a median 84%
    # depth in a JD, so description_max_chars was silently discarding it for
    # 72% of postings.
    eligibility_note = extract_eligibility_note(description, description_max_chars)
    if description and len(description) > description_max_chars:
        description = description[:description_max_chars].rstrip() + "…"

    apply_url = mapped["apply_url"]
    job_id = Job.make_id(source, mapped["company"], mapped["title"], mapped.get("location"))

    salary_dict = mapped.get("salary")
    contact_dict = mapped.get("contact")

    return Job(
        id=job_id,
        source=source,
        title=mapped["title"],
        company=mapped["company"],
        apply_url=apply_url,
        location=mapped.get("location"),
        remote=mapped.get("remote"),
        seniority=mapped.get("seniority"),
        employment_type=mapped.get("employment_type"),
        description=description,
        ats=detect_ats(apply_url),
        posted_at=mapped.get("posted_at"),
        salary=Salary(**salary_dict) if salary_dict else None,
        contact=Contact(**contact_dict) if contact_dict else None,
        company_linkedin=mapped.get("company_linkedin"),
        raw_ref=f"01_discover/raw.json#{raw_index}" if raw_index is not None else None,
        tiers=list(tiers) if tiers else None,
        eligibility_note=eligibility_note,
    )


def normalize_all(
    raw_records: list[dict[str, Any]],
    provider,
    *,
    source: str,
    description_max_chars: int = 4000,
    provenance: list[list[str]] | None = None,
) -> list[Job]:
    """`provenance` (v2.0): index-aligned with `raw_records`, from raw.json's
    top-level `provenance[source]` — see normalize_one's docstring. Optional
    and defaults to None so any external caller/test that doesn't care about
    tier attribution keeps working unchanged."""
    jobs: list[Job] = []
    for i, raw in enumerate(raw_records):
        item_tiers = provenance[i] if provenance is not None and i < len(provenance) else None
        job = normalize_one(
            raw, provider, source=source,
            description_max_chars=description_max_chars, raw_index=i,
            tiers=item_tiers,
        )
        if job is not None:
            jobs.append(job)
    return jobs
