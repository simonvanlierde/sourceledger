---
title: Share a dataset's provenance
---

# Share a dataset's provenance

After building a dataset, create a standalone HTML report for someone who receives it:

```sh
sourceledger report > provenance.html
```

Keep the report outside `data/processed/`: validation expects that folder to hold only
outputs described by the data package. Open `provenance.html` in a browser. It uses no
scripts, remote styles, fonts or network requests.

The command reads `sources.toml`, `data/fetches.jsonl` and the built package in
`data/processed/`. It checks that the listed outputs match the implemented manifest
sources and that their files match the package. It also flags a build that predates the
last logged revision. Each output links to its source details: URL, vintage, fetch
timestamp, raw and output hashes, licence and reuse terms.

Source details come from the current manifest; the fetch stamps describe the last build.
If a file or its provenance is stale, the report lists the problem. The command returns 0
when it produces a report, including one with integrity problems. A fatal input error
returns 1 and writes no HTML. A missing fetch log produces a provenance problem in the report. Run `validate` to gate a pipeline on those problems.

The report includes no raw values or header names and values. Displayed HTTP URLs omit
user information, query parameters and fragments. Local and non-HTTP URLs are omitted.
Review free-text names and reuse terms before sharing: sourceledger cannot recognize
secrets someone wrote into those fields or into a URL path. Displayed metadata is escaped
so it cannot become executable HTML.

Passing integrity checks does not authenticate the package, establish scientific
correctness or grant publication permission. Recipients must obtain the package and
report from a source they trust, and review the source terms.

In Python, `sourceledger.report(sources, processed=folder, log=fetch_log)` returns the HTML
as a string. It reads the same files and does not write the report itself.
