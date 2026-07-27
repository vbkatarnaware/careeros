// CareerOS cover-letter template. Matches resume.typ's font/header design
// so the two artifacts read as one set. Deterministic, zero AI at render
// time. Input: sys.inputs.data, a JSON object {name, location, phone, email,
// linkedin, date, company, recipient, salutation, paragraphs: [str, ...]} —
// see careeros/typst_render.py.
//
// v1.7.2 adds real letter furniture (rule, date, recipient block,
// salutation, sign-off) — before this it was a centered name over justified
// prose, which didn't read as correspondence. Every added field is OPTIONAL:
// when the caller passes an empty string the whole block is skipped rather
// than rendering a blank line, so a letter with no known recipient looks
// deliberate instead of broken.
//
// New Computer Modern is Typst's own bundled font (same as resume.typ) — no
// font file ships with this package and no font_paths are needed.

#let data = json(bytes(sys.inputs.data))
#let has(key) = key in data and data.at(key) != ""

#set page(
  paper: "a4",
  margin: (top: 18mm, bottom: 18mm, left: 20mm, right: 20mm),
)
#set text(
  font: "New Computer Modern",
  size: 11pt,
  fill: rgb("#1a1a1a"),
  features: (liga: 0, dlig: 0, clig: 0),
  lang: "en",
  hyphenate: false,
)
#set par(justify: true, leading: 0.65em, spacing: 1em)
#show link: set text(fill: rgb("#0645AD"))

#align(center)[
  #text(weight: 700, size: 1.8em, smallcaps(data.name))
  #v(0.3em)
  #text(size: 0.85em)[
    #smallcaps[#data.location  |  #data.phone  |  #data.email  |  #data.linkedin]
  ]
]

#v(0.6em)
#line(length: 100%, stroke: 0.5pt + rgb("#999999"))
#v(1em)

#if has("date") [
  #align(right)[#text(size: 0.95em)[#data.date]]
  #v(0.8em)
]

#if has("recipient") or has("company") [
  #text(size: 0.95em)[
    #if has("recipient") [#data.recipient \ ]
    #if has("company") [#data.company]
  ]
  #v(1.1em)
]

#if has("salutation") [
  #data.salutation
  #v(0.7em, weak: true)
]

#for p in data.paragraphs [
  #par(justify: true)[#p]
  #v(0.8em, weak: true)
]

#v(0.8em)
#text[Sincerely,]
#v(1.4em)
#text[#data.name]
