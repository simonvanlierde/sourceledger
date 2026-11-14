"""Align published rows by their declared keys, keeping their values and provenance."""

from collections.abc import Iterable, Mapping, Sequence


def align_rows[BeforeValue, AfterValue](
    before: Iterable[Mapping[str, BeforeValue]],
    after: Iterable[Mapping[str, AfterValue]],
    *,
    keys: Sequence[str],
) -> dict[tuple[str | int, ...], tuple[dict[str, BeforeValue], dict[str, AfterValue]]]:
    """Return shallow row snapshots for keys present in both inputs, in before-row order.

    Every row in both inputs is checked before matching, including rows with no match.
    Key cells must be nonblank strings or integers, excluding booleans. Their text and
    types are preserved: ``2020`` and ``"2020"`` are different keys. Other cells are
    unchanged, including missing values, nonfinite numbers and provenance fields.
    Nested objects remain shared because the snapshots are shallow.

    Empty inputs or no common keys return an empty dictionary. Both inputs are held in
    memory. For other join types or dataframe operations, use pandas or Polars.

    Args:
        before: The earlier rows, such as ``read_output(before_path)``.
        after: The later rows, such as ``read_output(after_path)``.
        keys: A nonempty sequence of distinct, nonblank column names.

    Raises:
        ValueError: Invalid key names, a nonmapping row, a missing or invalid key cell,
            or a duplicate key in either input. Row errors identify the input and row.
        OSError: An input iterator could not read its table. Other iterator errors
            also propagate unchanged.
    """
    if isinstance(keys, (str, bytes)) or not isinstance(keys, Sequence):
        msg = "keys must be a nonempty sequence of distinct nonblank column names"
        raise ValueError(msg)  # noqa: TRY004  # input errors share the public ValueError contract
    names = tuple(keys)
    if (
        not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        msg = "keys must be a nonempty sequence of distinct nonblank column names"
        raise ValueError(msg)
    old, new = _index(before, names, "before"), _index(after, names, "after")
    return {key: (row, new[key]) for key, row in old.items() if key in new}


def _index[Value](
    rows: Iterable[Mapping[str, Value]], keys: tuple[str, ...], side: str
) -> dict[tuple[str | int, ...], dict[str, Value]]:
    indexed = {}
    for number, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            msg = f"{side} row {number}: expected a mapping"
            raise ValueError(msg)  # noqa: TRY004  # input errors share the public ValueError contract
        snapshot = dict(row)
        cells: list[str | int] = []
        for column in keys:
            if column not in snapshot:
                msg = f"{side} row {number}: missing key column {column!r}"
                raise ValueError(msg)
            cell = snapshot[column]
            if (
                isinstance(cell, bool)
                or not isinstance(cell, (str, int))
                or (isinstance(cell, str) and not cell.strip())
            ):
                msg = f"{side} row {number}: key column {column!r} must be a nonblank string or integer"
                raise ValueError(msg)
            cells.append(cell)
        key = tuple(cells)
        if key in indexed:
            msg = f"{side} row {number}: duplicate key {key!r}"
            raise ValueError(msg)
        indexed[key] = snapshot
    return indexed
