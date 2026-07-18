# CareerOS — Website

The official website for [CareerOS](https://github.com/vbkatarnaware/careeros),
an AI-powered, deterministic job discovery and recommendation engine.

This is a static site, fully isolated from the Python package. It has its own
`package.json` and is deployable on its own. Nothing here imports from or affects
the `careeros/` Python application.

## Tech

- **[Astro](https://astro.build)** — static-first, minimal JavaScript
- **[Tailwind CSS v4](https://tailwindcss.com)** — CSS-first design tokens
- **React islands** — only where interactivity earns it (theme toggle, the
  architecture diagram, copy buttons, mobile nav)
- **[Motion](https://motion.dev)** — subtle, reduced-motion-aware animation
- Self-hosted **Geist** and **Geist Mono** (no external font CDN)

## Local development

Requires Node 18+ (built and tested on Node 22).

```bash
cd website
npm install
npm run dev      # http://localhost:4321
```

Other scripts:

```bash
npm run build    # static build → dist/
npm run preview  # serve the production build locally
```

## Project structure

```
website/
├── astro.config.mjs      # static output; mdx + react + sitemap + tailwind
├── src/
│   ├── styles/global.css # design tokens (light/dark) + base styles
│   ├── lib/              # site metadata, nav, pipeline + decisions data
│   ├── components/
│   │   ├── ui/           # Button, Badge, CommandBlock, ThemeToggle, Section
│   │   ├── site/         # Header, Footer, nav, docs sidebar, SEO head
│   │   ├── home/         # Hero
│   │   └── ArchitectureDiagram.tsx  # interactive per-stage diagram
│   ├── layouts/          # BaseLayout, DocsLayout
│   └── pages/            # home + product-decisions, architecture, philosophy,
│                         # comparison, roadmap, open-source, and docs/*
└── public/               # favicon and static assets
```

## Design

- **Direction:** cool graphite with a single indigo accent (Linear-adjacent),
  all-sans with tight display tracking.
- **Theme:** dark-only, by design.
- **Tokens:** defined once as CSS custom properties in `src/styles/global.css`
  and exposed to Tailwind via `@theme inline`. Change the palette there.

## Content accuracy

Every claim on the site is grounded in the CareerOS repository — the README,
`AGENT_GUIDE.md`, the prompt files, the JSON schemas, and the changelog. There
are no invented features, metrics, users, or benchmarks. Paid providers are
never shown as enabled by default, and roadmap items are never presented as
shipped. If the product changes, update the copy in `src/lib/` and the relevant
pages.

## Deploy on Vercel

The site deploys directly from this subdirectory.

1. Import the `vbkatarnaware/careeros` repository into Vercel.
2. Set **Root Directory** to `website/`.
3. Framework preset auto-detects as **Astro**. Defaults are correct:
   - Build command: `npm run build`
   - Output directory: `dist`
4. Deploy.

A `vercel.json` is included with the framework preset and long-lived caching for
hashed assets. To use a custom domain, set it in the Vercel project and update
`site` in `astro.config.mjs` so canonical URLs and the sitemap are correct.

## License

MIT — © 2026 Vipul Katarnaware.
