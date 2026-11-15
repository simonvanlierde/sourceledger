# 0007. Align rows without choosing comparison policy

- Status: accepted
- Date: 2026-11-14

## Context

The WDI adopter compares successive tables by country and year. It needs unique keys and access to both values.
Its relative-change threshold, exclusion of zero bases and unit checks belong to that dataset.
[Decision 0005](0005-provenance-core.md) keeps sourceledger small and leaves project policy outside the core.

Keyed alignment is an established operation:

- [pandas merge](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html) supports an inner join, explicit keys and one-to-one validation. Its inner join preserves left-key order.
- [Polars join](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join.html) supports the same validation and explicit left-row ordering.
- [DataComPy](https://capitalone.github.io/datacompy/pandas_usage.html) adds comparison reports, numeric tolerances and duplicate matching.
- [csv-diff](https://github.com/simonw/csv-diff) reports changed, added and removed records from keyed files.

These tools already cover broader comparison needs. The WDI pipeline currently needs no dataframe runtime dependency.

## Decision

Add one dependency-free function, `align_rows(before, after, keys=...)`, over row iterables such as `read_output()`.
Use an inner, one-to-one alignment, returning both full rows for every common key in before-row order.
Validate both inputs before matching so an unmatched duplicate cannot disappear from the check.
Take shallow row snapshots to preserve values when an iterator reuses a mutable row dictionary.

Reject missing, blank or unsupported key cells rather than guessing their identity.
pandas matches null join keys while Polars excludes them by default; rejecting them here is an explicit local policy.
Preserve key text and types without coercion. Preserve every other value, including provenance, large integers and nonfinite numbers.

Do not add a comparison stage, numeric conversion, tolerances, join modes, file wrappers or an optional dataframe engine.
Projects decide what changed and how to report it. Projects already using pandas or Polars should keep using their joins.

## Consequences

The public function supplies validated row correspondence without defining numeric equality or losing provenance.
Both inputs must fit in memory. It does not report added or removed rows, and shallow snapshots still share nested values.
Broader comparisons use the existing tools above rather than extending this function into another dataframe library.
