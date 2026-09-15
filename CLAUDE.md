# CLAUDE.md

Instructions for Claude working in `maryland-hospital-prices`. Read this before
touching anything in `src/md_hospital_prices/`.

## What this repo is

Every US hospital must publish a machine-readable file (MRF) of its standard
charges. Twelve Maryland hospitals produce twelve different layouts. This repo
turns them into one tidy table.

Maryland is the only all-payer rate-setting state (HSCRC), so in theory every
payer pays the same rate for the same service at the same hospital. The point
of the analysis is to test whether that holds.

## The tidy grain — do not change it without saying so

One row is one price:

> one hospital, one code, one code type, one setting, one billing class,
> one payer, one plan, one rate type.

15 columns, defined once in `schema.py` as `ChargeRow`:

```
hospital_name, hospital_ein, code, code_type, description, setting,
billing_class, payer_name, plan_name, rate_type, rate_dollar, rate_percent,
contracting_method, last_updated_on, source_file
```

`rate_type` is one of `gross`, `discounted_cash`, `negotiated`, `min`, `max`.

`schema.py` is the contract. `write.py` asserts its Arrow schema matches
`ChargeRow` field-for-field, so adding a column means editing both or the
import fails loudly. That assert is intentional. Do not delete it.

## Rules that are not negotiable

**A blank rate means no contract with that payer. It never means zero.**
`_to_float` returns `None` for empty, `N/A`, `NA`, `NULL`, `NONE`, `-`. Rows
where both `rate_dollar` and `rate_percent` are `None` are dropped, not stored
as `0.00`. A zero here would read as a free procedure and corrupt every median.

**Dollars and percentages are separate columns.** A contract expressed as
"42.5% of billed charges" is not a dollar amount. Never coerce one into the
other, and never multiply a percent by the gross charge to manufacture a dollar
figure unless the output column is clearly named as an estimate.

**Hospital-wide rates carry `payer_name = NULL`.** Gross charge, discounted
cash price, min and max are facts about the hospital, not about a payer. Do not
backfill a placeholder payer.

**Never fabricate a price, a URL, or an EIN.** Test fixtures are synthetic on
purpose. If a real value is unknown, leave it null and report it. Inventing
healthcare pricing data in a public portfolio repo is the worst possible
outcome for this project.

**Stream everything.** `pandas.read_csv()` on a real MRF will take the machine
down. Parsing is generators end to end; the parquet writer flushes every 250k
rows. If you add a step, it must not materialise the full table.

**Unknown columns get reported, never silently dropped.**
`decode_wide_header` returns an `unknown` list. A parser that quietly ignores
columns is how an analysis ships with a hole in it.

## Gathering the data

```
data/seeds.csv          hand-maintained: one row per health SYSTEM (domain)
        |  discover     GET https://<domain>/cms-hpt.txt  (CMS mandates it)
        v
data/sources.csv        committed: one row per LOCATION, include yes/no, provenance
        |  fetch        only include=yes; resumable; sniffs real format
        v
data/raw/*.csv|zip      untouched MRFs                (gitignored, GBs)
        |  parse        streaming, flat memory
        v
data/interim/*.parquet  one tidy file per hospital    (gitignored)
        |  sample
        v
data/sample/*.csv       committable slice, 2k rows per hospital
```

One domain is one health system, not one hospital. Twelve seed domains yield
55 locations; 38 are Maryland acute-care hospitals and `include=yes`. The 17
`no` rows are out of state (DC, FL, DE) or not acute care (rehab, psych,
chronic, freestanding EDs, a trauma unit inside UMMC), each with the reason
in `notes`. `discover` preserves
`include` and `notes` on re-run; never reset them.

`parse_cms_hpt` handles JSON, `key: value` blocks, markdown-ish bullets and
numbered lists, the `mfr-url` misspelling, and double-spaced files. Add a new
quirk as a test case in `tests/test_fetch.py` first.

If discovery fails for a domain (UMMS returns 403 to scripts), existing rows
for that system stay as they are. UMMS's 17 rows were captured by reading
cms-hpt.txt in a browser. Do not guess a replacement URL, do not scrape the
hospital's site for one, and do not drop rows to make the run look clean.

Known traps, all handled, all worth remembering:
- Holy Cross ships `.zip`; `parse._open_text` reads the CSV inside.
- Mercy serves through `.ashx`; `fetch.sniff_format` renames after download.
- Luminis filenames carry a 10-digit NPI, not an EIN; `ein_from_filename`
  must return `None` there, never a 9-digit slice of the NPI.
- Grace/Sinai and White Oak/Shady Grove share EINs. `hospital_name` is the key.
- Meritus dates its filename; a 404 means re-run `discover`.

Be a polite guest: one request at a time, a real User-Agent, a pause between
files. Downloads stream in 1 MiB chunks, resume with a Range header, and
save progress to `sources.csv` after every file. `profile` (DuckDB,
out-of-core) is the sanity check between `fetch` and `parse`.

## Parsing

`_find_charge_header` scans the first six rows. CMS files put hospital
metadata in rows 1 and 2; the charge table header comes after.

Two layouts, detected by `detect_format`:

- **wide** — payer and plan live inside the column name:
  `standard_charge|Aetna|Choice POS II|negotiated_dollar`. Suffixes are sorted
  into dollar / percent / methodology by the `_*_SUFFIXES` sets at the top of
  `parse.py`. New suffixes go in those sets, not in ad hoc branches.
- **tall** — literal `payer_name` / `plan_name` columns, one row per payer.
  Gross, cash, min and max repeat on every payer row, so `_parse_tall` emits
  them once per code via a `seen_hospital_wide` set. Removing that dedupe
  multiplies the hospital-wide row count by the payer count.

A single source row can carry several codes (`code|1`, `code|2`, …). Each
becomes its own tidy row. `_code_pairs` handles up to nine.

The EIN comes free from the CMS filename convention
`<EIN>_<hospital-name>_standardcharges.<ext>`.

## Normalising

`normalise.py` canonicalises payer, setting, billing class, contracting method
and code type. It is deliberately conservative: it maps what it recognises and
leaves everything else alone, then `unmapped_payers()` prints the unmatched
tail after every run so the mapping grows from evidence.

Add a pattern only after seeing the spelling in real output. Keep patterns
tight. The bare `\bunited\b` was removed on purpose because it swallowed United
Concordia, which is dental and a different company.

## Commands

```bash
python -m venv .venv && source .venv/Scripts/activate   # Git Bash on Windows
pip install -e ".[dev]"

python -m md_hospital_prices discover                    # only when URLs change
python -m md_hospital_prices fetch                       # 38 files, GBs, resumable
python -m md_hospital_prices profile                     # row counts, out-of-core
python -m md_hospital_prices inspect data/raw/<file>     # look before parsing
python -m md_hospital_prices parse
python -m md_hospital_prices sample --per-hospital 2000
pytest
```

Always run `inspect` on a file or two before a full parse. Real MRFs deviate
from the CMS templates, and finding that out on one file beats finding it out
forty minutes into a run.

## Testing

`pytest` must pass before any commit. Fixtures live in `tests/fixtures/` and
are synthetic. The wide/tall cross-check (`test_tall_and_wide_agree`) asserts
that the same prices in two layouts produce identical tidy output; if you touch
either parser, that test is the one that catches you.

When adding support for a new layout quirk, add a fixture row that exercises it
in the same commit. Never paste a real hospital's row into a fixture.

## Git conventions

- Work on a branch, open a PR, never commit to `main` directly.
- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- `data/raw/` and `data/interim/` are gitignored. `data/sample/`,
  `data/seeds.csv` and `data/sources.csv` are committed.
- Never commit a parquet file or anything over a few MB. If a commit is about
  to add megabytes of data, stop and ask.
- `git config --global core.autocrlf true` on Windows, already set.

## Working with Merton

- Drafts and options over a single finished answer. Show the tradeoff.
- Push back when something is wrong. Do not agree to be agreeable.
- Concise and plain. No em dashes.
- Flag inferences explicitly rather than filling gaps silently.
