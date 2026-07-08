"""Stage: normalize. Deterministic. Zero AI, zero tokens.

Converts every provider's raw record (via that provider's `to_job_dict`)
into the one common `Job` shape every later stage depends on. This is where
`ats` gets derived from the apply URL's domain (so `apply` never has to
re-detect it), and where `description` gets truncated to a token-conscious
length before any AI stage ever sees it.
"""

from __future__ import annotations

from typing import Any

from careeros.models import Contact, Job, Salary

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
) -> Job | None:
    """Normalize one raw provider record into a Job. Returns None if the
    provider's own mapper rejects the record (missing title/URL)."""
    mapped = provider.to_job_dict(raw)
    if mapped is None:
        return None

    description = mapped.get("description")
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
    )


def normalize_all(
    raw_records: list[dict[str, Any]],
    provider,
    *,
    source: str,
    description_max_chars: int = 4000,
) -> list[Job]:
    jobs: list[Job] = []
    for i, raw in enumerate(raw_records):
        job = normalize_one(
            raw, provider, source=source,
            description_max_chars=description_max_chars, raw_index=i,
        )
        if job is not None:
            jobs.append(job)
    return jobs
