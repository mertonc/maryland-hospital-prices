"""Find and download each hospital's machine-readable file.

CMS requires every hospital to publish a plain-text file at the root of its
domain, https://<domain>/cms-hpt.txt, that points at the current MRF. That
turns discovery from "hunt around the website" into one predictable GET.

What the real files taught us (September 2026, twelve Maryland domains):

  * one domain usually means one health SYSTEM, not one hospital. Johns
    Hopkins lists six locations, MedStar lists ten hospitals plus 69 physical
    therapy sites, LifeBridge five. So discovery returns a list.
  * some of those locations are not in Maryland (Sibley and Georgetown are
    in DC, All Children's is in Florida). Hence the `include` column.
  * Holy Cross spells the key `mfr-url` and serves a .zip.
  * Mercy serves the CSV through a .ashx handler with no useful extension.
  * UMMS returns 403 to anything that is not a browser.

Two commands, both idempotent:

    discover   seeds.csv -> sources.csv   (one row per location, include=yes)
    fetch      sources.csv -> data/raw/   (only include=yes, resumes partials)

`sources.csv` is committed. It is the provenance record: where each file came
from, when, how big, and its sha256. Edit the `include` and `notes` columns by
hand; `discover` preserves them on re-run.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = (
    "md-hospital-prices/0.1 (+https://github.com/mertonc/maryland-hospital-prices; "
    "portfolio research)"
)
TIMEOUT = 90
CHUNK = 1 << 20  # 1 MiB

SOURCE_COLUMNS = [
    "hospital_name", "system", "domain", "include", "mrf_url", "source_page_url",
    "file_format", "local_path", "bytes", "sha256", "fetched_at", "status", "notes",
]

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
_MRF_HINT = re.compile(r"standard[-_]?charges|standardcharges|\bhpt\b|price[-_]?transparency", re.I)

# keys hospitals actually use, including the typo Holy Cross ships
_NAME_KEYS = {"location-name", "hospital-name", "location", "name"}
_MRF_KEYS = {"mrf-url", "mfr-url", "mrf", "machine-readable-file", "machine-readable-file-url"}
_PAGE_KEYS = {"source-page-url", "source-page", "source"}


class Location:
    __slots__ = ("name", "mrf_url", "source_page_url")

    def __init__(self, name: Optional[str], mrf_url: Optional[str], source_page_url: Optional[str]):
        self.name = name
        self.mrf_url = mrf_url
        self.source_page_url = source_page_url

    def __repr__(self):
        return f"Location({self.name!r}, {self.mrf_url!r})"


# --- discovery -------------------------------------------------------------

_NUMBERING = re.compile(r"^\d+[.)]\s*")


def _norm_key(k: str) -> str:
    """'Location Name' / 'location_name' / '**MRF URL**' -> 'location-name' / 'mrf-url'."""
    k = k.strip().strip("*_#` ").lower()
    return re.sub(r"[\s_]+", "-", k)


def parse_cms_hpt(text: str, base: str = "") -> list[Location]:
    """Every location in a cms-hpt.txt.

    Handles: JSON (object or array), `key: value` blocks separated by blank
    lines, markdown-ish bullets (`- MRF: url`), and as a last resort any URL
    that looks like a standard-charges file.
    """
    stripped = text.strip().lstrip("﻿")
    if not stripped:
        return []

    # 1. JSON
    if stripped[0] in "{[":
        try:
            data = json.loads(stripped)
            items = data if isinstance(data, list) else [data]
            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                flat = {_norm_key(k): v for k, v in item.items()}
                mrf = next((flat[k] for k in _MRF_KEYS if k in flat), None)
                if mrf:
                    out.append(Location(
                        name=next((flat[k] for k in _NAME_KEYS if k in flat), None),
                        mrf_url=urljoin(base, str(mrf)),
                        source_page_url=next((urljoin(base, str(flat[k])) for k in _PAGE_KEYS if k in flat), None),
                    ))
            if out:
                return out
        except json.JSONDecodeError:
            pass

    # 2. key: value, one location per block.
    #    A block ends at a blank line once it has an mrf-url, or when a new
    #    name/mrf-url shows up. Blank lines before the url are kept (Mercy
    #    double-spaces every line). The first name wins (Adventist gives a
    #    friendly heading and then a legal `Location:` line).
    out: list[Location] = []
    cur: dict[str, str] = {}

    def flush():
        if cur.get("mrf"):
            out.append(Location(cur.get("name"), urljoin(base, cur["mrf"]), cur.get("page")))
        cur.clear()

    for raw in stripped.splitlines():
        if raw.lstrip().startswith("#"):
            continue  # a document title, not a location
        line = _NUMBERING.sub("", raw.strip().lstrip("-*•").strip())
        if not line:
            if cur.get("mrf"):
                flush()
            continue
        key, sep, value = line.partition(":")
        key = _norm_key(key) if sep else ""
        value = value.strip().strip("*_").strip()

        if key in _MRF_KEYS and value:
            if cur.get("mrf"):
                flush()
            cur["mrf"] = value
        elif key in _NAME_KEYS and value:
            if cur.get("mrf"):
                flush()
            cur.setdefault("name", value)
        elif key in _PAGE_KEYS and value:
            cur["page"] = value
        elif not sep or key in {"http", "https"}:
            # a bare heading like "**Sinai Hospital of Baltimore**" names the block
            heading = line.strip("*#_` ").strip()
            if heading and not _URL_RE.search(heading):
                if cur.get("mrf"):
                    flush()
                cur.setdefault("name", heading)
    flush()
    if out:
        return out

    # 3. bare URLs
    urls = _URL_RE.findall(stripped)
    return [Location(None, u, None) for u in urls if _MRF_HINT.search(u)]


def discover_domain(domain: str, session: Optional[requests.Session] = None) -> tuple[list[Location], str]:
    """(locations, status) for one domain."""
    s = session or requests.Session()
    host = domain if domain.startswith("http") else f"https://{domain}"
    url = urljoin(host + "/", "cms-hpt.txt")
    try:
        r = s.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
    except requests.RequestException as exc:
        return [], f"discovery-error: {type(exc).__name__}"
    if r.status_code != 200:
        return [], f"discovery-http-{r.status_code}"
    locs = parse_cms_hpt(r.text, r.url)
    if not locs:
        return [], "discovery-no-mrf-url"
    return locs, "discovered"


# --- sources.csv -----------------------------------------------------------

def read_sources(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    # tolerate an older header: only keep known columns, fill the rest
    return [{c: (r.get(c) or "") for c in SOURCE_COLUMNS} for r in rows if (r.get("hospital_name") or "").strip()]


def write_sources(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SOURCE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in SOURCE_COLUMNS})


def read_seeds(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("domain") or "").strip()]


def discover_all(seeds_path: Path, sources_path: Path, pause: float = 1.0, verbose: bool = True) -> list[dict]:
    """Refresh sources.csv from every seed domain. Keeps hand edits."""
    existing = {r["mrf_url"]: r for r in read_sources(sources_path)}
    session = requests.Session()
    out: list[dict] = []

    for seed in read_seeds(seeds_path):
        domain = seed["domain"].strip()
        system = (seed.get("system") or seed.get("hospital_name") or domain).strip()
        if verbose:
            print(f"[discover] {domain}")
        locs, status = discover_domain(domain, session)
        if not locs:
            # keep whatever we already had for this domain (UMMS rows were
            # captured by hand because the site 403s scripts); never drop them
            kept = [r for r in existing.values() if r["domain"] == domain]
            if verbose:
                print(f"           {status}; keeping {len(kept)} existing row(s)")
            if kept:
                out.extend(kept)
            else:
                row = {c: "" for c in SOURCE_COLUMNS}
                row.update(hospital_name=system, system=system, domain=domain,
                           include="yes", status=status,
                           notes="discovery failed; open cms-hpt.txt in a browser and paste the mrf-url here")
                out.append(row)
            continue
        for loc in locs:
            prev = existing.get(loc.mrf_url, {})
            row = {c: "" for c in SOURCE_COLUMNS}
            row.update(prev)
            row.update({
                "hospital_name": prev.get("hospital_name") or loc.name or system,
                "system": system, "domain": domain,
                "include": prev.get("include") or "yes",
                "mrf_url": loc.mrf_url,
                "source_page_url": loc.source_page_url or prev.get("source_page_url", ""),
                "file_format": prev.get("file_format") or _guess_format(loc.mrf_url),
                "status": prev.get("status") or "discovered",
            })
            out.append(row)
        if verbose:
            print(f"           {len(locs)} location(s)")
        time.sleep(pause)

    write_sources(sources_path, out)
    if verbose:
        print(f"\n{len(out)} rows -> {sources_path}. Review the `include` column before `fetch`.")
    return out


# --- download --------------------------------------------------------------

def _guess_format(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    return ext if ext in {"csv", "json", "zip", "xlsx", "txt"} else "unknown"


def sniff_format(path: Path) -> str:
    """What a file actually is, from its first bytes. Extensions lie."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text[:1] in (b"{", b"["):
        return "json"
    if b"," in head or b"|" in head:
        return "csv"
    return "unknown"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _filename_for(row: dict) -> str:
    tail = Path(urlparse(row["mrf_url"]).path).name
    ext = Path(tail).suffix.lower()
    stem = Path(tail).stem if tail else _slug(row["hospital_name"])
    if ext not in {".csv", ".json", ".zip", ".xlsx", ".txt"}:
        ext = ".bin"  # will be renamed after sniffing
    return f"{stem}{ext}"


def download(row: dict, raw_dir: Path, session: Optional[requests.Session] = None,
             verbose: bool = True) -> dict:
    """Stream one MRF to disk, resuming a partial download if one exists.

    Returns the row updated with local_path/bytes/sha256/fetched_at/status.
    """
    s = session or requests.Session()
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / _filename_for(row)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": USER_AGENT}

    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"

    try:
        with s.get(row["mrf_url"], timeout=TIMEOUT, stream=True, headers=headers) as r:
            if r.status_code == 416:      # server says we already have it all
                pass
            elif r.status_code == 206:    # resuming
                mode = "ab"
            elif r.status_code == 200:
                mode = "wb"; have = 0     # server ignored Range; start over
            else:
                row["status"] = f"download-http-{r.status_code}"
                return row
            if r.status_code in (200, 206):
                total = int(r.headers.get("Content-Length") or 0) + have
                done = have
                with part.open(mode) as fh:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            if verbose and total:
                                print(f"           {done/1e6:8.1f} / {total/1e6:.1f} MB", end="\r", flush=True)
    except requests.RequestException as exc:
        row["status"] = f"download-error: {type(exc).__name__} (re-run to resume)"
        return row

    # finalise: sniff real format, rename, hash
    fmt = sniff_format(part)
    if fmt == "zip":
        # keep the archive; parse.py opens the CSV inside it without extracting
        dest = dest.with_suffix(".zip")
    elif fmt in {"csv", "json"} and dest.suffix.lower() not in {f".{fmt}"}:
        dest = dest.with_suffix(f".{fmt}")
    if dest.exists():
        dest.unlink()
    part.rename(dest)

    digest = hashlib.sha256()
    size = 0
    with dest.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
            size += len(block)

    row.update({
        "local_path": str(dest.as_posix()),
        "bytes": str(size),
        "sha256": digest.hexdigest(),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_format": fmt if fmt != "unknown" else row.get("file_format", "unknown"),
        "status": "ok" if fmt in {"csv", "zip", "json"} else f"ok-but-{fmt}",
    })
    if fmt == "zip":
        with zipfile.ZipFile(dest) as z:
            members = [m for m in z.namelist() if not m.endswith("/")]
        row["notes"] = (row.get("notes") + "; " if row.get("notes") else "") + f"zip contains {members}"
    if verbose:
        print(f"           {size/1e6:8.1f} MB -> {dest.name}  [{fmt}]          ")
    return row


def fetch_all(sources_path: Path, raw_dir: Path, pause: float = 2.0,
              force: bool = False, only: Optional[str] = None, verbose: bool = True) -> list[dict]:
    rows = read_sources(sources_path)
    if not rows:
        raise SystemExit(f"{sources_path} is empty. Run `discover` first.")
    session = requests.Session()

    for row in rows:
        if row["include"].strip().lower() not in {"yes", "y", "true", "1"}:
            continue
        if only and only.lower() not in row["hospital_name"].lower():
            continue
        if not row["mrf_url"].strip():
            if verbose:
                print(f"[no url]   {row['hospital_name']}: {row['notes'] or row['status']}")
            continue
        if row["status"] == "ok" and not force and row["local_path"] and Path(row["local_path"]).exists():
            if verbose:
                print(f"[skip]     {row['hospital_name']} (already fetched; --force to redo)")
            continue
        if verbose:
            print(f"[download] {row['hospital_name']}")
            print(f"           {row['mrf_url']}")
        download(row, raw_dir, session, verbose)
        write_sources(sources_path, rows)  # save progress after every file
        time.sleep(pause)

    ok = sum(1 for r in rows if r["status"] == "ok")
    wanted = sum(1 for r in rows if r["include"].strip().lower() in {"yes", "y", "true", "1"})
    if verbose:
        print(f"\n{ok}/{wanted} included files on disk. Provenance in {sources_path}")
    return rows
