// @ts-check
import { defineConfig } from "astro/config";
import react from "@astrojs/react";
import mdx from "@astrojs/mdx";
import sitemap from "@astrojs/sitemap";
import tailwindcss from "@tailwindcss/vite";

// The canonical production URL. Update if the site moves to a custom domain.
const SITE = "https://careeros.vercel.app";

// https://astro.build/config
export default defineConfig({
  site: SITE,
  output: "static",
  integrations: [react(), mdx(), sitemap()],
  vite: {
    plugins: [tailwindcss()],
  },
});
