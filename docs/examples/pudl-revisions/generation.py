# /// script
# requires-python = ">=3.12"
# dependencies = ["sourceledger==0.6.0", "pyarrow==26.0.1"]
# ///
"""Build a downstream table from each recorded PUDL release."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.compute as pc
import pyarrow.parquet as pq

import sourceledger

if TYPE_CHECKING:
    from sourceledger import Source


def annual_generation(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, int]]]:
    """Sum reported net generation to the nearest MWh for three report years."""
    table = pq.read_table(BytesIO(payload), columns=["report_date", "net_generation_mwh"])
    years = pc.year(table["report_date"])
    rows = []
    for year in (2023, 2024, 2025):
        generation = table["net_generation_mwh"].filter(pc.equal(years, year))
        total = pc.sum(generation).as_py()
        if total is None:
            msg = f"{source['id']}: no reported net generation for {year}"
            raise ValueError(msg)
        rows.append({"year": year, "net_generation_mwh": round(total)})
    return ["year", "net_generation_mwh"], rows


if __name__ == "__main__":
    sourceledger.entry(
        parse=annual_generation,
        manifest=Path(__file__).with_name("generation-sources.toml"),
        prog="pudl-generation",
    )
