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

- [0001. Local builds and redistribution](0001-local-builds-and-redistribution.md): accepted, consequences extended by 0006
- [0002. URL templates and POST stay in each project's fetcher](0002-fetchers-stay-in-each-project.md): superseded by 0004, restored by 0005
- [0003. Keep HTTP fetching in the standard library](0003-stdlib-http.md): accepted
- [0004. Declarative retrieval with per-request provenance](0004-declarative-retrieval.md): superseded by 0005
- [0005. A provenance core that records files other tools fetch](0005-provenance-core.md): accepted
- [0006. A release stage that writes and checks the archive metadata](0006-release-stage.md): accepted
