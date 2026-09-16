"""Command line entry point.

    python -m md_hospital_prices discover            seeds.csv -> sources.csv
    python -m md_hospital_prices fetch               sources.csv -> data/raw/
    python -m md_hospital_prices inspect <file>      header + layout, no parsing
    python -m md_hospital_prices profile             row counts per file via DuckDB
    python -m md_hospital_prices parse               data/raw/ -> data/interim/*.parquet
    python -m md_hospital_prices report              one line per parsed hospital
    python -m md_hospital_prices query "<sql>"       SQL against the parquet; table is `charges`
    python -m md_hospital_prices sample              small committable CSV slice
"""

from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path

from .fetch import discover_all, fetch_all
from .normalise import normalise_row, unmapped_payers
from .parse import (
    _find_charge_header,
    csv_member_of,
    decode_wide_header,
    detect_format,
    parse_file,
    read_metadata,
)
from .write import take_sample, write_csv, write_parquet

ROOT = Path.cwd()
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
SAMPLE = DATA / "sample"
SEEDS = DATA / "seeds.csv"
SOURCES = DATA / "sources.csv"

RAW_SUFFIXES = {".csv", ".txt", ".zip"}


def _raw_files() -> list[Path]:
    return sorted(p for p in RAW.glob("*") if p.suffix.lower() in RAW_SUFFIXES and not p.name.startswith("."))


def cmd_discover(args) -> int:
    if not SEEDS.exists():
        print(f"Missing {SEEDS}. It needs columns: system,domain")
        return 1
    discover_all(SEEDS, SOURCES, pause=args.pause)
    return 0


def cmd_fetch(args) -> int:
    if not SOURCES.exists():
        print(f"Missing {SOURCES}. Run `discover` first, or fill it in by hand.")
        return 1
    fetch_all(SOURCES, RAW, pause=args.pause, force=args.force, only=args.only)
    return 0


def cmd_inspect(args) -> int:
    path = Path(args.path)
    meta = read_metadata(path)
    idx, header = _find_charge_header(path)
    layout = detect_format(header)
    print(f"file        {path.name}  ({path.stat().st_size/1e6:.1f} MB on disk)")
    if path.suffix.lower() == ".zip":
        print(f"zip member  {csv_member_of(path)}")
    print(f"hospital    {meta.get('hospital_name')}")
    print(f"updated     {meta.get('last_updated_on')}")
    print(f"version     {meta.get('version')}")
    print(f"layout      {layout}  (header on row {idx})")
    print(f"columns     {len(header)}")
    if layout == "wide":
        plain, payer_cols, unknown = decode_wide_header(header)
        payers = sorted({c.payer for c in payer_cols})
        plans = sorted({c.key for c in payer_cols})
        print(f"payers      {len(payers)}   plans {len(plans)}   payer columns {len(payer_cols)}")
        print(f"plain cols  {sorted(plain)}")
        if unknown:
            print(f"UNKNOWN     {unknown}")
        print("payers:")
        for p in payers[:60]:
            print(f"    {p}")
        if len(payers) > 60:
            print(f"    ... and {len(payers)-60} more")
    else:
        for h in header[:60]:
            print(f"    {h}")
        if len(header) > 60:
            print(f"    ... and {len(header)-60} more")
    return 0


def cmd_profile(args) -> int:
    """Row counts and distinct codes per raw file, without loading anything.

    DuckDB reads CSV out-of-core, so this works on a 5 GB file on a laptop.
    It is the sanity check to run between `fetch` and `parse`.
    """
    try:
        import duckdb
    except ImportError:
        print("pip install duckdb")
        return 1

    files = _raw_files()
    if not files:
        print(f"No files in {RAW}. Run `fetch` first.")
        return 1

    con = duckdb.connect()
    print(f"{'file':<70} {'MB':>8} {'rows':>12} {'cols':>5} layout")
    for path in files:
        try:
            idx, header = _find_charge_header(path)
            layout = detect_format(header)
            src = path
            if path.suffix.lower() == ".zip":
                # DuckDB cannot read inside a zip; stream the member to a temp file
                import shutil, tempfile, zipfile
                member = csv_member_of(path)
                tmp = Path(tempfile.gettempdir()) / member
                with zipfile.ZipFile(path) as z, z.open(member) as r, tmp.open("wb") as w:
                    shutil.copyfileobj(r, w, 1 << 20)
                src = tmp
            n = con.execute(
                "select count(*) from read_csv(?, skip=?, header=true, all_varchar=true, "
                "ignore_errors=true, sample_size=-1)",
                [str(src), idx],
            ).fetchone()[0]
            print(f"{path.name[:70]:<70} {path.stat().st_size/1e6:8.1f} {n:12,} {len(header):5} {layout}")
        except Exception as exc:
            print(f"{path.name[:70]:<70}  FAILED: {type(exc).__name__}: {str(exc)[:80]}")
    return 0


def cmd_parse(args) -> int:
    files = _raw_files()
    if args.only:
        files = [f for f in files if args.only.lower() in f.name.lower()]
    if not files:
        print(f"No files in {RAW}. Run `fetch` first.")
        return 1

    INTERIM.mkdir(parents=True, exist_ok=True)
    grand = 0
    leftovers: list[str] = []

    for path in files:
        dest = INTERIM / (path.stem + ".parquet")
        if dest.exists() and not args.force:
            print(f"[skip]  {path.name} (parquet exists; --force to redo)")
            continue
        print(f"[parse] {path.name}")

        def rows():
            for row in parse_file(path):
                clean = normalise_row(row)
                if row.payer_name:
                    leftovers.append(row.payer_name)
                yield clean

        # write to a .part and rename on success, so an interrupted run never
        # leaves a half-written parquet that the next run mistakes for done
        part = dest.with_name(dest.name + ".part")
        try:
            grand += write_parquet(rows(), part)
            part.replace(dest)
        except Exception as exc:  # one bad file must not kill the run
            print(f"    FAILED: {type(exc).__name__}: {exc}")
        finally:
            if part.exists():
                part.unlink()

    print(f"\n{grand:,} tidy rows across {len(files)} files -> {INTERIM}")

    tail = unmapped_payers(leftovers)
    if tail:
        print("\nPayer names no rule matched (add rules in normalise.py as needed):")
        for name, n in tail[:25]:
            print(f"    {n:>9,}  {name}")
    return 0


def cmd_report(args) -> int:
    """One line per hospital from the parsed parquet: what we have, how fresh,
    how many payers. The first thing to look at after `parse`."""
    try:
        import duckdb
    except ImportError:
        print("pip install duckdb")
        return 1
    files = sorted(INTERIM.glob("*.parquet"))
    if not files:
        print(f"No parquet in {INTERIM}. Run `parse` first.")
        return 1
    con = duckdb.connect()
    glob = str(INTERIM / "*.parquet").replace("\\", "/")
    sql = f"""
      select hospital_name,
             any_value(last_updated_on)                                   as updated,
             count(*)                                                      as rows,
             count(distinct code || '|' || code_type)                      as codes,
             count(distinct payer_name)                                    as payers,
             count(distinct plan_name)                                     as plans,
             sum(case when rate_type='negotiated' then 1 else 0 end)       as negotiated,
             sum(case when rate_type='estimated'  then 1 else 0 end)       as estimated,
             round(median(case when rate_type='gross' then rate_dollar end), 2) as median_gross
      from read_parquet('{glob}')
      group by 1 order by 1
    """
    rows = con.execute(sql).fetchall()
    hdr = ("hospital", "updated", "rows", "codes", "payers", "plans", "negotiated", "estimated", "median_gross")
    print(f"{hdr[0]:<48} {hdr[1]:>10} {hdr[2]:>10} {hdr[3]:>7} {hdr[4]:>6} {hdr[5]:>5} {hdr[6]:>10} {hdr[7]:>9} {hdr[8]:>12}")
    for r in rows:
        name = (r[0] or "")[:48]
        print(f"{name:<48} {str(r[1] or ''):>10} {r[2]:>10,} {r[3]:>7,} {r[4]:>6} {r[5]:>5} {r[6]:>10,} {r[7]:>9,} {str(r[8]):>12}")
    tot = con.execute(f"select count(*) from read_parquet('{glob}')").fetchone()[0]
    print(f"\n{len(rows)} hospitals, {tot:,} tidy rows. Query them with duckdb: select * from '{glob}' limit 10")
    return 0


def cmd_query(args) -> int:
    """Run SQL against the parsed parquet. The table is called `charges`.

        python -m md_hospital_prices query "select count(*) from charges"
        python -m md_hospital_prices query -f notebooks/q1.sql
        python -m md_hospital_prices query            # interactive prompt
    """
    try:
        import duckdb
    except ImportError:
        print("pip install duckdb")
        return 1
    if not list(INTERIM.glob("*.parquet")):
        print(f"No parquet in {INTERIM}. Run `parse` first.")
        return 1
    con = duckdb.connect()
    glob = str(INTERIM / "*.parquet").replace("\\", "/")
    con.execute(f"create view charges as select * from read_parquet('{glob}')")

    def run(sql: str) -> None:
        sql = sql.strip().rstrip(";")
        if not sql:
            return
        try:
            rel = con.sql(sql)
            if rel is None:
                print("ok")
                return
            if args.csv:
                import csv as _csv
                w = _csv.writer(sys.stdout)
                w.writerow(rel.columns)
                for row in rel.fetchall():
                    w.writerow(row)
            else:
                rel.limit(args.limit).show(max_width=200, max_rows=args.limit)
        except Exception as exc:
            print(f"error: {exc}")

    if args.file:
        run(Path(args.file).read_text())
    elif args.sql:
        run(" ".join(args.sql))
    else:
        print("SQL on `charges`. Blank line runs; Ctrl-D or `exit` quits.")
        buf: list[str] = []
        while True:
            try:
                line = input("charges> " if not buf else "     ...> ")
            except EOFError:
                break
            if line.strip().lower() in {"exit", "quit"}:
                break
            if not line.strip():
                run("\n".join(buf)); buf = []
            else:
                buf.append(line)
        if buf:
            run("\n".join(buf))
    return 0


def cmd_sample(args) -> int:
    files = _raw_files()
    if not files:
        print(f"No files in {RAW}. Run `fetch` first.")
        return 1

    from itertools import islice

    def all_rows():
        # stop reading each file once we have enough; otherwise this would
        # re-parse 5 GB to keep 2,000 rows per hospital
        for path in files:
            for row in islice(parse_file(path), args.per_hospital):
                yield normalise_row(row)

    n = write_csv(take_sample(all_rows(), args.per_hospital), SAMPLE / "charges_sample.csv")
    print(f"{n:,} rows -> {SAMPLE/'charges_sample.csv'} (safe to commit)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="md_hospital_prices")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="read each domain's cms-hpt.txt into data/sources.csv")
    d.add_argument("--pause", type=float, default=1.0)
    d.set_defaults(func=cmd_discover)

    f = sub.add_parser("fetch", help="download included rows of data/sources.csv into data/raw/")
    f.add_argument("--pause", type=float, default=2.0)
    f.add_argument("--force", action="store_true", help="re-download files already marked ok")
    f.add_argument("--only", help="substring of hospital_name to limit the run")
    f.set_defaults(func=cmd_fetch)

    i = sub.add_parser("inspect", help="show one file's shape without parsing it")
    i.add_argument("path")
    i.set_defaults(func=cmd_inspect)

    pr = sub.add_parser("profile", help="row counts per raw file via DuckDB (out-of-core)")
    pr.set_defaults(func=cmd_profile)

    pa_ = sub.add_parser("parse", help="parse data/raw/ into tidy parquet")
    pa_.add_argument("--force", action="store_true")
    pa_.add_argument("--only", help="substring of file name to limit the run")
    pa_.set_defaults(func=cmd_parse)

    r = sub.add_parser("report", help="one line per parsed hospital")
    r.set_defaults(func=cmd_report)

    q = sub.add_parser("query", help="run SQL against the parsed parquet (table: charges)")
    q.add_argument("sql", nargs="*", help="SQL text; omit for an interactive prompt")
    q.add_argument("-f", "--file", help="read SQL from a file")
    q.add_argument("--limit", type=int, default=50, help="rows to display (default 50)")
    q.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    q.set_defaults(func=cmd_query)

    s = sub.add_parser("sample", help="write a small committable CSV slice")
    s.add_argument("--per-hospital", type=int, default=2000)
    s.set_defaults(func=cmd_sample)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
