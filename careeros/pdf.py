"""Markdown -> PDF rendering for resume/cover artifacts (Phase 3, v1.1).

Deterministic, zero AI. Pure-Python via the optional `fpdf2` dependency (the
`[pdf]` extra) — no system binaries (no wkhtmltopdf/LaTeX), so it installs
and runs the same on every OS and in CI. A single clean column: headers,
bullet lists, and inline **bold** are handled; nothing fancier is needed for
a resume/cover letter.

Fail-soft by design: `render_markdown_to_pdf()` returns `None` if `fpdf2`
isn't installed, rather than raising — callers (drive.py) fall back to
uploading the Markdown source instead and print a warning. PDF generation
is the mandatory default when the extra IS installed; it is never silently
skipped just because it could be avoided.
"""

from __future__ import annotations

_MARGIN_MM = 18
_FONT = "Helvetica"

# The built-in PDF standard fonts (Helvetica/Times/Courier) only support
# latin-1 — but resume/cover content legitimately contains a small set of
# common Unicode punctuation (en-dashes in date ranges, middle dots as
# separators, smart quotes). Rather than bundling a Unicode TTF font (extra
# asset + license considerations for no real benefit here), map the common
# cases to their closest latin-1-safe equivalent. Anything still outside
# latin-1 after this is replaced with '?' by fpdf2 itself rather than
# crashing the whole render — see `_lazy_fpdf` docstring.
_UNICODE_FALLBACKS = {
    "–": "-",   # en dash
    "—": "--",  # em dash
    "·": "-",   # middle dot
    "‘": "'", "’": "'",   # smart single quotes
    "“": '"', "”": '"',   # smart double quotes
    "…": "...",  # ellipsis
    "•": "-",   # bullet
    " ": " ",   # non-breaking space
}


def _sanitize_for_core_font(text: str) -> str:
    for ch, replacement in _UNICODE_FALLBACKS.items():
        text = text.replace(ch, replacement)
    return text


def _lazy_fpdf():
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    return FPDF


def render_markdown_to_pdf(markdown_text: str) -> bytes | None:
    """Render simple markdown (headers, bullet lists, inline **bold**) to a
    single-column PDF. Returns None if the `[pdf]` extra isn't installed —
    callers must treat that as "fall back to Markdown", not an error."""
    FPDF = _lazy_fpdf()
    if FPDF is None:
        return None

    markdown_text = _sanitize_for_core_font(markdown_text)

    pdf = FPDF(format="A4")
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=_MARGIN_MM)
    pdf.add_page()
    pdf.set_font(_FONT, size=11)

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            pdf.ln(3)
            continue
        if line.startswith("# "):
            pdf.set_font(_FONT, style="B", size=16)
            pdf.multi_cell(0, 8, line[2:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(_FONT, size=11)
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.set_font(_FONT, style="B", size=13)
            pdf.multi_cell(0, 7, line[3:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(_FONT, size=11)
        elif line.startswith("### "):
            pdf.ln(1)
            pdf.set_font(_FONT, style="B", size=11.5)
            pdf.multi_cell(0, 6, line[4:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(_FONT, size=11)
        elif line.startswith("- ") or line.startswith("* "):
            pdf.set_x(_MARGIN_MM + 4)
            pdf.multi_cell(0, 6, f"-  {line[2:].strip()}", new_x="LMARGIN", new_y="NEXT", markdown=True)
        else:
            pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT", markdown=True)

    return bytes(pdf.output())
