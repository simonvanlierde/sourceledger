# Decisions

Each file here records one decision that shapes this project: what was chosen, why,
and what it costs. The record keeps the reasoning once the people who made the choice
have moved on.

## Adding a record

1. Copy `0000-template.md` to the next free number and a short slug, such as
   `0001-one-parser.md`.
2. Fill in its sections. Set its status to `proposed`, then to `accepted` once agreed.
3. Add it to the index below.

Don't rewrite an accepted record. When a decision changes, write a new record, and set
the old one's status to `superseded by 0007`, naming the new record's number.

## Index

- [0001. Local builds and redistribution](0001-local-builds-and-redistribution.md): accepted
- [0002. URL templates and POST stay in each project's fetcher](0002-fetchers-stay-in-each-project.md): superseded by 0004
- [0004. Declarative retrieval with per-request provenance](0004-declarative-retrieval.md): accepted direction; implementation contract draft, capability unavailable
