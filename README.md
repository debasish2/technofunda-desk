# Dalal Desk

A personal Indian stock screener and chart desk, built on free data only (NSE and BSE filings, Yahoo Finance).
Pages: **Desk** (chart, fundamentals, research panel), **Screener**, **Market breadth**, **Industries**.

Server: FastAPI + SQLite. Front end: plain HTML/JS with TradingView's free Lightweight Charts (kept in `vendor/`).

## Run it

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start-desk.bat
```

`start-desk.bat` starts the server on port 8000 and opens the site. To reach it from a phone, see `scripts/allow_local_network.ps1`
(one firewall rule) and the Tailscale notes below.

## Data

The `data/` folder (SQLite databases, logs, caches) is **not** in this repository: it is rebuilt from the free sources.

```bat
.venv\Scripts\python -m backend.nightly            # prices, classification, saved screens, disclosures (daily)
.venv\Scripts\python -m backend.nightly --weekly   # adds fundamentals and standalone figures
```

`scripts/register_nightly.ps1` schedules both. The first full load of prices takes a while (about 1,800 stocks).
Your hand-made industry themes are `data/themes.json` and are kept here.

## Accuracy programme

Quarterly figures come from NSE XBRL filings (to Dec 2024), BSE result PDFs read by `backend/sources/filing.py`, and Yahoo Finance where
neither is available. `python -m backend.verify` compares Yahoo's rows with the filings; `python -m backend.reconcile --apply`
stores the filing's figures where they are shown to be right. Figures with no filing behind them carry a dagger on the Desk and Screener.

## Not in the repository

* Your browser-side settings (chart view, drawings, Screener columns and filters, saved screens) live in the browser's local storage.
* Any `.env` file (for example an EODHD key) is ignored.
