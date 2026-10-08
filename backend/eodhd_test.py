"""Does EODHD cover Indian stocks well enough to replace the free-data patchwork?

    python -m backend.eodhd_test --free       # free key: is NSE/BSE listed, do prices work (no fundamentals on this plan)
    python -m backend.eodhd_test              # paid Equity Analyst key: fundamentals for the test stocks, checked against filings
    python -m backend.eodhd_test --selftest   # offline: exercise the parsing and scoring on made-up data, no key needed

The key is read from the EODHD_API_KEY environment variable, or from a line  EODHD_API_KEY=...  in a file named
.env in the project folder. It is never printed or written to the report.

Scoring uses quarters I verified by hand against the companies' own result filings (TRUTH below), so a match
means EODHD agrees with the primary source, not merely with Yahoo. Amounts are converted from rupees to crore.
"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://eodhd.com/api"
CR = 1e7

# stocks chosen to include the ones the free sources could NOT read (Reliance, Infosys, HUL, ITC, ...)
TEST = ["STLTECH", "TCS", "SONACOMS", "TEJASNET", "BHARATFORG", "HFCL", "DIXON", "APOLLOHOSP", "BHARTIARTL",
        "RELIANCE", "INFY", "HINDUNILVR", "ITC", "HDFCBANK"]

# (sales Rs crore, net profit Rs crore) for the quarter ending on the date, read from the company's filing
TRUTH = {
    "STLTECH": {"2026-06-30": (1910, 197), "2026-03-31": (1441, 59), "2025-12-31": (1257, -17), "2025-09-30": (1034, 4)},
    "TCS": {"2026-06-30": (72275, 13349)},                  # profit attributable to owners
    "SONACOMS": {"2026-03-31": (1257.5, 191.9)},
    "TEJASNET": {"2025-09-30": (261.8, -307.1), "2025-06-30": (202.0, -193.9)},
    "BHARATFORG": {"2025-09-30": (4031.9, 299.3)},
}
RECENT = "2026-03-31"          # "current" means a quarter at or after this date


def api_key():
    k = os.environ.get("EODHD_API_KEY", "").strip()
    env = ROOT / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("EODHD_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    return k


def get(path, key, **params):
    r = requests.get(f"{BASE}/{path}", params={**params, "api_token": key, "fmt": "json"}, timeout=60,
                     headers={"User-Agent": "setup-desk-test"})
    return r


def quarterly(payload):
    """EODHD nests the statement under Financials::Income_Statement::quarterly, or returns that subtree when
    filtered. Returns {date: row} either way."""
    if not isinstance(payload, dict):
        return {}
    node = payload
    for k in ("Financials", "Income_Statement", "quarterly"):
        if isinstance(node, dict) and k in node:
            node = node[k]
    return {d: v for d, v in node.items() if isinstance(v, dict) and len(d) == 10 and d[4] == "-"} if isinstance(node, dict) else {}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score(sym, q):
    """Compare EODHD's quarters with the filings. Returns a dict for the report."""
    dates = sorted(q)
    out = {"quarters": len(dates), "oldest": dates[0] if dates else None, "newest": dates[-1] if dates else None,
           "current": bool(dates and dates[-1] >= RECENT), "checked": [], "fields": sorted({k for r in q.values() for k, v in r.items() if num(v) is not None})[:40]}
    gaps = []
    for a, b in zip(dates, dates[1:]):                     # consecutive quarter ends are ~91 days apart
        days = (__import__("datetime").date.fromisoformat(b) - __import__("datetime").date.fromisoformat(a)).days
        if days > 100:
            gaps.append(f"{a}->{b}")
    out["gaps"] = gaps
    for d, (s_true, np_true) in TRUTH.get(sym, {}).items():
        row = q.get(d)
        if not row:
            out["checked"].append({"quarter": d, "found": False})
            continue
        s, n = num(row.get("totalRevenue")), num(row.get("netIncome"))
        s, n = (s / CR if s is not None else None), (n / CR if n is not None else None)
        ok_s = s is not None and abs(s - s_true) <= 0.01 * abs(s_true)
        ok_n = n is not None and abs(n - np_true) <= max(0.03 * abs(np_true), 1.0)
        out["checked"].append({"quarter": d, "found": True, "sales": s and round(s, 1), "sales_filing": s_true, "sales_ok": ok_s,
                               "profit": n and round(n, 1), "profit_filing": np_true, "profit_ok": ok_n})
    return out


def run_free(key):
    print("FREE-PLAN CHECK (no fundamentals on this plan)\n")
    u = get("user", key)
    print("account:", u.status_code, (u.text[:200] if not u.ok else json.dumps({k: v for k, v in u.json().items() if k in ("subscriptionType", "apiRequests", "dailyRateLimit")})))
    ex = get("exchanges-list/", key)
    if not ex.ok:
        print("exchange list refused:", ex.status_code, ex.text[:120])
    else:
        india = [e for e in ex.json() if e.get("Code") in ("NSE", "BSE") or "india" in str(e.get("Country", "")).lower()]
        print("India exchanges listed:", json.dumps(india)[:400] or "NONE")
    for t in ("TCS.NSE", "RELIANCE.NSE"):
        p = get(f"eod/{t}", key, period="d", order="d", **{"from": "2026-09-25"})
        print(f"end-of-day price {t}:", p.status_code, (p.json()[:1] if p.ok else p.text[:100]))
    f = get("fundamentals/TCS.NSE", key, filter="General::Name")
    print("fundamentals TCS.NSE:", f.status_code, f.text[:140].replace("\n", " "), "(expected to be refused on the free plan)")


def run_paid(key):
    report, used = {}, 0
    for sym in TEST:
        r = get(f"fundamentals/{sym}.NSE", key, filter="Financials::Income_Statement::quarterly")
        used += 10
        if r.status_code in (401, 402, 403):
            print(f"{sym}: refused ({r.status_code}) - the plan probably does not include fundamentals"); report[sym] = {"error": r.status_code}
            if sym == TEST[0]:
                break
            continue
        try:
            q = quarterly(r.json())
        except ValueError:
            q = {}
        if not q:
            print(f"{sym}: no quarterly statements returned"); report[sym] = {"quarters": 0}
            continue
        report[sym] = sc = score(sym, q)
        flag = "current" if sc["current"] else "STALE"
        chk = "".join("." if (c.get("sales_ok") and c.get("profit_ok")) else "X" for c in sc["checked"]) or "-"
        print(f"{sym:<11} {sc['quarters']:>3} quarters {sc['oldest']}..{sc['newest']} [{flag}] gaps:{len(sc['gaps'])} vs filings:{chk}")
    have = [s for s, v in report.items() if v.get("quarters")]
    cur = [s for s in have if report[s]["current"]]
    checked = [c for s in have for c in report[s]["checked"] if c.get("found")]
    good = [c for c in checked if c["sales_ok"] and c["profit_ok"]]
    print(f"\nSUMMARY: {len(have)}/{len(TEST)} stocks returned quarters; {len(cur)} have a quarter since {RECENT}; "
          f"{len(good)}/{len(checked)} checked quarters match the filings; about {used} API calls used")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "eodhd_test.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


def selftest():
    fake = {"Financials": {"Income_Statement": {"quarterly": {
        "2026-06-30": {"date": "2026-06-30", "totalRevenue": "19100000000.00", "netIncome": "1970000000.00", "ebitda": "3970000000"},
        "2026-03-31": {"date": "2026-03-31", "totalRevenue": "14410000000.00", "netIncome": "590000000.00"},
        "2025-12-31": {"date": "2025-12-31", "totalRevenue": "12570000000.00", "netIncome": "-170000000.00"},
        "2025-06-30": {"date": "2025-06-30", "totalRevenue": "10190000000.00", "netIncome": "100000000.00"}}}}}
    for payload in (fake, fake["Financials"]["Income_Statement"]["quarterly"]):            # unfiltered and filtered shapes
        sc = score("STLTECH", quarterly(payload))
        assert sc["quarters"] == 4 and sc["newest"] == "2026-06-30" and sc["current"], sc
        assert sc["gaps"] == ["2025-06-30->2025-12-31"], f"should flag only the missing Sep 2025 quarter, got {sc['gaps']}"
        ok = {c["quarter"]: c for c in sc["checked"]}
        assert ok["2026-06-30"]["sales_ok"] and ok["2026-06-30"]["profit_ok"]
        assert ok["2025-12-31"]["sales_ok"] and ok["2025-12-31"]["profit_ok"]
        assert not ok["2025-09-30"]["found"]
    wrong = {"2026-06-30": {"totalRevenue": "21000000000", "netIncome": "1970000000"}}
    assert not score("STLTECH", wrong)["checked"][0]["sales_ok"], "a 10% revenue error must fail"
    print("selftest passed: parsing, gap detection and scoring behave as intended")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    k = api_key()
    if not k:
        sys.exit("No key found. Set the EODHD_API_KEY environment variable, or add a line EODHD_API_KEY=... to a file named .env in the project folder.")
    (run_free if "--free" in sys.argv else run_paid)(k)
