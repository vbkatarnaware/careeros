"""Shared Apify field-mapping helpers, ported from the Career Ops archive's
`providers/_apify.mjs`.

Apify actor output field names are NOT contractually guaranteed and vary by
actor/version ("company" vs "company_name" vs "employer"). The archive's own
hard-won lesson (see providers/apify-indeed.mjs there): never hardcode a
single field name; try a candidate list, verify the real shape live once a
token is configured, and extend the candidate list if the actor renames a
field. This module is that same defensive pattern for CareerOS's providers.
"""

from __future__ import annotations

from typing import Any, Optional

TITLE_KEYS = ["title", "job_title", "jobTitle", "position", "name"]
COMPANY_KEYS = [
    "company", "company_name", "companyName", "employer", "employer_name",
    "employerName", "organization", "organization_name", "organizationName",
]
LOCATION_KEYS = ["location", "job_location", "jobLocation", "city", "candidate_required_location"]
URL_KEYS = [
    "url", "job_url", "jobUrl", "link", "job_link", "jobLink",
    "apply_link", "applyLink", "applyUrl", "apply_url", "jobPostingUrl", "final_url", "href",
]
DESCRIPTION_KEYS = [
    "description", "job_description", "jobDescription",
    "description_text", "descriptionText", "snippet", "descriptionPlain",
]
POSTED_AT_KEYS = [
    "postedAt", "posted_at", "postedDate", "posted_date",
    "datePosted", "date_posted", "publishedAt", "published_at", "date_posted_iso",
]
REMOTE_KEYS = ["remote", "is_remote", "isRemote", "remote_derived"]
SENIORITY_KEYS = ["seniority", "seniority_level", "experience_level"]
EMPLOYMENT_TYPE_KEYS = ["employment_type", "employmentType", "job_type", "type"]


def pick_field(obj: dict[str, Any], candidates: list[str], fallback: str = "") -> str:
    for key in candidates:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return fallback


def pick_bool(obj: dict[str, Any], candidates: list[str]) -> Optional[bool]:
    for key in candidates:
        v = obj.get(key)
        if isinstance(v, bool):
            return v
    return None
