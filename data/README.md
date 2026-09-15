# Data

Hospital price transparency machine-readable files (MRFs) are self-hosted by
each hospital. There is no central index, but CMS requires every hospital to
publish `https://<domain>/cms-hpt.txt` pointing at its current file, so
discovery is one predictable request per health system.

```
seeds.csv        hand-maintained: one row per health system (domain)
sources.csv      committed provenance: one row per hospital location
raw/             the untouched MRFs                (gitignored, gigabytes)
interim/         one tidy parquet per hospital     (gitignored)
sample/          2,000 rows per hospital, committed so the repo is reproducible
```

## sources.csv

| column | meaning |
| --- | --- |
| `hospital_name`, `system`, `domain` | where it came from |
| `include` | `yes` to download and parse. Out-of-state and specialty locations are `no` with the reason in `notes` |
| `mrf_url`, `source_page_url` | as published in `cms-hpt.txt` |
| `file_format` | guessed from the URL, corrected by sniffing the bytes after download |
| `local_path`, `bytes`, `sha256`, `fetched_at` | filled in by `fetch`; the sha256 is what makes results checkable |
| `status` | `discovered`, `ok`, or an error you can act on |
| `notes` | hand-written; survives re-runs of `discover` |

Twelve domains turned out to be twelve health *systems*, which list 55
locations between them. 38 are Maryland hospitals and included. The 17
excluded are in DC, Florida or Delaware, or are not acute-care hospitals
(rehab, psychiatric, chronic care, freestanding EDs, a trauma unit that
shares its parent's file). Flip `include` to `yes`
on any of those if the analysis wants them.

UMMS returns 403 to anything that is not a browser, so its 17 rows were
captured by reading <https://www.umms.org/cms-hpt.txt> in a browser. `discover`
will report `discovery-http-403` for it and leave those rows untouched.

## Quirks found on the first pass (September 2026)

- Holy Cross spells the key `mfr-url` and serves `.zip` files from
  `hpt.trinity-health.org`. The parser reads the CSV inside without extracting.
- Mercy serves its CSV through a `.ashx` handler. The downloader sniffs the
  first bytes and renames the file to match what it actually is.
- Luminis filenames carry a ten-digit NPI where CMS expects a nine-digit EIN.
  `hospital_ein` is left null for those rather than filing a wrong number.
- Grace Medical Center shares Sinai's EIN; White Oak shares Shady Grove's.
  `hospital_name` (from the file's own metadata row) is the safe key, not EIN.
- Meritus puts a date in the filename, so the URL changes on every refresh.
  Re-run `discover` when a fetch returns 404.

## Commands

```bash
python -m md_hospital_prices discover     # refresh sources.csv from seeds.csv
python -m md_hospital_prices fetch        # download include=yes rows; resumable
python -m md_hospital_prices profile      # row counts per file, out-of-core
python -m md_hospital_prices inspect data/raw/<file>
python -m md_hospital_prices parse
python -m md_hospital_prices sample
```
