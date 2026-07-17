// CareerOS resume template. Deterministic, zero AI at render time.
//
// Design: adapted from guided-resume-starter-cgc 2.0.0 (Unlicense, Spencer
// Elkington, https://typst.app/universe/package/guided-resume-starter-cgc) —
// vendored and tightened per-entry (10% pad -> em-based gaps), flipped the
// exp/edu grid to (1fr, auto) so a long role/institution never collides with
// the right-aligned date, combined date+location onto one line (keeps the
// date column the same height as a single-line role, and is simpler for an
// ATS text stream either way), and switched contacts to real literal text
// instead of "Email"/"GitHub"-style link labels. New Computer Modern is
// Typst's own bundled font (same lineage as LaTeX's Latin Modern) — no font
// file ships with this package, and ligatures are disabled for clean ATS
// text extraction ("workflow" never corrupts to "workﬂow").
//
// ATS-safe by construction: single column (the (1fr, auto) grids only
// right-align a date within one row, not a multi-column body layout — the
// content stream still reads top-to-bottom, left-to-right, confirmed
// empirically), standard section headings, real selectable text in reading
// order, no icons/images/text-boxes/headers-footers.
//
// Input: a single JSON blob on sys.inputs.data (see careeros/typst_render.py
// for the exact shape) plus sys.inputs.fit ({size, margin} — see
// typst_render.py's _FIT_TIERS). Canonical facts (name/company/dates/titles/
// education/project taglines) are supplied by the caller from profile.yaml;
// this template never invents or reorders facts, it only lays them out.

#let data = json(bytes(sys.inputs.data))
#let fit = json(bytes(sys.inputs.fit))
#let fit-size = float(fit.size) * 1pt
#let fit-margin = float(fit.margin) * 1mm

// -- Layout primitives (vendored + adapted from guided-resume-starter-cgc) -

#let resume(
  author: "",
  location: "",
  contacts: (),
  tagline: "",
  size: 10pt,
  margin: 1.3cm,
  body,
) = {
  set document(author: author, title: author)
  set text(
    font: "New Computer Modern",
    size: size,
    lang: "en",
    features: (liga: 0, dlig: 0, clig: 0),
    hyphenate: false,
  )
  set page(margin: (top: margin, bottom: margin, left: margin + 0.2cm, right: margin + 0.2cm))
  show link: set text(fill: rgb("#0645AD"))

  show heading: it => [
    #pad(top: 0pt, bottom: -0.55em, [#smallcaps(it.body)])
    #line(length: 100%, stroke: 0.7pt)
  ]

  align(center)[#block(text(weight: 700, 2.0em, smallcaps(author)))]
  pad(top: 0.25em, align(center)[#smallcaps[#contacts.join("  |  ")]])
  if location != "" { align(center)[#smallcaps[#location]] }
  if tagline != "" { align(center)[#pad(top: 0.15em, emph(tagline))] }

  set par(justify: true, leading: 0.52em, spacing: 0.45em)
  set list(marker: [•], body-indent: 0.4em, spacing: 0.32em)
  show heading: set block(above: 0.7em, below: 0.45em)

  body
}

// Compact one-line-per-entry education (institution/degree left, score + date
// + location right) -- closer to a table than CGC's stock two-line block.
#let edu(institution: "", date: "", degrees: (), gpa: "", location: "") = {
  let date-loc = if date != "" and location != "" { date + "  |  " + location } else { date + location }
  pad(bottom: 0.7em, grid(
    columns: (1fr, auto),
    column-gutter: 1em,
    align(left)[#{ if degrees.len() > 0 [#strong[#degrees.at(0).at(0)], ] }#institution],
    align(right)[#{ if gpa != "" [#emph[#gpa]#h(1em)] }#emph[#date-loc]],
  ))
}

#let skills(areas) = {
  block(below: 1em)[
    #for area in areas {
      block(below: 0.8em)[#strong[#area.at(0): ]#area.at(1).join(", ")]
    }
  ]
}

// Date + location on ONE line (e.g. "Jun 2022 - Aug 2022  |  Mumbai, India").
// Two stacked lines in the right column made it taller than the left
// column's single role/project line, which shoved the bullets below too
// close to the heading -- one line keeps both columns the same height. It's
// also a non-issue for ATS extraction either way: same text, simpler stream.
#let exp(role: "", project: "", date: "", location: "", summary: "", details: [], gap: 0.8em) = {
  let date-loc = if date != "" and location != "" { date + "  |  " + location } else { date + location }
  block(below: gap)[
    #pad(bottom: 0.4em, grid(
      columns: (1fr, auto),
      column-gutter: 1em,
      align(left)[
        #strong[#role]#{ if project != "" [ | #emph[#project]] }
        #{ if summary != "" [\ #emph[#summary]] }
      ],
      align(right)[#emph[#date-loc]],
    ))
    #details
  ]
}

// Strips the scheme for display text ("https://rizent.me" -> "rizent.me"),
// matching how the header's own contact links display bare domains.
#let bare-url(url) = url.replace("https://", "").replace("http://", "")

// -- Header ----------------------------------------------------------------

// Each item is boxed so Typst never breaks a line in the middle of a phone
// number or URL -- a wrap point can only fall between whole contact items.
// No personal-site link here on purpose: phone/email/LinkedIn/GitHub fit on
// one line as real handle text (not generic "LinkedIn"/"GitHub" labels,
// which some ATS pipelines and recruiter tools can't pattern-match a
// structured profile URL out of if the visible text isn't the URL itself).
#let contact-list = (
  if data.phone != "" { box[#data.phone] },
  if data.email != "" { box[#link("mailto:" + data.email)[#data.email]] },
  if data.linkedin != "" { box[#link("https://" + data.linkedin)[#data.linkedin]] },
  if data.at("github", default: "") != "" { box[#link("https://" + data.github)[#data.github]] },
).filter(c => c != none)

#show: resume.with(
  author: data.name,
  location: data.location,
  contacts: contact-list,
  tagline: data.at("tagline", default: ""),
  size: fit-size,
  margin: fit-margin,
)

// -- Summary -----------------------------------------------------------
= Summary
#data.summary

// -- Experience --------------------------------------------------------
= Experience
#for e in data.experience [
  #exp(
    role: e.company,
    project: e.role,
    date: e.dates,
    location: e.at("location", default: ""),
    details: list(..e.bullets.map(b => [#b])),
  )
]

// -- Selected Products ----------------------------------------------------
#if "projects" in data and data.projects.len() > 0 [
  = Selected Products
  #for p in data.projects [
    #let has-url = p.at("url", default: none) != none and p.url != ""
    #exp(
      role: p.name,
      project: if has-url { link(p.url)[#bare-url(p.url)] } else { "" },
      date: "",
      summary: p.at("tagline", default: ""),
      details: list(..p.bullets.map(b => [#b])),
    )
  ]
]

// -- Skills ----------------------------------------------------------------
= Skills
#skills(data.skills.map(cat => (cat.category, cat.items)))

// -- Education ---------------------------------------------------------
= Education
#for e in data.education [
  #edu(
    institution: e.institution,
    date: e.years,
    gpa: e.score,
    degrees: ((e.degree, ""),),
  )
]
