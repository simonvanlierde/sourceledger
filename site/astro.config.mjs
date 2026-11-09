import { existsSync } from "node:fs";
import { posix } from "node:path";
import { fileURLToPath } from "node:url";
import { satteri } from "@astrojs/markdown-satteri";
import { defineConfig } from "astro/config";

const site = "https://simonvanlierde.com";
const base = "/sourceledger";
const docs = fileURLToPath(new URL("../docs/", import.meta.url));
const blob = "https://github.com/simonvanlierde/sourceledger/blob/main/";

// The docs link to each other as files (`recipes.md#pooch`), which GitHub follows. On the
// site a link to another page becomes that page, and any other relative link (an ADR, an
// example, the README) goes to the file on GitHub.
const docLinks = {
  name: "doc-links",
  element: {
    filter: ["a"],
    visit(node, ctx) {
      const href = node.properties.href;
      if (typeof href !== "string" || /^([a-z][a-z+.-]*:|#|\/)/i.test(href)) return;
      const [path, hash = ""] = href.split(/(?=#)/);
      const name = path.match(/^([\w-]+)\.md$/)?.[1];
      const page = name && existsSync(docs + path) && `${base}/${name === "index" ? "" : `${name}.html`}`;
      ctx.setProperty(node, "href", (page || blob + posix.normalize(`docs/${path}`)) + hash);
    },
  },
};

const find = (node, tag) => (node.children ?? []).filter((c) => c.tagName === tag);
const text = (node) => (node.type === "text" ? node.value : (node.children ?? []).map(text).join("")).trim();

// Wide tables scroll on their own. Each cell is labelled with its column for the stacked
// phone layout, and each box is a focusable region named after its caption or the heading
// above it, so keyboard users can scroll it without scripts. site.js drops that again for
// a box that does not overflow.
const tableNames = new Map();
const tableScroll = {
  name: "table-scroll",
  element: {
    filter: ["table"],
    visit(node, ctx) {
      const headers = find(find(node, "thead")[0] ?? {}, "tr").flatMap((tr) => find(tr, "th").map(text));
      for (const body of find(node, "tbody")) {
        for (const tr of find(body, "tr")) {
          find(tr, "td").forEach((td, i) => headers[i] && ctx.setProperty(td, "data-label", headers[i]));
        }
      }
      const caption = find(node, "caption")[0];
      const siblings = ctx.parent(node).children;
      let name = caption ? text(caption) : "";
      for (let i = ctx.indexOf(node) - 1; !name && i >= 0; i--) {
        if (/^h[1-6]$/.test(siblings[i].tagName)) name = text(siblings[i]);
      }
      // Two tables under one heading are numbered, as region names must differ.
      const used = tableNames.get(ctx.parent(node)) ?? new Map();
      tableNames.set(ctx.parent(node), used);
      used.set(name, (used.get(name) ?? 0) + 1);
      if (used.get(name) > 1) name += ` (table ${used.get(name)})`;
      ctx.wrapNode(node, {
        type: "element",
        tagName: "div",
        properties: { className: ["table-scroll"], tabindex: 0, role: "region", "aria-label": name || "Table" },
        children: [],
      });
    },
  },
};

// Static output: `astro build` writes plain files to dist/, which site.yml deploys to
// GitHub Pages. The custom domain of the user site serves this project's site under
// /sourceledger/, and every page lives at <name>.html, the published address.
export default defineConfig({
  output: "static",
  site,
  base,
  build: { format: "file" },
  // Whitespace between tags is kept, so inline elements keep their spacing.
  compressHTML: false,
  // Code blocks are styled by site.css alone, with no highlighter colours.
  markdown: { processor: satteri({ hastPlugins: [docLinks, tableScroll] }), syntaxHighlight: false },
});
