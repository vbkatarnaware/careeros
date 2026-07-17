export const site = {
  name: "CareerOS",
  tagline: "An AI-powered, deterministic job discovery and recommendation engine.",
  description:
    "CareerOS finds jobs, scores them against your real experience, and writes the results into a Google Sheet you open every morning. Deterministic where possible, AI only where reasoning genuinely adds value.",
  github: "https://github.com/vbkatarnaware/careeros",
  license: "MIT",
  author: "Vipul Katarnaware",
  year: 2026,
};

export const primaryNav = [
  { label: "Architecture", href: "/architecture/" },
  { label: "Product Decisions", href: "/product-decisions/" },
  { label: "Philosophy", href: "/philosophy/" },
  { label: "Comparison", href: "/comparison/" },
  { label: "Docs", href: "/docs/" },
  { label: "Roadmap", href: "/roadmap/" },
];

export const docsNav = [
  {
    group: "Getting started",
    items: [
      { label: "Overview", href: "/docs/" },
      { label: "Installation", href: "/docs/installation/" },
      { label: "Quick start", href: "/docs/quick-start/" },
      { label: "Configuration", href: "/docs/configuration/" },
    ],
  },
  {
    group: "Reference",
    items: [
      { label: "Commands", href: "/docs/commands/" },
      { label: "Examples", href: "/docs/examples/" },
      { label: "Architecture", href: "/docs/architecture/" },
    ],
  },
  {
    group: "Project",
    items: [
      { label: "Developer guide", href: "/docs/developer-guide/" },
      { label: "Contributing", href: "/docs/contributing/" },
      { label: "Roadmap", href: "/docs/roadmap/" },
    ],
  },
];
