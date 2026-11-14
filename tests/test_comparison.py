"""Keyed alignment preserves rows without choosing numeric comparison policies."""

from collections.abc import Iterator
from pathlib import Path
from typing import assert_type

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sourceledger import align_rows, read_output


def test_alignment_preserves_value_types_for_output_readers(tmp_path: Path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    before.write_text("id,value\na,1\n")
    after.write_text("id,value\na,2\n")
    pairs = align_rows(read_output(before), read_output(after), keys=("id",))
    old, new = pairs[("a",)]
    assert_type(old, dict[str, str])
    assert_type(new, dict[str, str])
    assert (float(old["value"]), float(new["value"])) == (1.0, 2.0)

    mixed = align_rows([{"id": 1}], [{"id": 1, "value": 2.0}], keys=("id",))
    assert_type(mixed[(1,)][0], dict[str, int])
    assert_type(mixed[(1,)][1], dict[str, int | float])


def test_alignment_keeps_common_rows_in_before_order():
    before = [
        {"country": "NLD", "year": 2020, "value": 0, "raw_sha256": "old"},
        {"country": "DEU", "year": 2020, "value": 4},
        {"country": "BRA", "year": 2020, "value": 9},
    ]
    after = [
        {"country": "DEU", "year": 2020, "value": 4},
        {"country": "KEN", "year": 2020, "value": 1},
        {"country": "NLD", "year": 2020, "value": 10, "raw_sha256": "new"},
    ]
    result = align_rows(iter(before), iter(after), keys=("country", "year"))
    assert list(result) == [("NLD", 2020), ("DEU", 2020)]
    assert result[("NLD", 2020)] == (before[0], after[2])
    assert result[("DEU", 2020)] == (before[1], after[0])


@pytest.mark.parametrize(("before", "after"), [([], []), ([{"id": "a"}], []), ([], [{"id": "a"}])])
def test_no_common_rows_is_empty(before: list[dict[str, str]], after: list[dict[str, str]]):
    assert align_rows(before, after, keys=["id"]) == {}


def test_key_values_are_not_normalized():
    rows = [{"id": value} for value in (2020, "2020", " NLD ", "NLD")]
    assert list(align_rows(rows, reversed(rows), keys=["id"])) == [(2020,), ("2020",), (" NLD ",), ("NLD",)]


@pytest.mark.parametrize(
    "keys",
    [[], "id", b"id", [""], [" \t"], ["id", "id"], [None], [1], None, {"id"}],
    ids=[
        "empty",
        "bare-string",
        "bare-bytes",
        "blank",
        "whitespace",
        "duplicate",
        "null-name",
        "integer",
        "null",
        "set",
    ],
)
def test_key_columns_must_be_explicit_and_distinct(keys: object):
    with pytest.raises(ValueError, match="keys") as caught:
        align_rows([], [], keys=keys)  # ty: ignore[invalid-argument-type]  # exercise invalid input
    assert str(caught.value) == "keys must be a nonempty sequence of distinct nonblank column names"


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("cell", [None, True, False, 1.0, float("nan"), "", " \n", [], {}])
def test_invalid_key_cells_are_refused_even_without_matches(side: str, cell: object):
    rows = [{"id": "valid"}, {"id": cell}]
    with pytest.raises(ValueError, match="key column") as caught:
        align_rows(rows if side == "before" else [], rows if side == "after" else [], keys=["id"])
    assert str(caught.value) == f"{side} row 2: key column 'id' must be a nonblank string or integer"


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("row", [{"other": "a"}, None, ["a"], "id"])
def test_missing_keys_and_nonmapping_rows_are_refused(side: str, row: object):
    rows = [{"id": "valid"}, row]
    with pytest.raises(ValueError, match="row 2:") as caught:
        align_rows(rows if side == "before" else [], rows if side == "after" else [], keys=["id"])  # ty: ignore[invalid-argument-type]  # exercise nonmapping input
    problem = "missing key column 'id'" if isinstance(row, dict) else "expected a mapping"
    assert str(caught.value) == f"{side} row 2: {problem}"


@pytest.mark.parametrize("side", ["before", "after"])
def test_duplicate_keys_are_refused_before_unmatched_rows_are_discarded(side: str):
    rows = [{"id": "a", "value": 0}, {"id": "a", "value": 1}]
    with pytest.raises(ValueError, match="duplicate key") as caught:
        align_rows(rows if side == "before" else [], rows if side == "after" else [], keys=["id"])
    assert str(caught.value) == f"{side} row 2: duplicate key ('a',)"


def test_rows_are_shallow_snapshots_including_reused_mappings():
    row: dict[str, object] = {}

    def reused() -> Iterator[dict[str, object]]:
        for value in ("a", "b"):
            row["id"] = value
            yield row

    after = [{"id": "b", "nested": []}, {"id": "a"}]
    result = align_rows(reused(), after, keys=["id"])
    row["id"] = "changed"
    after[1]["id"] = "changed"
    assert result[("a",)] == ({"id": "a"}, {"id": "a"})
    assert result[("b",)][0] == {"id": "b"}
    assert result[("b",)][1] is not after[0]
    assert result[("b",)][1]["nested"] is after[0]["nested"]


def test_values_are_not_parsed_filtered_or_rounded():
    values = [2**60 + 1, 0, None, "", "not numeric", float("nan"), float("inf"), True]
    rows = [{"id": i, "value": value} for i, value in enumerate(values)]
    result = align_rows(rows, rows, keys=["id"])
    for i, value in enumerate(values):
        assert result[(i,)][0]["value"] is value
    assert result[(0,)][0]["value"] == 2**60 + 1


@pytest.mark.parametrize("side", ["before", "after"])
def test_iterator_errors_propagate(side: str):
    failure = OSError("table could not be read")

    def broken() -> Iterator[dict[str, str]]:
        yield {"id": "a"}
        raise failure

    with pytest.raises(OSError, match="table could not be read") as caught:
        align_rows(broken() if side == "before" else [], broken() if side == "after" else [], keys=["id"])
    assert caught.value is failure


@given(
    st.dictionaries(st.integers(), st.integers(), max_size=8),
    st.dictionaries(st.integers(), st.integers(), max_size=8),
    st.data(),
)
def test_alignment_matches_a_slow_join_and_ignores_after_order(
    before: dict[int, int], after: dict[int, int], data: st.DataObject
):
    old = [{"id": key, "value": value} for key, value in before.items()]
    new = [{"id": key, "value": value} for key, value in after.items()]
    shuffled = data.draw(st.permutations(new))
    expected = {(a["id"],): (a, b) for a in old for b in new if a["id"] == b["id"]}
    result = align_rows(old, shuffled, keys=["id"])
    assert result == expected
    assert list(result) == list(expected)


@given(st.one_of(st.integers(), st.text(min_size=1).filter(lambda value: bool(value.strip()))))
def test_generated_duplicate_keys_are_refused(key: str | int):
    rows = [{"id": key}, {"id": key}]
    with pytest.raises(ValueError, match="duplicate key"):
        align_rows(rows, [], keys=["id"])
