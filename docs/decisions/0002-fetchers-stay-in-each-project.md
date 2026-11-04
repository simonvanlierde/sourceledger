# 0002. URL templates and POST stay in each project's fetcher

- Status: accepted
- Date: 2026-10-29

## Context

Some projects that use sourceledger fetch their data with URL templates or POST requests. They could move that fetching into sourceledger. The two cases are wise-pipeline's World Bank fetcher and cml-ia-factors' runtime cache.

## Decision

URL templates and POST requests stay in each project's own fetcher. wise-pipeline keeps its templated World Bank fetcher. cml-ia-factors keeps its runtime cache.

Each source in `sources.toml` is one fixed `url`, fetched with a GET, and the rest of the design leans on that:

- The fetch record and the conditional re-fetch (`If-None-Match`, `If-Modified-Since`) belong to that one URL. Change it, and `build` refuses the old download.
- The `sha256` pin checks the one file that URL returns.
- `archive = true` asks Save Page Now to snapshot that URL. The archive fetches the URL itself, with no headers or body.

A URL template expands into many URLs in one run, and a POST answer has no URL that names it. Both would need their own record, pin and re-fetch rules. sourceledger does its own HTTP on top of `urllib` (retries, the size cap, the time budget and the redirect check), so each new request shape needs the same care and a test against a real server. The projects' fetchers already work, and `build`'s `parse` argument already covers their own file formats.

## Consequences

- sourceledger does not grow template or POST handling for these projects.
- A templated or POST source stays out of `sources.toml`, so sourceledger keeps no fetch record or pin for it.
- Moving a fetcher into sourceledger later needs a new record. A second project that needs the same request shape is the time to look again. On 2026-11-04 none does: only wise-pipeline uses URL templates, and only msc-thesis-ie sends a POST (KNMI hourly data).
- Paging through an API, a third shape, already has two users: msc-thesis-ie follows PDOK's OGC `next` links, and credit-matrix's census follows Europe PMC's `cursorMark`. It is the likeliest shape to come back here.
