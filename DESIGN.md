---
name: sourceledger
description: Reading-focused documentation with a visible source-to-output trail.
colors:
  paper: "#ffffff"
  ink: "#26334b"
  muted: "#526078"
  link: "#26334b"
  rule: "#c9cfd8"
  code: "#f0f2f6"
  focus: "#587723"
  selection: "#dfff76"
  panel: "#26334b"
  accent: "#dfff76"
typography:
  display:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
    fontSize: "3.625rem"
    fontWeight: 600
    lineHeight: 1.08
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
    fontSize: "2.75rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
    fontSize: "2rem"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Arimo, Helvetica, Arial, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.65
  code-block:
    fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, monospace'
    fontSize: "0.82rem"
    lineHeight: 1.7
rounded:
  inline-code: "3px"
  revision-control: "4px"
  code-block: "5px"
spacing:
  small: "0.5rem"
  regular: "1rem"
  section-inset: "1.5rem"
  wide-inset: "1.75rem"
  column-inset: "3rem"
components:
  revision-control:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.revision-control}"
    padding: "0.5rem 0.9rem"
  revision-control-selected:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.ink}"
    rounded: "{rounded.revision-control}"
    padding: "0.5rem 0.9rem"
  navigation-current:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.ink}"
    padding: "0.55rem 0.7rem"
  provenance-panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.paper}"
    padding: "1.6rem 1.75rem"
---

# Design System: sourceledger

## Overview

A readable documentation column sits beside a compact navigation rail. White paper, navy ink and lime highlights give source records and revision results a clear visual hierarchy. The signature example connects a provenance diagram, a comparison table and a validation result.

## Colors

### Primary

Navy **ink** carries text; **panel** gives provenance and validation their dark ground. Lime **accent** marks selected navigation, selected revision controls, changed values and result emphasis. **Selection** supplies the same lime for selected text.

### Neutral

**Paper** is the page ground. **Muted** supports captions and secondary text; **rule** divides rows and page regions. **Code** supplies the pale ground for code and comparison surfaces. **Link** and **focus** retain separate semantic roles even when their values match another token.

Dark mode follows the operating-system preference: paper becomes `#171f30`, ink `#e1e6eb`, muted `#b0bdc9`, link and focus use lime, rule becomes `#3d4c5a`, and code becomes `#25314a`. Panel and accent stay fixed; panel text stays white. Selected controls and marked values keep navy text.

## Typography

Arimo is served locally as WOFF2 with weights 400–700 and Helvetica/Arial fallbacks. Its license is in `docs/assets/fonts/OFL.txt`. Code uses the system monospace stack; no separate display font is loaded.

The frontmatter records desktop homepage display, documentation headline, section title, body and code-block roles. Third-level headings use 1.5rem at weight 500; fourth-level headings use 1.2rem bold. Documentation introductions use 1.3rem with 1.6 line height; the homepage introduction uses 1.25rem. Headings balance their lines. The wordmark is bold, 1.625rem with −0.03em tracking.

## Layout

The header and reading grid share an 85rem maximum width. Desktop content has 3rem outer padding, an 11rem navigation column and a 3.5rem column gap. Navigation sticks 2rem from the top. Documentation articles stop at 72ch; the homepage allows a wider example, with opening text constrained to 65ch and ordinary paragraphs to 68ch.

Below 64rem, the reading grid becomes one column with 1.5rem horizontal padding. Navigation becomes a native disclosure above the article; JavaScript closes it below 64rem and opens it at or above 64rem. Without JavaScript it starts open. Documentation headlines shrink to 2.25rem and section titles to 1.7rem.

At 43.75rem and below, homepage display type becomes 2.5625rem, its introduction becomes 1.125rem, and the three-column provenance flow stacks vertically. Diagram arrows turn downward. Comparison and diagram insets become 1.5rem.

Tables sit in keyboard-focusable horizontal scroll regions. General tables have a 36rem minimum width; the revision example removes that minimum. Code blocks scroll internally. Header links, navigation links, buttons and disclosure summaries have a minimum 44px height.

## Elevation & Depth

No shadows or animated transitions are used. Navy panels, pale comparison surfaces and thin dividers establish grouping. The copy status floats at the lower right with ink background and paper text; it disappears when empty.

## Shapes

Structural panels and navigation highlights have square corners. Small radii belong to inline code, revision controls and code blocks as recorded above. Borders are thin; the provenance diagram uses CSS lines and arrowheads rather than image assets.

## Components

### Navigation and links

The wordmark and source link share a compact masthead. Navigation has a pale code-ground hover state and a lime current-page state with navy semibold text. Ordinary links remain underlined; hover increases underline thickness. Keyboard focus uses a 2px outline with 4px offset. A focus-revealed skip link reaches the main content.

### Revision controls and comparison

Two bordered buttons select the original or revised download through `aria-pressed`. Hover underlines their labels. Forced-colors mode also makes the selected label bold. JavaScript reveals the controls and initially selects the revision; without it, both comparison panels remain readable. Lime marks the changed table value. A navy result strip directly follows the table, with lime emphasis. A native disclosure exposes checksums and the validation report.

### Provenance flow

Three ordered nodes describe published source, recorded download and built output. White text sits on navy, with lime monospace identifiers and lime connecting arrows. Node-top dividers use `#718198`. This connects to the pale comparison panel without rounded card shells.

### Code and copy feedback

Code blocks use a pale background and thin rule border. Code blocks use 0.75rem vertical padding. Copy buttons sit beside independently scrollable code, with a 44px target and no extra toolbar row. Code receives keyboard focus so long lines remain reachable. They appear only when the Clipboard API exists; console output blocks are excluded. Hover uses the paper ground; copying disables the button temporarily. A polite live region reports success or failure for five seconds. The installation command uses a compact square block with inline copy space.

## Do's and Don'ts

### Do

- **Do** preserve readable content when JavaScript is unavailable.
- **Do** keep keyboard focus visible and wide tables scrollable.
- **Do** use the connected diagram, comparison and result to explain provenance.

### Don't

- **Don't** make lime the default reading-text color on white.
- **Don't** replace native disclosure and button behavior with decorative controls.
- **Don't** add raster illustrations where the current system uses HTML and CSS.
