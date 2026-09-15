# Data

Hospital price transparency machine-readable files are self-hosted by each
hospital. There is no central index, so every source is recorded by hand in
`sources.csv` with the URL and the date it was fetched.

Raw files are not committed. They are large and they change.

Run `python -m md_hospital_prices.fetch` to download them into `data/raw/`.
