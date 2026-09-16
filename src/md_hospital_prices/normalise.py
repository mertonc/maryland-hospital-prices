"""Make payer names comparable across hospitals.

The same insurer is spelled a dozen ways across twelve files: "CareFirst",
"CAREFIRST BLUECHOICE", "Carefirst BCBS of MD", "CFBCBS". Without this step
every cross-hospital comparison silently splits one payer into many, and the
analysis is wrong in a way that looks fine.

This is deliberately conservative: it maps what it recognises and leaves
everything else alone, with `unmapped_payers()` so you can see the tail and
decide what to add. Guessing here would be worse than not mapping.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

# canonical name -> patterns that mean it
_PAYER_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Maryland's HSCRC sets one rate for everyone, and hospitals publish it
    # under a single pseudo-payer. Seen on Frederick and Shady Grove.
    ("All Payers", re.compile(r"^all\s*payers?$", re.I)),
    ("Self Pay", re.compile(r"^self[\s-]*pay$|^uninsured$", re.I)),
    ("CareFirst BlueCross BlueShield", re.compile(r"care\s*first|cfbcbs|blue\s*choice|blue\s*preferred", re.I)),
    ("Aetna", re.compile(r"\baetna\b", re.I)),
    # deliberately not a bare \bunited\b — that would swallow United Concordia
    ("UnitedHealthcare", re.compile(r"united\s*health(care)?|\buhc\b|\bumr\b|optum", re.I)),
    ("Cigna", re.compile(r"\bcigna\b", re.I)),
    ("Humana", re.compile(r"\bhumana\b", re.I)),
    ("Kaiser Permanente", re.compile(r"kaiser", re.I)),
    ("Johns Hopkins Health Plans", re.compile(r"johns\s*hopkins|priority\s*partners|\bjhhc\b|\behp\b", re.I)),
    ("Medicare", re.compile(r"\bmedicare\b", re.I)),
    ("Medicaid", re.compile(r"medicaid|\bmco\b|maryland\s*physicians\s*care", re.I)),
    ("TRICARE", re.compile(r"tricare|champus", re.I)),
    ("Veterans Affairs", re.compile(r"\bva\b|veterans", re.I)),
    ("Workers Compensation", re.compile(r"worker'?s?\s*comp", re.I)),
]

_NOISE = re.compile(r"\b(inc|llc|corp|company|of\s+maryland|md|health\s*plan[s]?)\b\.?", re.I)
_PUNCT = re.compile(r"[^a-z0-9 ]+", re.I)


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = " ".join(value.split())
    return v or None


def normalise_payer(raw: Optional[str]) -> Optional[str]:
    """Canonical payer name, or the tidied original if nothing matches."""
    v = clean_text(raw)
    if v is None:
        return None
    for canonical, pattern in _PAYER_PATTERNS:
        if pattern.search(v):
            return canonical
    return v


def payer_key(raw: Optional[str]) -> Optional[str]:
    """A join key: lowercase, punctuation and corporate noise stripped."""
    v = normalise_payer(raw)
    if v is None:
        return None
    v = _NOISE.sub(" ", v)
    v = _PUNCT.sub(" ", v)
    return " ".join(v.split()).lower() or None


_SETTINGS = {"inpatient", "outpatient", "both", "ip", "op"}


def normalise_setting(raw: Optional[str]) -> Optional[str]:
    v = clean_text(raw)
    if v is None:
        return None
    low = v.lower()
    if low in {"ip", "in", "inpatient"}:
        return "inpatient"
    if low in {"op", "out", "outpatient"}:
        return "outpatient"
    if low in {"both", "ip/op", "inpatient/outpatient"}:
        return "both"
    return low if low in _SETTINGS else low


def normalise_billing_class(raw: Optional[str]) -> Optional[str]:
    v = clean_text(raw)
    if v is None:
        return None
    low = v.lower()
    if low.startswith("prof"):
        return "professional"
    if low.startswith("fac") or low.startswith("inst"):
        return "facility"
    if low in {"both", "facility/professional"}:
        return "both"
    return low


_METHODS = {
    "case rate": re.compile(r"case\s*rate", re.I),
    "fee schedule": re.compile(r"fee\s*sched", re.I),
    "percent of total billed charges": re.compile(r"percent|%", re.I),
    "per diem": re.compile(r"per\s*diem", re.I),
    "capitation": re.compile(r"capitat", re.I),
    "other": re.compile(r"\bother\b", re.I),
}


def normalise_method(raw: Optional[str]) -> Optional[str]:
    v = clean_text(raw)
    if v is None:
        return None
    for canonical, pattern in _METHODS.items():
        if pattern.search(v):
            return canonical
    return v.lower()


def normalise_code_type(raw: Optional[str]) -> Optional[str]:
    v = clean_text(raw)
    if v is None:
        return None
    low = v.strip().upper().replace("-", "").replace(" ", "")
    return {
        "CPT": "CPT", "HCPCS": "HCPCS", "ICD10": "ICD10", "ICD10CM": "ICD10",
        "ICD10PCS": "ICD10", "MSDRG": "MS-DRG", "DRG": "MS-DRG",
        "APRDRG": "APR-DRG", "APC": "APC", "NDC": "NDC", "RC": "RC",
        "REV": "RC", "REVENUE": "RC", "EAPG": "EAPG", "LOCAL": "LOCAL",
        "TRIS": "TRIS", "CDM": "CDM",
    }.get(low, v.strip().upper())


def normalise_row(row):
    """Apply every normaliser to a ChargeRow, returning a new one."""
    from dataclasses import replace
    return replace(
        row,
        payer_name=normalise_payer(row.payer_name),
        setting=normalise_setting(row.setting),
        billing_class=normalise_billing_class(row.billing_class),
        contracting_method=normalise_method(row.contracting_method),
        code_type=normalise_code_type(row.code_type) or row.code_type,
        plan_name=clean_text(row.plan_name),
        description=clean_text(row.description),
    )


def unmapped_payers(names: Iterable[Optional[str]], top: int = 40) -> list[tuple[str, int]]:
    """Payer names no pattern claimed, commonest first. Review this every run."""
    counts: Counter = Counter()
    for n in names:
        v = clean_text(n)
        if v is None:
            continue
        if not any(p.search(v) for _c, p in _PAYER_PATTERNS):
            counts[v] += 1
    return counts.most_common(top)
