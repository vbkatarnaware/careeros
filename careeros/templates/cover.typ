// CareerOS cover-letter template. Matches resume.typ's font/header design
// so the two artifacts read as one set. Deterministic, zero AI at render
// time. Input: sys.inputs.data, a JSON object {name, location, phone, email,
// linkedin, paragraphs: [str, ...]} — see careeros/typst_render.py.
//
// New Computer Modern is Typst's own bundled font (same as resume.typ) — no
// font file ships with this package and no font_paths are needed.

#let data = json(bytes(sys.inputs.data))

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

#v(1.2em)

#for p in data.paragraphs [
  #par(justify: true)[#p]
  #v(0.8em, weak: true)
]
