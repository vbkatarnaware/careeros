"""Data contracts.

These dataclasses mirror schemas/*.schema.json. The JSON Schema files are the
actual source of truth used to validate stage output on disk (see
`careeros.runmeta.validate_stage`); these dataclasses exist so the Python
pipeline stages get type-checking and IDE support while building/reading the
same shape. Keep both in sync when either changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def stable_hash(*parts: str) -> str:
    """sha1 over pipe-joined parts. Used for Job.id and every cache fingerprint.

    Deterministic and order-sensitive by design: callers must pass parts in a
    fixed, documented order (see call sites) so the same logical input always
    hashes to the same id.
    """
    joined = "|".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


@dataclass
class Contact:
    name: Optional[str] = None
    linkedin: Optional[str] = None
    email: Optional[str] = None


@dataclass
class Salary:
    min: Optional[float] = None
    max: Optional[float] = None
    currency: Optional[str] = None
    unit: Optional[str] = None  # year | month | week | day | hour | None


@dataclass
class Job:
    """The universal provider contract. See schemas/job.schema.json."""

    id: str
    source: str
    title: str
    company: str
    apply_url: str
    location: Optional[str] = None
    remote: Optional[bool] = None
    seniority: Optional[str] = None
    employment_type: Optional[str] = None
    description: Optional[str] = None
    ats: Optional[str] = None
    salary: Optional[Salary] = None
    posted_at: Optional[str] = None
    contact: Optional[Contact] = None
    raw_ref: Optional[str] = None

    @staticmethod
    def make_id(source: str, company: str, title: str, location: Optional[str]) -> str:
        return stable_hash(source, company, title, location or "")

    def content_hash(self) -> str:
        """Fingerprint of everything that could change an evaluation's answer.

        Deliberately excludes `raw_ref`/`contact` (operational metadata, not
        fit-relevant) so a contact-enrichment pass never busts the eval cache.

        `salary` is also excluded ON PURPOSE (reviewed in P2.2, kept out): it's
        a noisy AI-extracted field, the full `description` it's derived from is
        already hashed here, and the hard salary deal-breaker is re-checked live
        every run in pipeline/constraints.py (never from the cached eval). So
        including it would only add cache misses — more AI spend — for a
        marginal refresh of a 10%-weighted logistics sub-score. Not worth it.
        """
        return stable_hash(
            self.title, self.company, self.location or "",
            self.description or "", str(self.seniority), str(self.employment_type),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        salary = Salary(**d["salary"]) if d.get("salary") else None
        contact = Contact(**d["contact"]) if d.get("contact") else None
        kwargs = {k: v for k, v in d.items() if k not in ("salary", "contact")}
        return Job(salary=salary, contact=contact, **kwargs)


@dataclass
class Rubric:
    role_fit: float
    seniority_fit: float
    skills_match: float
    domain: float
    logistics: float


@dataclass
class Eval:
    """The final evaluation. Written ONCE by `evaluate`. See schemas/eval.schema.json.

    This is source of truth #2 (alongside Profile). Every later artifact
    (daily report, resume, cover, deep report, application answers) reads
    this file and must never recompute score/confidence/recommendation.
    """

    id: str
    score: float
    confidence: float
    recommendation: str  # "apply" | "skip"
    strengths: list[str]
    weaknesses: list[str]
    ats_keywords: list[str]
    company_summary: str
    fit_paragraph: str
    rubric: Rubric
    prompt_version: str
    profile_version: int
    job_hash: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Eval":
        rubric = Rubric(**d["rubric"])
        kwargs = {k: v for k, v in d.items() if k != "rubric"}
        return Eval(rubric=rubric, **kwargs)


@dataclass
class ProfileBullet:
    text: str
    tags: list[str]
    visibility: str  # "headline" | "supporting" | "hidden"


@dataclass
class ProfileExperience:
    company: str
    role: str
    bullets: list[ProfileBullet]
    location: Optional[str] = None
    dates: Optional[dict] = None


@dataclass
class Profile:
    """The candidate's facts. Source of truth #1. See schemas/profile.schema.json.

    `version` is part of every downstream cache fingerprint (Eval.profile_version,
    resume/cover cache keys) — bump it whenever facts change and every derivation
    that depends on the changed fact recomputes; everything else stays cached.
    """

    version: int
    candidate: dict
    headline: str
    targets: list[str]
    experience: list[ProfileExperience]
    deal_breakers: dict = field(default_factory=dict)
    location: dict = field(default_factory=dict)
    comp: dict = field(default_factory=dict)
    role_priorities: list[str] = field(default_factory=list)
    ranking_notes: str = ""
    work_mode_priority: list[str] = field(default_factory=list)
    summary_variants: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    education: list[dict] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        experience = [
            ProfileExperience(
                company=e["company"], role=e["role"],
                location=e.get("location"), dates=e.get("dates"),
                bullets=[ProfileBullet(**b) for b in e.get("bullets", [])],
            )
            for e in d.get("experience", [])
        ]
        kwargs = {k: v for k, v in d.items() if k != "experience"}
        return Profile(experience=experience, **kwargs)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def dumps(obj: Any) -> str:
    """Canonical JSON serialization for anything written to the run-dir.

    Sorted keys + fixed separators so byte-identical content always produces
    byte-identical output — this is what makes file diffs and content hashing
    reliable across runs.
    """
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
