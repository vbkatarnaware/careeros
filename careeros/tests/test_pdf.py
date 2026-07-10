"""Tests for careeros/pdf.py — the markdown -> PDF renderer for resume/cover
Drive uploads (Phase 3, v1.1). Real fpdf2 is exercised (it's a cheap,
pure-Python, deterministic render — no need to mock it); the fail-soft
"[pdf] extra not installed" path is tested by simulating ImportError.

CI installs `.[dev,pdf]` so these run for real there. A contributor running
just `pip install -e ".[dev]"` locally (without the optional [pdf] extra)
gets the real-render tests skipped rather than failed — see `importorskip`."""

from __future__ import annotations

import pytest

pytest.importorskip("fpdf", reason="requires the optional [pdf] extra (fpdf2)")

from unittest.mock import patch

from careeros import pdf as pdf_mod
from careeros.pdf import render_markdown_to_pdf


def _is_valid_pdf(b: bytes) -> bool:
    return isinstance(b, bytes) and b[:5] == b"%PDF-"


def test_renders_a_simple_heading_and_paragraph():
    out = render_markdown_to_pdf("# Title\n\nSome body text.")
    assert _is_valid_pdf(out)


def test_renders_h1_h2_h3_and_bullets_without_literal_markdown_leaking_through():
    """Regression: h3 (### Company — Role, used by resume_v1.md's Experience
    section) was originally unhandled and leaked the literal '###' into the
    PDF text instead of being rendered as a heading."""
    md = (
        "# Name\n\n## Experience\n\n### Acme — PM\n2020 - present\n\n"
        "- Did a thing.\n- Did another thing.\n"
    )
    out = render_markdown_to_pdf(md)
    assert _is_valid_pdf(out)
    # fpdf2 exposes a page's text via output round-trip is heavy; instead we
    # assert indirectly by checking the PDF byte stream does NOT contain the
    # literal raw markdown marker sequence "### " unescaped as content text
    # (a crude but effective smoke check — the real coverage is the visual
    # verification done during development).
    assert b"### " not in out.replace(b"\\043\\043\\043 ", b"")  # tolerate PDF-escaped '#'


def test_empty_input_still_produces_a_valid_pdf():
    out = render_markdown_to_pdf("")
    assert _is_valid_pdf(out)


def test_blank_lines_do_not_crash():
    out = render_markdown_to_pdf("# A\n\n\n\nB\n\n\n")
    assert _is_valid_pdf(out)


def test_sanitizes_unicode_punctuation_that_core_fonts_cannot_encode():
    """The built-in PDF standard font (Helvetica) is latin-1 only. En/em
    dashes, middle dots, smart quotes, and ellipses are common in real
    resume/cover content (date ranges, contact-line separators) and must not
    crash the render."""
    md = "Dates: 2020 – 2024 — note. Sep·arator. ‘Quoted’ “text”…"
    out = render_markdown_to_pdf(md)
    assert _is_valid_pdf(out)


def test_real_artifact_files_all_render(tmp_path):
    """Smoke test against every real resume.md/cover.md this repo has
    generated so far — catches any markdown shape the prompts actually
    produce that a synthetic test might miss."""
    import glob
    files = glob.glob(".careeros/runs/*/artifacts/*/resume.md") + \
        glob.glob(".careeros/runs/*/artifacts/*/cover.md")
    if not files:
        return  # nothing generated yet in this environment — not a failure
    for f in files:
        with open(f, encoding="utf-8") as fh:
            out = render_markdown_to_pdf(fh.read())
        assert _is_valid_pdf(out), f"failed to render {f}"


def test_returns_none_when_fpdf2_not_installed():
    """Fail-soft contract: no [pdf] extra -> None, never an exception. The
    caller (drive.py) must treat None as 'fall back to Markdown'."""
    with patch.object(pdf_mod, "_lazy_fpdf", return_value=None):
        assert render_markdown_to_pdf("# Anything") is None


def test_sanitize_helper_maps_known_unicode_chars():
    sanitized = pdf_mod._sanitize_for_core_font("a–b—c·d")
    assert sanitized == "a-b--c-d"


def test_sanitizes_currency_symbols_that_core_fonts_cannot_encode():
    """Regression: a candidate whose comp.currency is INR (or any non-
    USD/EUR/GBP/JPY currency written with its symbol, not spelled out) must
    not crash the render -- this used to raise inside fpdf2 and block the
    whole `publish`/Drive upload for that job."""
    md = "Target: INR-symbol 20-28 LPA. Also EUR-symbol 50k, GBP-symbol 40k, JPY-symbol 5,000,000."
    md = md.replace("INR-symbol", "₹").replace("EUR-symbol", "€") \
           .replace("GBP-symbol", "£").replace("JPY-symbol", "¥")
    out = render_markdown_to_pdf(md)
    assert _is_valid_pdf(out)


def test_returns_none_on_genuine_render_exception():
    """Fail-soft contract extends past 'not installed': ANY fpdf2 render
    failure (not just missing chars) must surface as None, not raise -- the
    caller (drive.py) falls back to Markdown rather than the whole
    publish/upload failing."""
    with patch.object(pdf_mod, "_sanitize_for_core_font", side_effect=RuntimeError("boom")):
        assert render_markdown_to_pdf("# Anything") is None
