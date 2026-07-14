"""Typst-based PDF rendering for resume/cover artifacts.

Deterministic, zero AI at render time. Pure-Python via the optional `typst`
dependency (the `[resume]`/`[drive]` extra) — `typst` bundles its own
compiler binary (Apache-2.0, https://github.com/messense/typst-py), so this
installs and runs the same on every OS and in CI, with no LaTeX/pango/browser
system dependency. Both templates use "New Computer Modern", a font Typst
ships inside its own compiler binary — no font files are bundled with this
package and no font_paths are needed.

The renderer MERGES two inputs and hands the result to `templates/resume.typ`:
  - canonical facts (name, contact, company, title, dates, location,
    education) come straight from `profile.yaml` via the typed `Profile`
    model — the tailoring JSON below can never alter them;
  - tailoring (tagline, summary, reworded-but-fact-preserving experience
    bullets, which companies/projects to include, skills selection/order)
    comes from the AI content step (`resume.json`, see prompts/resume_v2.md).

This split is the actual anti-hallucination guarantee: a bug or prompt
regression in the tailoring step can misword a bullet, but it structurally
cannot invent a company, a date, or a degree, because those never pass
through the model at all.

Fail-soft by design, matching `careeros/pdf.py`'s contract: `render_resume_pdf()`
returns `None` if `typst` isn't installed or rendering fails for any reason,
never raises. Callers fall back to the legacy Markdown/fpdf2 path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Profile

_RESUME_TEMPLATE_PATH = Path(__file__).parent / "templates" / "resume.typ"
_COVER_TEMPLATE_PATH = Path(__file__).parent / "templates" / "cover.typ"

_MONTHS = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}

# Page-density auto-fit: render_resume_pdf() tries these largest-first and
# keeps the first that fits on exactly one page, so lighter-content resumes
# render bigger and fuller instead of leaving blank space, and heavier ones
# shrink a notch before ever needing a manual trim. resume.typ's internal
# spacing (line leading, list/section gaps) is all defined in `em`, relative
# to this size, so it scales proportionally too — a bigger tier never means
# cramped lines. Values are bare numbers (pt/mm) — resume.typ attaches the
# unit, since sys.inputs only carries strings. `margin` is the page's
# top/bottom value (resume.typ adds ~2mm for the slightly wider left/right).
_FIT_TIERS = [
    {"size": "11.0", "margin": "15"},
    {"size": "10.6", "margin": "14.5"},
    {"size": "10.2", "margin": "14"},
    {"size": "9.8", "margin": "13.5"},
    {"size": "9.4", "margin": "13"},
    {"size": "9.0", "margin": "12.5"},
    {"size": "8.6", "margin": "12"},
    {"size": "8.2", "margin": "11.5"},
    {"size": "7.8", "margin": "11"},  # last resort before >1 page
]


def _lazy_typst():
    try:
        import typst
    except ImportError:
        return None
    return typst


def _format_date_part(value: str) -> str:
    """'2024-09' -> 'Sep 2024'; '2024' -> '2024'; 'present' -> 'Present'."""
    if not value:
        return ""
    v = value.strip()
    if v.lower() == "present":
        return "Present"
    if "-" in v:
        year, month = v.split("-", 1)
        return f"{_MONTHS.get(month, month)} {year}"
    return v


def _format_date_range(dates: dict | None) -> str:
    if not dates:
        return ""
    start = _format_date_part(dates.get("start", ""))
    end = _format_date_part(dates.get("end", ""))
    if start and end:
        return f"{start} – {end}"
    return start or end


def _format_education_years(dates: dict | None) -> str:
    """Education years are year-only, e.g. '2019–2023' (no month)."""
    if not dates:
        return ""
    start = (dates.get("start") or "").split("-")[0]
    end = (dates.get("end") or "").split("-")[0]
    if start and end:
        return f"{start}–{end}"
    return start or end


def _bare_url(url: str) -> str:
    """Profile stores full URLs; the resume displays the bare handle, matching
    the reference design ("linkedin.com/in/...", not "https://...")."""
    return (url or "").replace("https://", "").replace("http://", "").rstrip("/")


def _contact_line_fields(candidate: dict) -> dict:
    return {
        "name": candidate.get("full_name", ""),
        "location": candidate.get("location") or "",
        "phone": candidate.get("phone") or "",
        "email": candidate.get("email") or "",
        "linkedin": _bare_url(candidate.get("linkedin") or ""),
        "github": _bare_url(candidate.get("github") or ""),
        "portfolio_url": _bare_url(candidate.get("portfolio_url") or ""),
    }


def build_render_data(profile: Profile, tailoring: dict) -> dict[str, Any]:
    """Merge canonical profile facts with AI tailoring into the template's
    input shape. Canonical facts always win; tailoring only supplies the
    zones it's allowed to (tagline, summary, reworded bullets, skill order).
    """
    data: dict[str, Any] = {}
    data.update(_contact_line_fields(profile.candidate))
    data["tagline"] = tailoring.get("tagline") or profile.tagline or ""
    data["summary"] = tailoring.get("summary") or ""

    tailored_experience_by_company = {
        e.get("company"): e.get("bullets", [])
        for e in tailoring.get("experience", [])
        if e.get("company")
    }

    tailored_companies = tailoring.get("companies")
    included_companies = set(tailored_companies) if tailored_companies else None

    experience = []
    for exp in profile.experience:
        if included_companies is not None and exp.company not in included_companies:
            # Intentional exclusion: this JD's tailoring picked which
            # companies to show (same selector-not-writer rule as
            # `projects`), and this one wasn't picked.
            continue
        bullets = tailored_experience_by_company.get(exp.company)
        if not bullets:
            # Fail-soft fallback: no tailoring for this company (e.g. a
            # partial/failed AI pass) -> use the canonical bullets verbatim
            # rather than dropping the company from the resume.
            bullets = [
                b.text for b in exp.bullets if b.visibility != "hidden"
            ][:4]
        experience.append({
            "company": exp.company,
            "role": exp.role,
            "location": exp.location or "",
            "dates": _format_date_range(exp.dates),
            "bullets": bullets,
        })
    data["experience"] = experience

    tailored_project_names = [
        p.get("name") for p in tailoring.get("projects", []) if p.get("name")
    ]
    if tailored_project_names:
        projects_by_name = {p.get("name"): p for p in profile.projects}
        selected_projects = [
            projects_by_name[name]
            for name in tailored_project_names
            if name in projects_by_name
        ]
    else:
        # Fail-soft fallback: no project tailoring (e.g. legacy resume.json
        # or a partial AI pass) -> include every profile project, matching
        # pre-selection behavior rather than silently dropping them all.
        selected_projects = profile.projects

    data["projects"] = [
        {
            "name": p.get("name", ""),
            "url": p.get("url"),
            "tagline": p.get("tagline") or "",
            "bullets": [
                b["text"] for b in p.get("bullets", [])
                if b.get("visibility") != "hidden"
            ],
        }
        for p in selected_projects
    ]

    tailored_skills = tailoring.get("skills")
    if tailored_skills:
        data["skills"] = [
            {"category": s.get("category", ""), "items": s.get("items", [])}
            for s in tailored_skills
        ]
    else:
        # Fail-soft fallback: group by the profile's own `category` field.
        by_category: dict[str, list[str]] = {}
        for skill in profile.skills:
            if skill.get("visibility") == "hidden":
                continue
            cat = skill.get("category") or "Other"
            by_category.setdefault(cat, []).append(skill["name"])
        data["skills"] = [
            {"category": cat.title(), "items": items}
            for cat, items in by_category.items()
        ]

    data["education"] = [
        {
            "degree": e.get("degree", ""),
            "institution": e.get("institution", ""),
            "score": e.get("score") or "",
            "years": _format_education_years(e.get("dates")),
        }
        for e in profile.education
    ]

    return data


def render_resume_pdf(profile: Profile, tailoring: dict) -> bytes | None:
    """Render a one-page resume PDF from profile facts + AI tailoring JSON.

    Tries a small set of font-size/margin presets from most-generous to
    most-compact (`_FIT_TIERS`), keeping the first one that renders to
    exactly one page. This is the page-density mechanism: a resume with less
    content renders larger and fuller instead of leaving blank space, while
    one with more content shrinks a notch before ever needing a manual trim.

    Returns None if `typst` isn't installed, or if rendering fails for any
    reason (a malformed tailoring dict, a Typst compile error) — callers
    must treat that as "fall back to the legacy renderer", not an error,
    matching careeros/pdf.py's fail-soft contract."""
    typst = _lazy_typst()
    if typst is None:
        return None

    try:
        data = build_render_data(profile, tailoring)
        template_src = _RESUME_TEMPLATE_PATH.read_bytes()
        data_json = json.dumps(data)

        last_pdf_bytes: bytes | None = None
        for tier in _FIT_TIERS:
            pdf_bytes = bytes(typst.compile(
                input=template_src,
                format="pdf",
                sys_inputs={"data": data_json, "fit": json.dumps(tier)},
            ))
            last_pdf_bytes = pdf_bytes
            if pdf_page_count(pdf_bytes) == 1:
                return pdf_bytes
        # No tier fit on one page — return the most compact attempt anyway;
        # cli.py's finalize-time ">1 page" gate is the true last resort and
        # will report it so the resume.json can be trimmed by hand.
        return last_pdf_bytes
    except Exception:
        return None


def pdf_page_count(pdf_bytes: bytes) -> int:
    """Page count of a rendered PDF, via the pure-Python `pypdf` (no poppler/
    system binary). Part of the ATS one-page gate — `careeros artifacts
    --finalize` rejects any resume that renders to more than one page."""
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def render_data_to_markdown(data: dict[str, Any]) -> str:
    """Reconstruct a v1-shaped markdown document from the merged render data
    — the last-resort fallback when `typst` itself isn't installed (rare:
    `careeros doctor` FAILs on this, matching the old fpdf2 check). Rendered
    through the legacy `careeros/pdf.py::render_markdown_to_pdf` so a resume
    is never left with literally no PDF, even in a broken install."""
    lines = [f"# {data['name']}", "",
             f"{data['location']} · {data['email']} · {data['linkedin']}", ""]
    if data.get("tagline"):
        lines += [f"*{data['tagline']}*", ""]
    lines += ["## Summary", data.get("summary", ""), "", "## Experience", ""]
    for exp in data["experience"]:
        lines.append(f"### {exp['company']}: {exp['role']}")
        lines.append(exp["dates"])
        lines.extend(f"- {b}" for b in exp["bullets"])
        lines.append("")
    if data.get("projects"):
        lines.append("## Projects")
        for p in data["projects"]:
            suffix = f" ({p['url']})" if p.get("url") else ""
            tagline = f" — {p['tagline']}" if p.get("tagline") else ""
            lines.append(f"- **{p['name']}**{suffix}{tagline}: {' '.join(p['bullets'])}")
        lines.append("")
    lines.append("## Skills")
    for cat in data["skills"]:
        lines.append(f"**{cat['category']}**: {', '.join(cat['items'])}")
    lines += ["", "## Education"]
    for e in data["education"]:
        lines.append(f"- **{e['degree']}**, {e['institution']} — {e['score']} ({e['years']})")
    return "\n".join(lines)


def render_cover_pdf(profile: Profile, cover_markdown: str) -> bytes | None:
    """Render a cover letter PDF matching the resume's font/header design.

    `cover_markdown` is the existing cover.md content (unchanged content
    model — cover letters are still freely written prose, grounded in
    profile.yaml + the eval's fit_paragraph, not verbatim-selected bullets).
    Paragraphs are split on blank lines; markdown syntax is stripped to
    plain prose. Fail-soft, same contract as render_resume_pdf."""
    typst = _lazy_typst()
    if typst is None:
        return None

    try:
        paragraphs = [
            " ".join(block.split())
            for block in cover_markdown.strip().split("\n\n")
            if block.strip()
        ]
        data = _contact_line_fields(profile.candidate)
        data["paragraphs"] = paragraphs
        template_src = _COVER_TEMPLATE_PATH.read_bytes()
        pdf_bytes = typst.compile(
            input=template_src,
            format="pdf",
            sys_inputs={"data": json.dumps(data)},
        )
        return bytes(pdf_bytes)
    except Exception:
        return None
