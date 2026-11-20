---
name: sourceledger
description: Reading-focused documentation with a visible source-to-output trail.
colors:
  paper: "#f9fafc"
  ink: "#243247"
  muted: "#57677e"
  link: "#315ba8"
  selection: "#e7edf8"
  rule: "#d8e0eb"
  code: "#eef1f7"
  panel: "#253c61"
  on-panel: "#f8faff"
  signal: "#bfd5ff"
  muted-on-panel: "#c0cddf"
  panel-rule: "#7892b8"
typography:
  body:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
  code:
    fontFamily: "_, SFMono-Regular, Consolas, Menlo, monospace"
  social-card-code:
    fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, "DejaVu Sans Mono", monospace'
---

# Design system: sourceledger

Zensical supplies the reading layout, search, navigation and Python API reference.
The site serves the Markdown in `docs/` at the published `/sourceledger/` address.
`use_directory_urls = false` preserves the published `.html` page addresses.

## Colour and typography

`docs/assets/docs-tokens.css` defines the shared colour and font tokens.
`docs/assets/brand.css` applies the Chalk & cobalt palette: chalk paper, blue-black ink
and cobalt links. Pale blue marks the selected navigation entry and text selection.
The provenance flow uses a deep blue panel with light blue source identifiers.
Muted blue text, rules and code surfaces support the reading hierarchy.
Search keeps the theme's black foreground so its translucent result counts retain contrast.
The social card uses the same palette, with muted light blue text and blue rules on its panel.
Its ruled paper represents a ledger. Status colours retain their semantic meaning.
Links stay underlined. Keyboard focus remains visible.

Arimo is served locally from `docs/assets/fonts/`, with Helvetica and Arial fallbacks.
Its licence and source are beside the font. Code uses the system monospace stack.
The site does not fetch fonts from a third party.

## Reading and navigation

The five top-level navigation entries remain Start here, Worked example,
Manifest and commands, How it works and Safe use. Related pages sit beneath these entries.
The theme supplies the responsive drawer, search dialog and table of contents.
`docs/assets/docs.js` gives the drawer and search native buttons and makes overflowing tables
reachable by keyboard. `docs/assets/docs.css` supplies the shared accessibility adjustments.

## Connected example

The home page connects the published source, recorded download and built output in one provenance flow.
Its three columns stack on a narrow screen. The revision table and validation result follow that flow.
The synthetic example changes the 2023 value from 419.31 to 419.32.
Both values and the checksums remain readable without JavaScript.
`tests/test_docs.py` checks this content against the real fetch, build and validation path.

The standalone example report uses the same synthetic sources as the report tests.
The home page and social card make no claims about users or adoption.

## Assets and previews

`docs/og-card.html` draws the social card in `docs/assets/og.png`.
`docs-overrides/main.html` supplies the link-preview metadata on every page.
`just og-card` regenerates the image and changes its URL hash so preview caches refresh.
`just docs` builds the whole site in strict mode and applies script hashes from
`docs/script-hashes.json` to each page’s Content Security Policy. Changes to inline scripts
require review of that allowlist. The policy is inserted before any scripts.
`just docs` checks the rendered preview metadata; `just docs-serve` previews it locally.

## Template source

The full-project Copier baseline remains v0.15.0 in `.copier-answers.yml`.
The shared docs tokens and Zensical token mappings come from
[project-templates v0.16.1](https://github.com/simonvanlierde/project-templates/tree/v0.16.1).
The hash policy adapts that release's Starlight build hook to Zensical.
The `docs_engine: zensical` answer selects the engine for the next full Copier update.
That update must still offer the release, hook and dependency-policy changes outside this docs slice.
