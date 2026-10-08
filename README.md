# TechnoFunda Desk

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

## Login

Until the first user exists the app is open (as it always was). To turn the login on, open **http://localhost:8000/setup on the PC that runs the server**,
choose a user name and a password (8+ characters), and you are signed in. From then on every page and every data call, on this PC, on a phone and through any
tunnel, needs a login. Sessions last 30 days; there is a **Sign out** button in the menu.

More people: `.venv\Scripts\python -m backend.auth add NAME` (asks for the password), `passwd NAME`, `remove NAME`, `list`.
Passwords are stored only as salted scrypt hashes in `data/users.json`, together with `data/secret.key` (both are outside GitHub).
Five wrong passwords from one address lock it out for a minute. Forgot the password? Run `python -m backend.auth passwd NAME` on the PC.

## Sharing it with someone else

The app runs on your PC, so the PC has to be on. Safest options, best first:

1. **Tailscale, invite the person** (what your phone already uses): in the Tailscale admin page share this PC with their Tailscale account; they open
   `http://<your PC's Tailscale address>:8000` and sign in with a login you create for them. Traffic is encrypted and the app is not on the public internet.
2. **Tailscale Funnel or a Cloudflare Tunnel**: gives a public https address for people without Tailscale. Only do this after the login is on, and give each person
   their own user (`python -m backend.auth add NAME`) so you can remove one later.
3. Do **not** open port 8000 on your router.

The price data comes from Yahoo Finance and the NSE/BSE websites for personal use; check their terms before giving access to many people.
Each person's chart drawings, columns and watchlist live in their own browser.
