"""Tests for careeros/typst_render.py — the Typst-based resume/cover PDF
renderer. Real `typst` is exercised (it's a cheap, deterministic,
pure-Python-wrapped compile — no need to mock it); the fail-soft "[resume]
extra not installed" path is tested by simulating ImportError.

CI installs `.[dev,resume]` so these run for real there. A contributor
running just `pip install -e ".[dev]"` locally (without the optional
`[resume]` extra) gets the real-render tests skipped rather than failed —
see `importorskip`."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytest.importorskip("typst", reason="requires the optional [resume] extra (typst)")

from pypdf import PdfReader

from careeros import typst_render as tr
from careeros.models import Profile, ProfileBullet, ProfileExperience
from careeros.typst_render import build_render_data, render_cover_pdf, render_resume_pdf


def _is_valid_pdf(b: bytes) -> bool:
    return isinstance(b, bytes) and b[:5] == b"%PDF-"


def _page_count(b: bytes) -> int:
    import io
    return len(PdfReader(io.BytesIO(b)).pages)


def _extract_text(b: bytes) -> str:
    import io
    reader = PdfReader(io.BytesIO(b))
    return "\n".join(page.extract_text() for page in reader.pages)


def _make_profile(**overrides) -> Profile:
    defaults = dict(
        version=1,
        candidate={
            "full_name": "Test Candidate",
            "email": "test@example.com",
            "phone": "+1-555-0100",
            "location": "Testville, TC",
            "linkedin": "https://www.linkedin.com/in/testcandidate/",
        },
        headline="Test headline",
        tagline="Product | Growth | AI",
        targets=["product-manager"],
        experience=[
            ProfileExperience(
                company="Acme Corp",
                role="Product Manager",
                location="Remote",
                dates={"start": "2022-01", "end": "present"},
                bullets=[
                    ProfileBullet(text="Shipped widget X, growing revenue 40%.",
                                  tags=["product"], visibility="headline"),
                    ProfileBullet(text="Led a team of 5 engineers.",
                                  tags=["leadership"], visibility="supporting"),
                ],
            ),
        ],
        skills=[
            {"name": "SQL", "category": "data", "tags": [], "visibility": "headline"},
        ],
        education=[
            {"degree": "B.Sc", "institution": "Test University",
             "score": "3.8 GPA", "dates": {"start": "2018", "end": "2022"}},
        ],
        projects=[],
    )
    defaults.update(overrides)
    return Profile(**defaults)


@pytest.fixture
def profile():
    return _make_profile()


def test_renders_a_valid_one_page_resume_pdf(profile):
    tailoring = {
        "tagline": "Product Management | AI",
        "summary": "A concise, honest summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    }
    out = render_resume_pdf(profile, tailoring)
    assert _is_valid_pdf(out)
    assert _page_count(out) == 1


def test_rendered_pdf_contains_expected_keywords_and_facts(profile):
    tailoring = {
        "summary": "A concise, honest summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    }
    out = render_resume_pdf(profile, tailoring)
    text = _extract_text(out)
    for expected in ["Test Candidate", "Acme Corp", "Product Manager",
                      "40%", "SQL", "Test University", "B.Sc"]:
        assert expected in text, f"{expected!r} missing from extracted text"


def test_ligatures_are_disabled_workflow_extracts_clean(profile):
    tailoring = {
        "summary": "Built agentic workflow automation for internal office tools.",
        "experience": [{"company": "Acme Corp", "bullets": ["Automated a workflow across the office."]}],
        "skills": [],
    }
    out = render_resume_pdf(profile, tailoring)
    text = _extract_text(out)
    assert "workflow" in text
    assert "office" in text
    assert "workow" not in text  # the classic fi/fl-ligature corruption bug


def test_canonical_facts_survive_even_with_no_tailoring_for_a_company(profile):
    """Fail-soft fallback: if the AI tailoring step is missing a company
    entirely, the canonical profile bullets are used verbatim rather than
    silently dropping that job from the resume."""
    tailoring = {"summary": "Summary.", "experience": [], "skills": []}
    out = render_resume_pdf(profile, tailoring)
    text = _extract_text(out)
    assert "Acme Corp" in text
    assert "Shipped widget X, growing revenue 40%." in text


def test_build_render_data_dates_use_en_dash_not_double_hyphen(profile):
    data = build_render_data(profile, {})
    assert data["experience"][0]["dates"] == "Jan 2022 – Present"
    assert "--" not in data["experience"][0]["dates"]


def test_build_render_data_education_years_are_year_only(profile):
    data = build_render_data(profile, {})
    assert data["education"][0]["years"] == "2018–2022"


def test_tagline_falls_back_to_profile_tagline_when_tailoring_omits_it(profile):
    data = build_render_data(profile, {"summary": "s"})
    assert data["tagline"] == "Product | Growth | AI"


def test_renders_a_valid_cover_letter_pdf(profile):
    md = "Dear Hiring Team,\n\nI am excited to apply.\n\nBest,\nTest Candidate"
    out = render_cover_pdf(profile, md)
    assert _is_valid_pdf(out)
    text = _extract_text(out)
    assert "Test Candidate" in text
    assert "excited to apply" in text


def test_returns_none_when_typst_not_installed(profile):
    with patch.object(tr, "_lazy_typst", return_value=None):
        assert render_resume_pdf(profile, {}) is None
        assert render_cover_pdf(profile, "text") is None


def _profile_with_links(**overrides):
    candidate = {
        "full_name": "Test Candidate",
        "email": "test@example.com",
        "phone": "+1-555-0100",
        "location": "Testville, TC",
        "linkedin": "https://www.linkedin.com/in/testcandidate/",
        "github": "https://github.com/testcandidate",
        "portfolio_url": "https://testcandidate.dev",
    }
    return _make_profile(candidate=candidate, **overrides)


def test_build_render_data_includes_github_and_portfolio_when_present():
    data = build_render_data(_profile_with_links(), {})
    assert data["github"] == "github.com/testcandidate"
    assert data["portfolio_url"] == "testcandidate.dev"


def test_build_render_data_contact_links_empty_when_absent(profile):
    """The default fixture profile has no github/portfolio_url on file —
    confirms the fields fail-soft to empty strings rather than raising."""
    data = build_render_data(profile, {})
    assert data["github"] == ""
    assert data["portfolio_url"] == ""


def test_rendered_pdf_contains_github_but_not_portfolio_site():
    """GitHub renders as real handle text in the header. The personal-site
    link is deliberately NOT rendered (even though build_render_data still
    computes it as a fact) -- it was dropped to keep the contact line to one
    row; see resume.typ's header comment for the ATS-parsing reasoning."""
    out = render_resume_pdf(_profile_with_links(), {
        "summary": "Summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    })
    text = _extract_text(out)
    assert "github.com/testcandidate" in text
    assert "testcandidate.dev" not in text


def test_renders_a_valid_pdf_when_no_github_or_portfolio_on_file(profile):
    """No dangling separator or crash when a profile has neither link."""
    out = render_resume_pdf(profile, {
        "summary": "Summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    })
    assert _is_valid_pdf(out)
    assert _page_count(out) == 1


def _profile_with_companies():
    experience = [
        ProfileExperience(
            company="Acme Corp", role="Product Manager", location="Remote",
            dates={"start": "2022-01", "end": "present"},
            bullets=[ProfileBullet(text="Shipped widget X, growing revenue 40%.",
                                    tags=[], visibility="headline")],
        ),
        ProfileExperience(
            company="Old Co", role="Associate", location="Remote",
            dates={"start": "2019-01", "end": "2021-12"},
            bullets=[ProfileBullet(text="Ran onboarding for 30 clients.",
                                    tags=[], visibility="headline")],
        ),
        ProfileExperience(
            company="Internship Inc", role="Intern", location="Remote",
            dates={"start": "2018-06", "end": "2018-08"},
            bullets=[ProfileBullet(text="Built a reporting script.",
                                    tags=[], visibility="headline")],
        ),
    ]
    return _make_profile(experience=experience)


def test_build_render_data_selects_tailored_companies():
    """Company selection is a tailoring zone, same as projects: only the
    named subset appears, dropping the rest of profile.yaml's companies."""
    profile = _profile_with_companies()
    data = build_render_data(profile, {
        "companies": ["Acme Corp"],
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
    })
    assert [e["company"] for e in data["experience"]] == ["Acme Corp"]


def test_build_render_data_falls_back_to_all_companies_when_tailoring_omits_companies():
    """Fail-soft: legacy/partial tailoring with no `companies` key includes
    every profile company rather than silently dropping any of them."""
    profile = _profile_with_companies()
    data = build_render_data(profile, {})
    assert [e["company"] for e in data["experience"]] == ["Acme Corp", "Old Co", "Internship Inc"]


def _profile_with_projects():
    projects = [
        {"name": "Alpha", "url": "https://a.example",
         "tagline": "Your AI co-founder for widgets.",
         "bullets": [{"text": "Alpha bullet.", "visibility": "headline"}]},
        {"name": "Beta", "url": "https://b.example",
         "bullets": [{"text": "Beta bullet.", "visibility": "headline"}]},
        {"name": "Gamma", "url": None,
         "bullets": [{"text": "Gamma bullet.", "visibility": "headline"}]},
    ]
    return _make_profile(projects=projects)


def test_build_render_data_includes_project_tagline_when_present():
    """Project taglines are a canonical fact (profile.yaml), merged in
    regardless of what the AI tailoring selects — same tier as company/url."""
    profile = _profile_with_projects()
    data = build_render_data(profile, {"projects": [{"name": "Alpha"}]})
    assert data["projects"][0]["tagline"] == "Your AI co-founder for widgets."


def test_build_render_data_project_tagline_empty_when_absent():
    """Fail-soft: a project with no `tagline` in profile.yaml (e.g. Beta,
    above) gets an empty string, not a KeyError or None leaking into Typst."""
    profile = _profile_with_projects()
    data = build_render_data(profile, {"projects": [{"name": "Beta"}]})
    assert data["projects"][0]["tagline"] == ""


def test_rendered_pdf_contains_project_tagline():
    profile = _profile_with_projects()
    out = render_resume_pdf(profile, {
        "summary": "Summary.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
        "projects": [{"name": "Alpha"}],
    })
    text = _extract_text(out)
    assert "Your AI co-founder for widgets." in text


def test_build_render_data_selects_tailored_projects_by_name():
    """Project selection is a tailoring zone: only the named subset appears,
    in the tailoring's own order — never every profile project."""
    profile = _profile_with_projects()
    data = build_render_data(profile, {"projects": [{"name": "Gamma"}, {"name": "Alpha"}]})
    assert [p["name"] for p in data["projects"]] == ["Gamma", "Alpha"]


def test_build_render_data_falls_back_to_all_projects_when_tailoring_omits_projects():
    """Fail-soft: legacy/partial tailoring with no `projects` key includes
    every profile project rather than silently dropping them all."""
    profile = _profile_with_projects()
    data = build_render_data(profile, {})
    assert [p["name"] for p in data["projects"]] == ["Alpha", "Beta", "Gamma"]


class _RecordingTypst:
    """Wraps the real `typst` module, recording each `fit` tier it's asked to
    compile with — lets tests assert which/how many _FIT_TIERS were tried
    without mocking away the real render."""

    def __init__(self, real_typst):
        self._real = real_typst
        self.fit_calls: list[dict] = []

    def compile(self, **kwargs):
        self.fit_calls.append(json.loads(kwargs["sys_inputs"]["fit"]))
        return self._real.compile(**kwargs)


def test_render_resume_pdf_picks_the_most_generous_fit_tier_that_fits(profile):
    """Light content should stop at the first (most generous) tier rather
    than always falling through to the compact tuned-default."""
    real_typst = pytest.importorskip("typst")
    tailoring = {
        "summary": "Short.",
        "experience": [{"company": "Acme Corp", "bullets": ["Shipped widget X, growing revenue 40%."]}],
        "skills": [{"category": "Data", "items": ["SQL"]}],
    }
    recorder = _RecordingTypst(real_typst)
    with patch.object(tr, "_lazy_typst", return_value=recorder):
        out = render_resume_pdf(profile, tailoring)
    assert _is_valid_pdf(out)
    assert _page_count(out) == 1
    assert recorder.fit_calls == [tr._FIT_TIERS[0]]


def test_render_resume_pdf_shrinks_tiers_for_heavy_content():
    """Content too dense for the generous/compact tiers auto-shrinks to a
    smaller tier and still renders exactly one page."""
    experiences = [
        ProfileExperience(
            company=f"Company {i}",
            role="Product Manager",
            location="Remote",
            dates={"start": "2020-01", "end": "2021-01"},
            bullets=[
                ProfileBullet(
                    text=(f"Bullet {i}-{j}: a fairly long, descriptive line of "
                          "accomplishment text meant to fill out a full line "
                          "of the rendered resume for density testing."),
                    tags=[], visibility="headline",
                )
                for j in range(4)
            ],
        )
        for i in range(4)
    ]
    projects = [
        {"name": f"Project {k}", "url": f"https://example.com/{k}",
         "bullets": [{"text": ("A fairly long project bullet describing "
                                "meaningful, verifiable work done end to end."),
                      "visibility": "headline"}]}
        for k in range(3)
    ]
    profile = _make_profile(experience=experiences, projects=projects)
    real_typst = pytest.importorskip("typst")
    recorder = _RecordingTypst(real_typst)
    with patch.object(tr, "_lazy_typst", return_value=recorder):
        out = render_resume_pdf(profile, {})  # fail-soft: every company/project
    assert _is_valid_pdf(out)
    assert _page_count(out) == 1
    assert len(recorder.fit_calls) > 1


def test_returns_none_on_genuine_render_exception(profile):
    """Fail-soft contract extends past 'not installed': ANY typst render
    failure must surface as None, not raise — the caller falls back to the
    legacy fpdf2/markdown path rather than the whole publish/upload failing."""
    with patch.object(tr, "build_render_data", side_effect=RuntimeError("boom")):
        assert render_resume_pdf(profile, {}) is None
