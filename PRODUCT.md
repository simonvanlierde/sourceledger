# Product

## Platform

web

## Users

Researchers who use Python or a CLI and are new to provenance tooling. Dataset recipients also need to inspect recorded sources and verify delivered files.

## Product Purpose

Help researchers build datasets from published sources, record downloads (their own or another tool's), trace outputs to their sources and detect publisher revisions.

## Operating Context

A Python library and CLI configured with sources.toml. The website explains installation, a working revision example, commands and safe use.

## Capabilities and Constraints

The documentation uses static Jekyll pages on GitHub Pages. Preserve readable documentation, installation links, accessible navigation and a working revision demonstration. Offline verification and the standalone HTML provenance report ship with the library. Downloads by pooch, DVC, dlt or a project's own code enter through `record`, documented as recipes, not plugins.

## Brand Commitments

Use plain, direct language. Keep the five-link reading layout, white/navy/lime styling and connected provenance flowchart, revision comparison and validation result. Consider JSON vet and CiteSync as references without copying their layouts.

## Evidence on Hand

The documentation and synthetic revision fixture show a fictional publisher changing the 2023 value from 419.31 to 419.32. Use this example honestly; do not invent users, testimonials or adoption statistics.
