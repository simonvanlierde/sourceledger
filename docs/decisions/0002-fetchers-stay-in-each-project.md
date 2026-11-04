# 0002. URL templates and POST stay in each project's fetcher

- Status: superseded by 0004
- Date: 2026-10-29

[Decision 0004](0004-declarative-retrieval.md) replaces this boundary. Declarative fetching is not yet implemented.

## Context

Some projects that use sourceledger fetch their data with URL templates or POST requests. They could move that fetching into sourceledger. The two cases are wise-pipeline's World Bank fetcher and cml-ia-factors' runtime cache.

## Decision

URL templates and POST requests stay in each project's own fetcher. wise-pipeline keeps its templated World Bank fetcher. cml-ia-factors keeps its runtime cache.

## Consequences

- sourceledger does not grow template or POST handling for these projects.
- Moving a fetcher into sourceledger later needs a new record.
