---
name: sourceledger
description: Reading-focused documentation with a visible source-to-output trail.
colors:
  paper: "#ffffff"
  ink: "#26334b"
  focus: "#587723"
  accent: "#dfff76"
typography:
  body:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
  code:
    fontFamily: 'ui-monospace, monospace'
---

# Design system: sourceledger

Zensical supplies the reading layout, search, navigation and Python API reference.
The site serves the Markdown in `docs/` at the published `/sourceledger/` address.
`use_directory_urls = false` preserves the published `.html` page addresses.

## Colour and typography

`docs/assets/brand.css` applies white paper, navy ink and lime highlights.
Lime marks the selected navigation entry and the source identifiers on navy panels.
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
`just docs` builds the whole site in strict mode; `just docs-serve` previews it locally.
