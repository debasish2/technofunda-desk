"""Trial of IndianAPI (stock.indianapi.in): fetch a few stocks and save the raw replies under data/indianapi_raw/ for comparison with our filing-verified numbers.

Key lives in .env (INDIANAPI_KEY=...), which git ignores. Usage: python scripts/indianapi_probe.py [NAME ...]
Each stock costs a few calls; the free plan's allowance is small, so the default list is short."""
import json, os, sys, time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "indianapi_raw"
BASE = "https://stock.indianapi.in"
DEFAULT = ["Aegis Logistics", "Deep Industries", "PNB", "PVR Inox", "Reliance"]


def key():
    k = os.environ.get("INDIANAPI_KEY")
    env = ROOT / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("INDIANAPI_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        sys.exit("INDIANAPI_KEY is empty: paste the key after the = in .env")
    return k


def get(path, params, k):
    r = requests.get(BASE + path, params=params, headers={"x-api-key": k}, timeout=60)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500])


def main():
    k = key()
    OUT.mkdir(parents=True, exist_ok=True)
    for name in sys.argv[1:] or DEFAULT:
        slug = name.replace(" ", "_")
        for tag, path, params in [
            ("stock", "/stock", {"name": name}),
            ("quarters", "/historical_stats", {"stock_name": name, "stats": "quarter_results"}),
            ("yearly", "/historical_stats", {"stock_name": name, "stats": "yoy_results"}),
        ]:
            code, body = get(path, params, k)
            (OUT / f"{slug}.{tag}.json").write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"{name:20} {tag:9} HTTP {code}  {len(json.dumps(body))} bytes")
            if code == 429:
                sys.exit("rate / credit limit reached")
            time.sleep(1)


if __name__ == "__main__":
    main()
