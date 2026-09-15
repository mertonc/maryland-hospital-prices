"""Parse a CMS hospital machine-readable file into tidy ChargeRow records.

Handles the two CSV shapes CMS publishes (wide and tall). Streams row by row,
so a 1 GB file uses the same memory as a 1 KB one.

Real files deviate from the templates, so column matching is tolerant: anything
that cannot be interpreted is reported rather than silently dropped.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .schema import (
    ChargeRow,
    RATE_CASH,
    RATE_ESTIMATED,
    RATE_GROSS,
    RATE_MAX,
    RATE_MIN,
    RATE_NEGOTIATED,
)

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Suffixes CMS uses on the payer-specific columns. Values are what each one
# means to us; None means "carries no rate, used as metadata for its siblings".
_DOLLAR_SUFFIXES = {"negotiated_dollar", "negotiated_amount", ""}
_PERCENT_SUFFIXES = {"negotiated_percentage", "percent", "percentage"}
_METHOD_SUFFIXES = {"methodology", "contracting_method"}
_ESTIMATED_SUFFIXES = {"estimated_amount"}
# v3.0.0 (2026) adds these per payer; real allowed-amount statistics, worth a
# later rate_type of their own. Known and deliberately not parsed yet.
_IGNORE_SUFFIXES = {"negotiated_algorithm", "additional_payer_notes",
                    "median_amount", "10th_percentile", "90th_percentile", "count"}
_V3_STAT_PREFIXES = {"median_amount", "10th_percentile", "90th_percentile", "count"}

# exactly nine digits at the start, then a separator. A ten-digit NPI
# (Luminis) must NOT match, or we would file a fake EIN.
_EIN_RE = re.compile(r"^(\d{2}-?\d{7})(?=[_\-.\s])")


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _to_float(value: Optional[str]) -> Optional[float]:
    """Money as a number, or None. Empty means 'no contract', never zero."""
    v = _clean(value)
    if v is None:
        return None
    v = v.replace("$", "").replace(",", "").replace("%", "")
    if v.upper() in {"N/A", "NA", "NULL", "NONE", "-"}:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ein_from_filename(path: Path) -> Optional[str]:
    """CMS mandates <EIN>_<name>_standardcharges.<ext>, so the EIN is free."""
    m = _EIN_RE.match(path.name)
    if not m:
        return None
    raw = m.group(1).replace("-", "")
    return f"{raw[:2]}-{raw[2:]}"


def csv_member_of(path: Path) -> Optional[str]:
    """Name of the CSV inside a zip MRF (Holy Cross ships zips), else None."""
    if path.suffix.lower() != ".zip":
        return None
    with zipfile.ZipFile(path) as z:
        members = [m for m in z.namelist() if not m.endswith("/")]
        csvs = [m for m in members if m.lower().endswith((".csv", ".txt"))]
        if len(csvs) == 1:
            return csvs[0]
        if not csvs:
            raise ValueError(f"{path.name} contains no CSV: {members}")
        # several: prefer the one that looks like the standard charges file
        hits = [m for m in csvs if "standardcharges" in m.lower().replace("_", "").replace("-", "")]
        return (hits or csvs)[0]


@contextmanager
def _open_text(path: Path):
    """A text handle on the MRF, whether it is a bare CSV or a CSV inside a zip.

    Streams either way; nothing is extracted to disk or loaded into memory.
    """
    member = csv_member_of(path)
    if member is None:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            yield fh
        return
    with zipfile.ZipFile(path) as z, z.open(member) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="") as fh:
            yield fh


class PayerColumn:
    """One wide-format column, decoded into what it actually describes."""

    __slots__ = ("index", "payer", "plan", "kind")

    def __init__(self, index: int, payer: str, plan: str, kind: str):
        self.index = index
        self.payer = payer
        self.plan = plan
        self.kind = kind  # dollar | percent | method | estimated

    @property
    def key(self) -> tuple[str, str]:
        return (self.payer, self.plan)


def decode_wide_header(header: list[str]) -> tuple[dict[str, int], list[PayerColumn], list[str]]:
    """Split a wide header into plain columns, payer columns, and unknowns.

    Payer columns look like:
        standard_charge|Aetna|Choice POS II|negotiated_dollar
        standard_charge|CareFirst|BluePreferred|methodology
    """
    plain: dict[str, int] = {}
    payer_cols: list[PayerColumn] = []
    unknown: list[str] = []

    for i, raw in enumerate(header):
        name = raw.strip()
        lowered = name.lower()

        if "|" not in name:
            plain[lowered] = i
            continue

        parts = [p.strip() for p in name.split("|")]
        head = parts[0].lower()

        # standard_charge|gross, |discounted_cash, |min, |max
        if head == "standard_charge" and len(parts) == 2:
            plain[f"standard_charge|{parts[1].lower()}"] = i
            continue

        # code|1, code|1|type, and similar
        if head in {"code", "modifiers", "drug_unit_of_measurement",
                    "drug_type_of_measurement", "additional_generic_notes"}:
            plain[lowered] = i
            continue

        if head == "standard_charge" and len(parts) >= 3:
            payer, plan = parts[1], parts[2]
            suffix = parts[3].lower() if len(parts) > 3 else ""
            if suffix in _DOLLAR_SUFFIXES:
                payer_cols.append(PayerColumn(i, payer, plan, "dollar"))
            elif suffix in _PERCENT_SUFFIXES:
                payer_cols.append(PayerColumn(i, payer, plan, "percent"))
            elif suffix in _METHOD_SUFFIXES:
                payer_cols.append(PayerColumn(i, payer, plan, "method"))
            elif suffix in _ESTIMATED_SUFFIXES:
                payer_cols.append(PayerColumn(i, payer, plan, "estimated"))
            elif suffix in _IGNORE_SUFFIXES:
                pass
            else:
                unknown.append(name)
            continue

        if head == "additional_payer_notes":
            continue

        # CMS v3.0.0 wide files carry per-payer allowed-amount statistics as
        # median_amount|Payer|Plan, 10th_percentile|..., 90th_percentile|...,
        # count|... (prefix form, seen on MedStar). Known; not parsed yet.
        if head in _V3_STAT_PREFIXES:
            continue

        unknown.append(name)

    return plain, payer_cols, unknown


def _code_pairs(row: list[str], plain: dict[str, int]) -> list[tuple[str, str]]:
    """Every (code, code_type) on a row. A row can legitimately carry several."""
    out = []
    for i in range(1, 10):
        ci = plain.get(f"code|{i}")
        ti = plain.get(f"code|{i}|type")
        if ci is None:
            continue
        code = _clean(row[ci]) if ci < len(row) else None
        ctype = _clean(row[ti]) if ti is not None and ti < len(row) else None
        if code:
            out.append((code, (ctype or "UNKNOWN").upper()))
    return out


def _get(row: list[str], plain: dict[str, int], key: str) -> Optional[str]:
    i = plain.get(key)
    if i is None or i >= len(row):
        return None
    return _clean(row[i])


def read_metadata(path: Path) -> dict:
    """Hospital-level metadata sits in the first two rows of a CMS CSV."""
    with _open_text(path) as fh:
        reader = csv.reader(fh)
        try:
            keys = next(reader)
            vals = next(reader)
        except StopIteration:
            return {}
    meta = {}
    for k, v in zip(keys, vals):
        k = _clean(k)
        if k:
            meta[k.lower()] = _clean(v)
    return meta


def detect_format(header: list[str]) -> str:
    """Tall files name the payer in a column. Wide files name it in the header."""
    lowered = {h.strip().lower() for h in header}
    if "payer_name" in lowered:
        return "tall"
    if any(h.lower().startswith("standard_charge|") and h.count("|") >= 3 for h in header):
        return "wide"
    if any(h.lower().startswith("standard_charge|") for h in header):
        return "wide"
    return "unknown"


def _find_charge_header(path: Path, max_scan: int = 6) -> tuple[int, list[str]]:
    """Find the row that starts the charge table, skipping metadata rows."""
    with _open_text(path) as fh:
        for idx, row in enumerate(csv.reader(fh)):
            if idx >= max_scan:
                break
            lowered = [c.strip().lower() for c in row]
            if "description" in lowered and any(
                c.startswith("code") or c.startswith("standard_charge") for c in lowered
            ):
                return idx, row
    raise ValueError(f"No charge header found in the first {max_scan} rows of {path.name}")


def parse_file(path: Path) -> Iterator[ChargeRow]:
    """Stream one MRF into tidy rows. Memory use is flat regardless of size."""
    meta = read_metadata(path)
    hospital = meta.get("hospital_name") or path.stem
    updated = meta.get("last_updated_on")
    ein = ein_from_filename(path)
    header_idx, header = _find_charge_header(path)
    shape = detect_format(header)

    if shape == "wide":
        yield from _parse_wide(path, header_idx, header, hospital, ein, updated)
    elif shape == "tall":
        yield from _parse_tall(path, header_idx, header, hospital, ein, updated)
    else:
        raise ValueError(f"Unrecognised layout in {path.name}")


def _rows_after(path: Path, header_idx: int) -> Iterator[list[str]]:
    with _open_text(path) as fh:
        reader = csv.reader(fh)
        for _ in range(header_idx + 1):
            next(reader, None)
        for row in reader:
            if any(c.strip() for c in row):
                yield row


def _parse_wide(path, header_idx, header, hospital, ein, updated) -> Iterator[ChargeRow]:
    plain, payer_cols, _unknown = decode_wide_header(header)

    # group a payer/plan's dollar, percent and method columns together
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for col in payer_cols:
        grouped.setdefault(col.key, {})[col.kind] = col.index

    for row in _rows_after(path, header_idx):
        codes = _code_pairs(row, plain)
        if not codes:
            continue
        common = dict(
            hospital_name=hospital,
            hospital_ein=ein,
            description=_get(row, plain, "description"),
            setting=_get(row, plain, "setting"),
            billing_class=_get(row, plain, "billing_class"),
            last_updated_on=updated,
            source_file=path.name,
        )

        for code, ctype in codes:
            # hospital-wide rates: no payer, no plan
            for key, rate_type in (
                ("standard_charge|gross", RATE_GROSS),
                ("standard_charge|discounted_cash", RATE_CASH),
                ("standard_charge|min", RATE_MIN),
                ("standard_charge|max", RATE_MAX),
            ):
                amount = _to_float(_get(row, plain, key))
                if amount is None:
                    continue
                yield ChargeRow(code=code, code_type=ctype, payer_name=None,
                                plan_name=None, rate_type=rate_type,
                                rate_dollar=amount, rate_percent=None,
                                contracting_method=None, **common)

            # payer-specific rates
            for (payer, plan), cols in grouped.items():
                di, pi, mi, ei = cols.get("dollar"), cols.get("percent"), cols.get("method"), cols.get("estimated")
                dollar = _to_float(row[di]) if di is not None and di < len(row) else None
                percent = _to_float(row[pi]) if pi is not None and pi < len(row) else None
                method = _clean(row[mi]) if mi is not None and mi < len(row) else None
                est = _to_float(row[ei]) if ei is not None and ei < len(row) else None
                if dollar is not None or percent is not None:
                    yield ChargeRow(code=code, code_type=ctype, payer_name=payer,
                                    plan_name=plan, rate_type=RATE_NEGOTIATED,
                                    rate_dollar=dollar, rate_percent=percent,
                                    contracting_method=method, **common)
                if est is not None:
                    yield ChargeRow(code=code, code_type=ctype, payer_name=payer,
                                    plan_name=plan, rate_type=RATE_ESTIMATED,
                                    rate_dollar=est, rate_percent=None,
                                    contracting_method=method, **common)
                # both blank means no contract with this payer, not a free procedure


def _parse_tall(path, header_idx, header, hospital, ein, updated) -> Iterator[ChargeRow]:
    plain = {h.strip().lower(): i for i, h in enumerate(header)}

    def col(*names):
        for n in names:
            if n in plain:
                return plain[n]
        return None

    i_payer = col("payer_name")
    i_plan = col("plan_name")
    i_dollar = col("standard_charge|negotiated_dollar", "standard_charge|negotiated_amount")
    i_pct = col("standard_charge|negotiated_percentage", "standard_charge|percent")
    i_method = col("standard_charge|methodology", "standard_charge|contracting_method")
    i_est = col("estimated_amount")
    i_mod = col("modifiers")

    # hospital-wide rates repeat on every payer row of the same chargemaster
    # item, so dedupe per ITEM (code + description + modifiers + setting +
    # value), never per code: one CPT can map to many items with different
    # gross charges (Shady Grove has 15 items on 99213).
    seen_hospital_wide: set[tuple] = set()

    for row in _rows_after(path, header_idx):
        codes = _code_pairs(row, plain)
        if not codes:
            continue
        common = dict(
            hospital_name=hospital,
            hospital_ein=ein,
            description=_get(row, plain, "description"),
            setting=_get(row, plain, "setting"),
            billing_class=_get(row, plain, "billing_class"),
            last_updated_on=updated,
            source_file=path.name,
        )

        item = (common["description"], _get(row, plain, "modifiers") if i_mod is not None else None,
                common["setting"])

        for code, ctype in codes:
            for key, rate_type in (
                ("standard_charge|gross", RATE_GROSS),
                ("standard_charge|discounted_cash", RATE_CASH),
                ("standard_charge|min", RATE_MIN),
                ("standard_charge|max", RATE_MAX),
            ):
                amount = _to_float(_get(row, plain, key))
                if amount is None:
                    continue
                marker = (code, ctype, item, rate_type, amount)
                if marker in seen_hospital_wide:
                    continue
                seen_hospital_wide.add(marker)
                yield ChargeRow(code=code, code_type=ctype, payer_name=None,
                                plan_name=None, rate_type=rate_type,
                                rate_dollar=amount, rate_percent=None,
                                contracting_method=None, **common)

            payer = _clean(row[i_payer]) if i_payer is not None and i_payer < len(row) else None
            if not payer:
                continue
            plan = _clean(row[i_plan]) if i_plan is not None and i_plan < len(row) else None
            dollar = _to_float(row[i_dollar]) if i_dollar is not None and i_dollar < len(row) else None
            percent = _to_float(row[i_pct]) if i_pct is not None and i_pct < len(row) else None
            method = _clean(row[i_method]) if i_method is not None and i_method < len(row) else None
            est = _to_float(row[i_est]) if i_est is not None and i_est < len(row) else None
            if dollar is not None or percent is not None:
                yield ChargeRow(code=code, code_type=ctype, payer_name=payer,
                                plan_name=plan, rate_type=RATE_NEGOTIATED,
                                rate_dollar=dollar, rate_percent=percent,
                                contracting_method=method, **common)
            if est is not None:
                yield ChargeRow(code=code, code_type=ctype, payer_name=payer,
                                plan_name=plan, rate_type=RATE_ESTIMATED,
                                rate_dollar=est, rate_percent=None,
                                contracting_method=method, **common)
