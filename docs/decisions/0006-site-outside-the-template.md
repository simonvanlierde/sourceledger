# The docs site stays outside the template's `site` option

- Status: accepted
- Date: 2026-11-10

## Context

The docs site in `site/` is hand-built Astro (PR #82): a content collection, a
`[...slug].astro` route and its own layout. The project template can also scaffold an
Astro site (`site: astro`). Recording that answer makes `copier update` re-create the
template's `site/src/pages/index.astro` and `about.md` every time, because the template
only skips files that exist. They collide with the `/` route and break `just site`.

## Decision

`.copier-answers.yml` keeps `site: none`, so `copier update` does not touch `site/`,
`site.yml`, `preview-retire.yml` or the `site` recipes in the `justfile`.

## Consequences

Template fixes to the site workflows do not arrive on their own: compare
`.github/workflows/site.yml` with a fresh render of `site: astro` after a template bump.
The `site: build` check is not in `.github/rulesets/main-checks.json` unless added by hand.
