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

// Wide tables scroll on their own, and site.js stacks them on phones and makes a box that
// overflows focusable.
const tableScroll = {
  name: "table-scroll",
  element: {
    filter: ["table"],
    visit(node, ctx) {
      ctx.wrapNode(node, {
        type: "element",
        tagName: "div",
        properties: { className: ["table-scroll"] },
        children: [],
      });
    },
  },
};

// Static output: `astro build` writes plain files to dist/, which site.yml deploys to
// GitHub Pages. The custom domain of the user site serves this project's site under
// /sourceledger/, and every page keeps the <name>.html address the Jekyll site gave it.
export default defineConfig({
  output: "static",
  site,
  base,
  build: { format: "file" },
  // Keep the whitespace between tags, which the Jekyll pages had.
  compressHTML: false,
  // The site styles code blocks plainly, as the Jekyll site did.
  markdown: { processor: satteri({ hastPlugins: [docLinks, tableScroll] }), syntaxHighlight: false },
});
