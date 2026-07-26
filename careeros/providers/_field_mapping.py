"""Shared field-mapping helpers for provider output.

A job source's output field names are NOT contractually guaranteed and vary
by source/version ("company" vs "company_name" vs "employer"). The lesson
this module encodes: never hardcode a single field name; try a candidate
list, verify the real shape live once a credential is configured, and extend
the candidate list if the source renames a field.

Source-agnostic on purpose — any new provider reuses these lists rather than
re-deriving its own. (Named `_apify_common.py` until v1.7, when the
Apify-backed providers were removed; the pattern itself was never
Apify-specific.)
"""

from __future__ import annotations

from typing import Any

TITLE_KEYS = ["title", "job_title", "jobTitle", "position", "name"]
COMPANY_KEYS = [
    "company", "company_name", "companyName", "employer", "employer_name",
    "employerName", "organization", "organization_name", "organizationName",
]
URL_KEYS = [
    "url", "job_url", "jobUrl", "link", "job_link", "jobLink",
    "apply_link", "applyLink", "applyUrl", "apply_url", "jobPostingUrl", "final_url", "href",
]
DESCRIPTION_KEYS = [
    "description", "job_description", "jobDescription",
    "description_text", "descriptionText", "snippet", "descriptionPlain",
]


def pick_field(obj: dict[str, Any], candidates: list[str], fallback: str = "") -> str:
    for key in candidates:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return fallback
