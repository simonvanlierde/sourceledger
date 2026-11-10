"""Compare one PUDL table across two releases, and carry the change into a result.

Needs pyarrow only. Run it on the two raw files that `sourceledger fetch` stored:

    uv run --no-project --with pyarrow python compare.py v2026.9.0.parquet v2026.10.0.parquet

sourceledger says that the file changed and which release it came from. Everything below
is outside its scope: the row diff, the schema diff and the link to a number.
"""

import sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

KEY = ["plant_id_eia", "generator_id", "report_date"]  # unique in both releases
# kg CO2 per MMBtu burned, 40 CFR 98 Table C-1, rounded. Direct operational emissions at the
# plant, not life-cycle: no mining, transport, construction or methane leakage.
FACTOR = {"coal": 95.0, "gas": 53.06, "oil": 73.96}

old_path, new_path = sys.argv[1:3]
old, new = pq.read_table(old_path), pq.read_table(new_path)

print(
    "columns", old.num_columns, "->", new.num_columns, "| added:", sorted(set(new.column_names) - set(old.column_names))
)


# Row diff on the key: plant_id_pudl is not stable between releases, so it is not used.
def tag(t, n):
    return t.select(KEY).append_column(n, pa.array([True] * t.num_rows))


keys = tag(old, "in_old").join(tag(new, "in_new"), KEY, join_type="full outer")
print("keys added", pc.sum(pc.is_null(keys["in_old"])).as_py(), "| removed", pc.sum(pc.is_null(keys["in_new"])).as_py())

# Value diff on the rows both releases have.
both = old.join(new, KEY, join_type="inner", right_suffix="_new")
for column in ("net_generation_mwh", "total_mmbtu"):
    a, b = both[column], both[column + "_new"]
    tolerance = pc.multiply(pc.max_element_wise(pc.abs(a), pc.abs(b), 1.0), 1e-9)  # ignore float noise
    changed = pc.fill_null(pc.greater(pc.abs(pc.subtract(a, b)), tolerance), False)
    changed = pc.or_(changed, pc.xor(pc.is_null(a), pc.is_null(b)))
    years = pc.min_max(pc.year(both.filter(changed)["report_date"]))
    print(f"{column}: {pc.sum(changed).as_py()} changed, in report years {years['min']}-{years['max']}")

# 2025 rows by data_maturity: provisional in the old release, final in the new one.
for name, table in (("old", old), ("new", new)):
    year = table.filter(pc.equal(pc.year(table["report_date"]), 2025))
    print(
        name,
        "2025 data_maturity:",
        {v["values"]: v["counts"] for v in pc.value_counts(year["data_maturity"]).to_pylist()},
    )


def co2_kt(table):
    """US direct operational CO2 in kt by report year, coal, gas and oil only."""
    table = table.filter(pc.is_in(pc.cast(table["fuel_type_code_pudl"], pa.string()), pa.array(list(FACTOR))))
    fuel = pc.cast(table["fuel_type_code_pudl"], pa.string())
    factor = pc.index_in(fuel, pa.array(list(FACTOR)))  # position in FACTOR
    kg = pc.multiply(table["total_mmbtu"], pc.take(pa.array(list(FACTOR.values())), factor))
    by_year = (
        pa.table({"year": pc.year(table["report_date"]), "kg": kg})
        .drop_null()
        .group_by("year")
        .aggregate([("kg", "sum")])
    )
    return {y: kg / 1e6 for y, kg in zip(by_year["year"].to_pylist(), by_year["kg_sum"].to_pylist())}


before, after = co2_kt(old), co2_kt(new)
print("\nyear  old kt      new kt      change")
for year in (2023, 2024, 2025):
    print(f"{year}  {before[year]:>10,.0f}  {after[year]:>10,.0f}  {after[year] / before[year] - 1:+.2%}")
