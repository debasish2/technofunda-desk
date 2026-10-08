"""Which quarterly figures on the site have been checked against a company's own filing.

    f  from a filing: NSE XBRL or a results PDF that passed its arithmetic checks
    m  Yahoo's figure, and the company's filing says the same (backend/verify.py compared them)
    u  Yahoo's figure that no filing has confirmed yet: shown with a mark on the Desk and the Screener
"""
from . import verify

FILING = {"NSE XBRL", "BSE PDF", "BSE PDF (OCR)", "NSE PDF", "NSE PDF (OCR)", "BSE PDF (checked)", "Bank PDF"}


_cache = {"key": None, "set": set()}


def _agreed(con):
    """{(sym, qend)} the filing and Yahoo agree on."""
    try:
        key = tuple(con.execute("SELECT COUNT(*), MAX(checked) FROM verify_q").fetchone())
        if key == _cache["key"]:
            return _cache["set"]
        rows = [dict(r) for r in con.execute("SELECT * FROM verify_q WHERE comparable=1")]
    except Exception:
        return set()
    _cache.update(key=key, set={(r["sym"], r["qend"]) for r in rows if verify.classify(r) == "agree"})
    return _cache["set"]


def tag(source, agreed):
    return "f" if source in FILING else ("m" if agreed else "u")


def for_stock(con, sym, rows, agreed=None):
    """{qend: tag} for the quarter rows (dicts with qend, source) of one stock."""
    agreed = _agreed(con) if agreed is None else agreed
    return {r["qend"]: tag(r["source"], (sym, r["qend"]) in agreed) for r in rows}


def newest(con):
    """{sym: (qend, tag)} for every stock's newest usable quarter."""
    agreed = _agreed(con)
    out = {}
    for r in con.execute("SELECT sym, qend, source FROM quarter WHERE flags='' AND sales IS NOT NULL AND op IS NOT NULL AND np IS NOT NULL "
                         "ORDER BY sym, qend"):
        out[r["sym"]] = (r["qend"], tag(r["source"], (r["sym"], r["qend"]) in agreed))
    return out
