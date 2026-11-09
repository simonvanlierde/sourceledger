import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";

// The pages are the Markdown files at the top of docs/, which GitHub renders as well. The
// folders below it (decisions, examples) stay on GitHub.
export const collections = {
  docs: defineCollection({ loader: glob({ base: "../docs", pattern: "*.md" }) }),
};
