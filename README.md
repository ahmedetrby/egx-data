# EGX Data

Daily data from the Egyptian Exchange (EGX), collected automatically by a
GitHub Actions workflow every trading day (Sunday–Thursday) after the close.

## What it collects

The scraper opens the EGX investor-categories page in a real Chromium
(Playwright) so the site's protection challenge passes, then calls the
site's own `WebService.asmx` endpoints from inside the page session:

- `GetInvestorTables` — buy / sell / net values per nationality
  (Egyptians / Arabs / Foreigners) and per group (Total / Individuals /
  Institutions), split by instrument:
  - **SB=2** stocks · **SB=3** bonds · **SB=4** treasury bills
  - **SB=1** all instruments (kept for continuity with v1 history)
- `InvestorNatColumnChart` — each nationality's share of trading (%)
- The site's Indices page — EGX 30 / EGX 70 / EGX 100 closes and daily % change
- Central Bank of Egypt — official USD buy/sell rate for the day

Before anything is written, the scraper validates that the three
nationality nets per instrument sum to zero, and retries the whole fetch
up to 3 times. If the data cannot be fetched, the run fails and no file
is touched — there are never partial or guessed rows.

## Files

| File | Contents |
| --- | --- |
| `EGX_History.csv` | One row per day, wide format: total (all instruments) buy/sell/net by group × nationality. History starts 2026-03-19. |
| `EGX_Instruments.csv` | Long format per instrument (stocks / bonds / T-bills): date, instrument, group, nationality, buy, sell, net, share of trading %. |
| `EGX_Indices.csv` | One row per day: EGX 30/70/100 closes + % change, CBE USD buy/sell and the rate's date. |
| `scraper.py` | The scraper (Playwright + pandas). |
| `.github/workflows/main.yml` | Daily schedule (Sun–Thu 14:30 UTC) and commit-back of the CSVs. |

Dates are Africa/Cairo dates. CSVs are UTF-8 with BOM so Arabic headers
open correctly in Excel.

## Run it locally

```bash
pip install -r requirements.txt
playwright install chromium
python scraper.py
```
