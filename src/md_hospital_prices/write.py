"""Write tidy rows out without ever holding the whole table in memory.

Parquet, in row groups of 250k. A hospital MRF that is 2 GB of CSV becomes a
few hundred MB of parquet, and DuckDB can query it straight off disk.

Also writes a small `data/sample/` slice that IS safe to commit, so the repo
is reproducible for a reader who cannot download 20 GB of source files.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterable, Iterator, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import ChargeRow

BATCH = 250_000

_ARROW_SCHEMA = pa.schema([
    ("hospital_name", pa.string()),
    ("hospital_ein", pa.string()),
    ("code", pa.string()),
    ("code_type", pa.string()),
    ("description", pa.string()),
    ("setting", pa.string()),
    ("billing_class", pa.string()),
    ("payer_name", pa.string()),
    ("plan_name", pa.string()),
    ("rate_type", pa.string()),
    ("rate_dollar", pa.float64()),
    ("rate_percent", pa.float64()),
    ("contracting_method", pa.string()),
    ("last_updated_on", pa.string()),
    ("source_file", pa.string()),
])

COLUMNS = [f.name for f in fields(ChargeRow)]
assert COLUMNS == _ARROW_SCHEMA.names, "schema.py and write.py have drifted apart"


def _to_table(batch: list[ChargeRow]) -> pa.Table:
    cols = {name: [] for name in COLUMNS}
    for row in batch:
        d = asdict(row)
        for name in COLUMNS:
            cols[name].append(d[name])
    return pa.Table.from_pydict(cols, schema=_ARROW_SCHEMA)


def write_parquet(rows: Iterable[ChargeRow], dest: Path,
                  batch_size: int = BATCH, verbose: bool = True) -> int:
    """Stream rows to a parquet file. Returns the row count."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional[pq.ParquetWriter] = None
    batch: list[ChargeRow] = []
    total = 0

    def flush():
        nonlocal writer, batch, total
        if not batch:
            return
        table = _to_table(batch)
        if writer is None:
            writer = pq.ParquetWriter(dest, _ARROW_SCHEMA, compression="zstd")
        writer.write_table(table)
        total += len(batch)
        batch = []
        if verbose:
            print(f"    {total:,} rows", end="\r", flush=True)

    try:
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                flush()
        flush()
    finally:
        if writer is not None:
            writer.close()

    if verbose:
        print(f"    {total:,} rows -> {dest.name}          ")
    return total


def take_sample(rows: Iterable[ChargeRow], per_hospital: int = 2000) -> Iterator[ChargeRow]:
    """First N rows per hospital — enough to demo the schema, small enough to commit."""
    seen: dict[str, int] = {}
    for row in rows:
        n = seen.get(row.hospital_name, 0)
        if n >= per_hospital:
            continue
        seen[row.hospital_name] = n + 1
        yield row


def write_csv(rows: Iterable[ChargeRow], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow(asdict(row))
            total += 1
    return total
