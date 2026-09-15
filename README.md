# Maryland Hospital Prices

What Maryland hospitals publish in their price transparency files, how much the
price of the same procedure varies by payer, and which hospitals meet the 2026
CMS requirements.

**Status:** in progress, started September 2026. Data gathering and parsing
built; analysis not started.

## The question

Maryland is the only state with all-payer hospital rate setting (HSCRC). In
theory every payer pays the same rate for the same service at the same
hospital. The machine-readable files hospitals now have to publish let us check
whether that holds, and by how much it doesn't.

## The data

Every US hospital must publish a machine-readable file (MRF) of its standard
charges, and a pointer to it at `https://<domain>/cms-hpt.txt`. Twelve
Maryland health systems list 55 locations between them; 38 are Maryland
acute-care hospitals and in scope. See [`data/README.md`](data/README.md) for the
provenance file, what was excluded and why, and the quirks found on the first
pass.

Files run from tens of megabytes to gigabytes and come in two layouts (payer
in the column name, or payer in a column). Nothing here loads a whole file
into memory.

## Method

One row is one price:

> one hospital, one code, one code type, one setting, one billing class,
> one payer, one plan, one rate type.

| column | notes |
| --- | --- |
| `hospital_name`, `hospital_ein` | EIN from the CMS filename convention, null when the filename carries something else |
| `code`, `code_type` | CPT, HCPCS, MS-DRG, RC… a source row with several codes becomes several tidy rows |
| `setting`, `billing_class` | inpatient / outpatient; professional / facility |
| `payer_name`, `plan_name` | null for hospital-wide rates; payer names canonicalised |
| `rate_type` | `gross`, `discounted_cash`, `negotiated`, `min`, `max` |
| `rate_dollar`, `rate_percent` | kept separate; a percent-of-charges contract is not a dollar amount |
| `contracting_method` | fee schedule, case rate, per diem, percent of charges, capitation, other |
| `last_updated_on`, `source_file` | provenance on every row |

A blank rate means no contract with that payer, never zero.

```
data/seeds.csv ──discover──▶ data/sources.csv ──fetch──▶ data/raw/*.csv|zip
                                                               │
                                                             parse  (streaming)
                                                               ▼
                                                    data/interim/*.parquet
                                                               │
                                                            sample
                                                               ▼
                                                    data/sample/charges_sample.csv
```

## Results

Not yet.

## Limitations

- Hospitals deviate from the CMS template. Columns the parser cannot interpret
  are reported, not dropped, but "reported" still means a human has to look.
- Payer normalisation is rule-based and conservative. The unmatched tail is
  printed after every run and the rules grow from it.
- Two pairs of hospitals share an EIN, so `hospital_name` is the key.
- UMMS blocks scripted discovery; its 17 rows were captured from a browser.

## How to run

```bash
python -m venv .venv && source .venv/Scripts/activate   # Git Bash on Windows
pip install -e ".[dev]"
pytest                                                    # 34 tests

python -m md_hospital_prices fetch          # 38 files; resumable; hours on home wifi
python -m md_hospital_prices profile        # row counts per file via DuckDB
python -m md_hospital_prices inspect data/raw/<one file>
python -m md_hospital_prices parse
python -m md_hospital_prices sample
```

Then query without loading:

```sql
SELECT hospital_name, payer_name, median(rate_dollar) AS med, count(*) AS n
FROM 'data/interim/*.parquet'
WHERE code = '99213' AND rate_type = 'negotiated'
GROUP BY 1, 2 ORDER BY 1, n DESC;
```
