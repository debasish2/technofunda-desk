"""SQLite storage. Every quarterly number keeps the filing it came from."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "setupdesk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS screen (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT DEFAULT 'me', name TEXT, filters TEXT, min_value_cr REAL, created TEXT
);
CREATE TABLE IF NOT EXISTS screen_hit (
  screen_id INTEGER, d TEXT, sym TEXT, PRIMARY KEY (screen_id, d, sym)
);
CREATE TABLE IF NOT EXISTS stock (
  sym TEXT PRIMARY KEY, name TEXT, sector TEXT, isin TEXT,
  shares_cr REAL, cap_employed REAL, equity REAL, updated TEXT
);
CREATE TABLE IF NOT EXISTS bar (
  sym TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v INTEGER,
  PRIMARY KEY (sym, d)
);
CREATE TABLE IF NOT EXISTS insider (
  sym TEXT, did TEXT, d TEXT, who TEXT, category TEXT, txn TEXT, mode TEXT, kind TEXT, qty REAL, val_cr REAL, after_pct REAL,
  PRIMARY KEY (sym, did)
);
CREATE TABLE IF NOT EXISTS holding (sym TEXT, qend TEXT, promoter_pct REAL, public_pct REAL, PRIMARY KEY (sym, qend));
CREATE TABLE IF NOT EXISTS pledge (
  sym TEXT, asof TEXT, promoter_pct REAL, pledged_shares REAL, promoter_shares REAL, pledged_pct_of_promoter REAL,
  pledged_pct_of_total REAL, PRIMARY KEY (sym, asof)
);
CREATE TABLE IF NOT EXISTS disc_fetch (sym TEXT PRIMARY KEY, fetched TEXT);
CREATE TABLE IF NOT EXISTS npa (
  sym TEXT, qend TEXT, gnpa REAL, nnpa REAL, gnpa_pct REAL, nnpa_pct REAL, basis TEXT, source TEXT, ref TEXT,
  PRIMARY KEY (sym, qend)
);
CREATE TABLE IF NOT EXISTS quarter (
  sym TEXT, qend TEXT, sales REAL, op REAL, np REAL, eps REAL,
  basis TEXT, source TEXT, filed TEXT, ref TEXT, flags TEXT DEFAULT '',
  PRIMARY KEY (sym, qend)
);
CREATE TABLE IF NOT EXISTS ia_quarter (sym TEXT, qend TEXT, sales REAL, op REAL, np REAL, np_raw REAL, PRIMARY KEY (sym, qend));
CREATE TABLE IF NOT EXISTS ia_fetch (sym TEXT PRIMARY KEY, name TEXT, fetched TEXT, status TEXT, detail TEXT, np_scale REAL, basis TEXT);
CREATE TABLE IF NOT EXISTS ia_usage (month TEXT PRIMARY KEY, calls INTEGER);
CREATE TABLE IF NOT EXISTS ia_annual (sym TEXT, label TEXT, sales REAL, op REAL, np REAL, PRIMARY KEY (sym, label));
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)      # the nightly job and the web server share this file
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(quarter)")]
    if "flags" not in cols:
        con.execute("ALTER TABLE quarter ADD COLUMN flags TEXT DEFAULT ''")
    if "scrip" not in [r[1] for r in con.execute("PRAGMA table_info(stock)")]:
        con.execute("ALTER TABLE stock ADD COLUMN scrip TEXT")
    if "kind" not in [r[1] for r in con.execute("PRAGMA table_info(stock)")]:
        con.execute("ALTER TABLE stock ADD COLUMN kind TEXT DEFAULT 'corp'")   # 'fin' = bank / lender / insurer
    if "debt" not in [r[1] for r in con.execute("PRAGMA table_info(stock)")]:
        con.execute("ALTER TABLE stock ADD COLUMN debt REAL")                 # total debt in rupee crore (Yahoo's definition, leases included)
    if "np_annual" not in [r[1] for r in con.execute("PRAGMA table_info(stock)")]:
        con.execute("ALTER TABLE stock ADD COLUMN np_annual TEXT")           # net profit of the last four financial years, newest first, rupee crore, comma-separated
    if "query" not in [r[1] for r in con.execute("PRAGMA table_info(ia_fetch)")]:
        con.execute("ALTER TABLE ia_fetch ADD COLUMN query TEXT")             # the search text that found the company, so later calls need no guessing
    if "desk" not in [r[1] for r in con.execute("PRAGMA table_info(stock)")]:
        con.execute("ALTER TABLE stock ADD COLUMN desk INTEGER DEFAULT 1")   # 1 = charted on the Desk, 0 = screener only
    return con
