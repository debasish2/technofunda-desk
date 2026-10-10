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

## On a Mac (Apple silicon or Intel)

Nothing in the app itself is Windows-specific (Python, SQLite and a browser page); only the start and scheduling scripts were. The Mac versions:

1. **Python 3.11 or newer**: `brew install python@3.13`, or the installer from python.org. Then get the code: `git clone` your private GitHub repository.
2. **Set up**: `bash scripts/setup_mac.sh` (creates `.venv` and installs the packages; add `--full` for the OCR tools of the accuracy programme).
3. **Bring the data across** (the `data/` folder is not on GitHub). On the Windows PC: `.venv\Scripts\python deploy\pack.py --data` makes `deploy\out\data.tar.gz` (about 160 MB:
   all databases including the IndianAPI statements, logins and saved settings). Copy it to the Mac (AirDrop, USB stick or a cloud drive), then in the project folder:
   `mkdir -p data && tar -xzf ~/Downloads/data.tar.gz -C data`. Copy the `.env` file (your IndianAPI key) the same private way; it is not in the archive on purpose.
   Skipping this step also works: `.venv/bin/python -m backend.nightly --weekly` rebuilds the free data (the IndianAPI statements would need a fresh download).
4. **Start**: `./start-desk.command` (or double-click it; the first time, right-click > Open to get past Gatekeeper). It starts the server on port 8000 and opens the site.
5. **Schedule the daily jobs and start the server at login**: `.venv/bin/python scripts/mac_services.py install` (launchd; `status` and `remove` also exist; `--keep-awake` stops idle sleep).

Things that behave differently on a Mac:

* **Sleep.** A sleeping Mac serves nothing. launchd runs a job it missed while asleep as soon as the Mac wakes, but not one missed while switched off. For an always-available app,
  use the Lightsail kit in `deploy/` or `--keep-awake` with the Mac on power (a closed lid still sleeps it unless an external display is attached).
* **Time zone.** The jobs fire at the Mac's local time; set India time in System Settings > General > Date & Time (the app's own market-hours logic is always IST).
* **Phone on the same Wi-Fi**: `http://<the Mac's address>:8000` (the start script prints it). macOS asks once whether Python may accept incoming connections: Allow. Tailscale works as on Windows.
* **Run it on one machine at a time.** Both machines would fetch the same data, and the IndianAPI monthly call budget is shared.
* **GitHub backup**: `bash scripts/backup_github.sh` (scheduled nightly by `mac_services.py install`).

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
