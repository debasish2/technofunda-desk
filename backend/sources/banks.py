"""Bank results PDFs: quarterly profit, net interest income and asset quality (gross / net NPA).

Banks file one table in SEBI's bank format: interest earned, other income, interest expended, operating expenses,
provisions, profit before tax, tax, net profit, then the analytical ratios with gross and net NPA. Columns are the
current quarter, the previous quarter, the same quarter a year ago, and the year; the header carries each column's
date ("30.06.2026"). Many bank PDFs are scans, so pages are OCR'd (bse.ocr_words) when they have no text layer.

Every parsed column must pass arithmetic checks before it is trusted:
    net interest income = interest earned - interest expended
    total income        = interest earned + other income
    profit before tax   = total income - interest expended - operating expenses - provisions - exceptional
    net profit          = profit before tax - tax
A column that fails is dropped, not repaired.
"""
import io
import re
from datetime import date, timedelta

import pdfplumber

from . import bse

NUM = re.compile(r"^[\(\)\d,.\-–%]+$")
DATE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})")
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
DMY_TEXT = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]{3,9})[-\s,.]*(\d{2,4})\b", re.I)


def dates_in(text):
    """Dates written in one line of header text, in order: 30.06.2026, 30-Jun-2026, 30-June-26, 30th June 2026."""
    found = []
    text = re.sub(r"(\d)\s+([./])\s*(\d)", r"\1\2\3", text)          # "31 .03.2026" -> "31.03.2026"
    for m in re.finditer(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})|(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]{3,9})[-\s,.]*(\d{2,4})\b", text, re.I):
        try:
            if m.group(1):
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                mon = next((v for k, v in MONTHS.items() if k.startswith(m.group(5).lower()[:3])), None)
                if mon is None:
                    continue
                d, mo, y = int(m.group(4)), mon, int(m.group(6))
                y += 2000 if y < 100 else 0
            found.append(date(y, mo, d).isoformat())
        except ValueError:
            pass
    return found


def header_dates_by_x(lines, upto):
    """(x centre, iso date) for every date written in the header lines above the table, wherever it sits. Some banks print
    the column dates on two different lines (Kotak: the middle column's date a line above the others), so reading the
    header line by line scrambles the order; reading by position does not."""
    out = []
    for l in lines[max(0, upto - 14):upto]:
        i = 0
        while i < len(l):
            hit = None
            for n in (1, 2, 3):
                seg = l[i:i + n]
                if len(seg) < n:
                    break
                ds = dates_in(" ".join(w["text"] for w in seg))
                # the shortest run of words that is a date: neither its first nor its last word is spare
                if len(ds) == 1 and not (n > 1 and (dates_in(" ".join(w["text"] for w in seg[1:])) or dates_in(" ".join(w["text"] for w in seg[:-1])))):
                    hit = (n, ds[0])
                    break
            if hit:
                seg = l[i:i + hit[0]]
                out.append(((seg[0]["x0"] + seg[-1]["x1"]) / 2, hit[1]))
                i += hit[0]
            else:
                i += 1
    return out


def dates_by_position(lines, upto, groups):
    """One date per number column, from the header dates nearest each column's centre; None when a column has none."""
    cand = header_dates_by_x(lines, upto)
    if not cand:
        return None
    res = []
    for g in groups:
        cx = (min(w["x0"] for w in g) + max(w["x1"] for w in g)) / 2
        x, iso = min(cand, key=lambda c: abs(c[0] - cx))
        if abs(x - cx) > 70:
            return None
        res.append(iso)
    return res


MONTH_DAY = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\b", re.I)

ROWS = {
    "int_earned": [r"^interest (?:ea\w+|income)\b"],   # "interest earned" (private banks) or "interest income" (SBI and others)
    "other_inc": [r"^.?ther income"],
    "tinc": [r"^total income"],
    "int_exp": [r"^interest expended"],
    "opex": [r"^operating expenses"],
    "prov": [r"^provisions"],
    "exc": [r"^exceptional"],
    "pbt": [r"^profit.*before tax", r"^net profit.*before tax"],
    "tax": [r"^tax expense|^provision for tax|^tax$"],
    "np": [r"^net profit.*after (?:taxes?,? )?minority", r"^net profit.*attributable", r"^net profit.*for the (?:period|quarter|year)", r"^net profit.*after tax",
           r"^profit.*after tax"],
    # banks word these rows differently ("Gross NPAs", "Gross non-performing customer assets (net of write-off)") and the
    # ratio rows usually wrap, so the line that carries the numbers may start mid-sentence ("off) to gross customer assets")
    "gnpa": [r"^(?:amount of )?gross (?:non.?perform\w*|npas?)\b(?!.*\bto\b)(?!.*%)"],
    "nnpa": [r"^(?:amount of )?net (?:non.?perform\w*|npas?)\b(?!.*\bto\b)(?!.*%)"],
    "gnpa_pct": [r"^gross npas?\s*\(\s*%\s*\)", r"^gross npas?\s*(?:ratio\s*)?[:\-]?\s*%", r"gross (?:npas?|non.?perform[\w\s-]*) to gross", r"\bto [ag]ross (?:customer assets|advances)",
                 r"^(?:percentage|%) of gross npa", r"^gross npa ratio",
                 r"^(?:(?:percentage|%)\s+)?of\s+gross\s+(?:npas?|non.?perform\w*)"],      # "Percentage of Gross Non Performing Assets", Union's "of Gross NPAs"
    "nnpa_pct": [r"^net npas?\s*\(\s*%\s*\)", r"^net npas?\s*(?:ratio\s*)?[:\-]?\s*%", r"net (?:npas?|non.?perform[\w\s-]*) to net", r"\bto net (?:customer assets|advances)",
                 r"^customer assets\b", r"^(?:percentage|%) of net npa", r"^net npa ratio",
                 r"^(?:(?:percentage|%)\s+)?of\s+net\s+(?:npas?|non.?perform\w*)"],
}


def to_num(tokens):
    t = "".join(tokens).replace("–", "-").replace("%", "").strip()
    if t in ("-", ""):
        return 0.0
    if re.fullmatch(r"\d{3,},\d{2}", t):              # OCR turned the decimal point into a comma: "64016,32"
        t = t.replace(",", ".")
    t = t.replace(",", "")
    neg = "(" in t or t.startswith("-")
    t = re.sub(r"[()\-]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _label(words, left):
    s = " ".join(w["text"] for w in words if w["x0"] < left).strip().lower()
    for _ in range(2):
        s = re.sub(r"^(?:\(?[a-z]{1,2}\)|[a-z]\.|\(?[ivx]+\)|\d+[.)]?)\s+(?=\S)", "", s)
        s = re.sub(r"^[^\w(%]+", "", s)                                   # table border characters: | ! [
    return s


def _strip_enum(t):
    t = re.sub(r"^(?:\(?[a-z]{1,2}\)|\(?[ivx]+\)|\d+[.)]?)\s+", "", t.lower())
    return re.sub(r"^[^\w(%]+", "", t)


def parse_words(words, anchors=("int_earned",)):
    """-> {'basis', 'dates': [iso...], 'found': {key: [values per column]}} or None, from one page's words.
    `anchors` are the row kinds that identify the table (the P&L: interest earned; the NPA table: gross NPA rows)."""
    lines = bse._lines(None, words, tol=4.0)
    texts = [" ".join(w["text"] for w in l) for l in lines]
    pats = [p for k in anchors for p in ROWS[k]]
    anchor = next((l for l, t in zip(lines, texts) if any(re.search(p, _strip_enum(t)) for p in pats)
                   and sum(bool(NUM.match(w["text"])) and w["x0"] > 150 for w in l) >= 2), None)
    if anchor is None:
        return None
    nums = [w for w in anchor if NUM.match(w["text"]) and re.search(r"\d", w["text"]) and w["x0"] > 150]
    groups, cur = [], [nums[0]]
    for w in nums[1:]:
        if w["x0"] - cur[-1]["x1"] < 12:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    groups.append(cur)
    edges = [max(w["x1"] for w in g) for g in groups]
    left = min(w["x0"] for w in groups[0]) - 12

    def cols_of(l):
        vals = [[] for _ in edges]
        for w in l:
            if w["x0"] < left or not NUM.match(w["text"]):
                continue
            i = min(range(len(edges)), key=lambda k: abs(edges[k] - w["x1"]))
            if -45 <= w["x1"] - edges[i] <= (16 if "%" in w["text"] else 8):     # a "2.65%" is centred under its column, so it can overhang the figures above
                vals[i].append(w["text"])
        return [(to_num(v) if v else None) for v in vals]

    # Some layouts (IndusInd's, in OCR) print a row's label on the line above or below its numbers, or split it
    # ("...from Ordinary Activities before" / "Tax (7-8-9) 134324 ..."). So each line with numbers may be read under its
    # own label, under the label-only line above it joined to its own, or under the label-only line below it.
    own = [_label(l, left) for l in lines]
    has_nums = [any(w["x0"] >= left and NUM.match(w["text"]) for w in l) for l in lines]
    cands = []
    for i in range(len(lines)):
        c = [own[i]]
        prev = own[i - 1] if i > 0 and not has_nums[i - 1] else ""
        nxt = own[i + 1] if i + 1 < len(lines) and not has_nums[i + 1] else ""
        if prev:
            c.append((prev + " " + own[i]).strip())
        if nxt:
            c += [(own[i] + " " + nxt).strip(), nxt] if not own[i] else [(own[i] + " " + nxt).strip()]
        cands.append(c)
    found = {}
    for key, pats in ROWS.items():
        for pat in pats:
            hit = None
            for i, l in enumerate(lines):
                if has_nums[i] and any(re.search(pat, lab) for lab in cands[i]):
                    v = cols_of(l)
                    if any(x is not None for x in v):
                        hit = v
                        break
            if hit:
                found[key] = hit
                break
    # the header row(s) above the table carry one date per column
    dates = []
    above = lines[:max(1, lines.index(anchor))]
    head = " ".join(texts[:max(1, lines.index(anchor))])
    for t in texts[:max(1, lines.index(anchor))]:                    # the header line with one date per column
        ds = dates_in(t)
        if len(ds) >= 3:
            dates = ds
            break
    if len(dates) < 2:                                               # "June 30, March 31, ..." with a row of years below
        md = MONTH_DAY.findall(head)
        years = []
        for l in above:
            toks = [w["text"].strip(",") for w in l]
            ys = [t for t in toks if re.fullmatch(r"20\d\d", t)]
            if len(ys) >= max(2, len(md) - 1) and len(ys) >= len([t for t in toks if re.fullmatch(r"[\d,.]+", t)]):
                years = ys
                break
        dates = []
        for (mon, day), y in zip(md, years):
            try:
                dates.append(date(int(y), MONTHS[mon.lower()], int(day)).isoformat())
            except ValueError:
                pass
    if len(groups) >= 3:                                             # dates spread over several header lines: read them by position
        ok = lambda ds: valid_quarter_dates(ds[:3]) and (len(groups) < 8 or valid_quarter_dates(ds[4:7]))
        if not ok(dates):
            pos = dates_by_position(lines, lines.index(anchor), groups)
            if pos and ok(pos):
                dates = pos
    unit_div, unit_known = bse._unit_divisor(" ".join(texts[:max(1, lines.index(anchor))]))
    title = " ".join(texts[:max(1, lines.index(anchor))]).lower()
    # None when OCR garbled the title: parse_pdf then assigns it by order (standalone table first, consolidated second)
    basis = "Consolidated" if "consolidated" in title else "Standalone" if "standalone" in title else None
    return {"basis": basis, "dates": dates[:len(edges)], "found": found, "n": len(edges),
            "unit_div": unit_div, "unit_known": unit_known}


def months_between(later, earlier):
    a, b = date.fromisoformat(later), date.fromisoformat(earlier)
    return (a.year - b.year) * 12 + a.month - b.month


def is_quarter_end(d):
    """A real quarter end is the last day of March, June, September or December."""
    x = date.fromisoformat(d)
    return x.month in (3, 6, 9, 12) and (x + timedelta(days=1)).day == 1


def valid_quarter_dates(dates):
    """The three quarter columns must be: a quarter end that is not in the future, the quarter 3 months before it, and
    the same quarter a year earlier. Any other pattern means a header date was misread or a column shifted, and values
    would land on the wrong date, which no arithmetic check can detect."""
    if len(dates) < 3 or not all(is_quarter_end(d) for d in dates[:3]):
        return False
    if date.fromisoformat(dates[0]) > date.today():
        return False
    return months_between(dates[0], dates[1]) == 3 and months_between(dates[0], dates[2]) == 12


def _close(a, b, rel=0.004, ab=1.5):
    return a is not None and b is not None and abs(a - b) <= max(ab, rel * max(abs(a), abs(b)))


def npa_plausible(gp, np_, g_amt, n_amt):
    """Gross NPA ratio 0-60%, net not above gross, net amount not above gross amount."""
    if gp is not None and not 0 <= gp <= 60:
        return False
    if np_ is not None and not 0 <= np_ <= 40:
        return False
    if gp is not None and np_ is not None and np_ > gp + 0.05:
        return False
    if g_amt is not None and n_amt is not None and n_amt > g_amt * 1.02:
        return False
    return True


def columns(parsed, offset=0):
    """Per-column quarter dicts that passed the arithmetic checks (year-end columns are skipped).
    `offset` = first column of the block (0 for a single table; 4 for the consolidated half of an 8-column table)."""
    f, out = parsed["found"], []
    div = parsed.get("unit_div", 1.0)
    if not parsed.get("unit_known"):
        # unit line unreadable: no Indian bank has a quarterly total income above Rs 4 lakh crore, so a bigger figure
        # means the table is in lakhs (IndusInd reports that way)
        big = max([abs(v) for k in ("tinc", "int_earned") for v in f.get(k, []) if v is not None] or [0])
        div = 100.0 if big > 400_000 else 1.0
    PCT = ("gnpa_pct", "nnpa_pct")
    if not valid_quarter_dates(parsed["dates"][offset:offset + 3]):
        return [{"qend": None, "flags": ["column dates do not form current / previous / year-ago quarters"]}]
    for j, d in enumerate(parsed["dates"][offset:offset + 3]):  # current quarter, previous quarter, year-ago
        i = offset + j
        g = lambda k: (f[k][i] / (1.0 if k in PCT else div)) if k in f and len(f[k]) > i and f[k][i] is not None else None
        ie, oi, ti, ix, ox, pv, ex, pbt, tx, np_ = (g(k) for k in ("int_earned", "other_inc", "tinc", "int_exp", "opex", "prov", "exc", "pbt", "tax", "np"))
        if ix is None and None not in (ti, ox, pv, pbt):
            # the "interest expended" row was not read: it is whatever is left of total income after operating expenses,
            # provisions, exceptional items and profit before tax (checked by the independent net-profit test below)
            ix = ti - ox - pv - (ex or 0) - pbt
        absn = lambda v: None if v is None else abs(v)         # a stray dash before an NPA figure is not a minus sign
        npa = {"gnpa": absn(g("gnpa")), "nnpa": absn(g("nnpa")), "gnpa_pct": absn(g("gnpa_pct")), "nnpa_pct": absn(g("nnpa_pct"))}
        if not npa_plausible(npa["gnpa_pct"], npa["nnpa_pct"], npa["gnpa"], npa["nnpa"]):
            npa = dict.fromkeys(npa)
        if ie is None or ix is None or np_ is None:
            if any(v is not None for v in npa.values()):
                out.append({"qend": d, "flags": ["profit and loss not read"], **npa})   # keeps the asset-quality figures only
            continue
        bad = []
        if ti is not None and oi is not None and not _close(ie + oi, ti):
            bad.append("total income")
        if None not in (ti, ox, pv, pbt) and not _close(ti - ix - ox - (pv or 0) - (ex or 0), pbt, rel=0.01, ab=3):
            bad.append("profit before tax")
        if None not in (pbt, tx) and not _close(pbt - tx, np_, rel=0.01, ab=3):
            # consolidated results deduct minority interest / add associates after tax, so only flag a big gap
            if abs(pbt - tx - np_) > 0.12 * abs(pbt):
                bad.append("net profit")
        if bad:
            out.append({"qend": d, "flags": bad, **npa})
            continue
        out.append({"qend": d, "nii": round(ie - ix, 2), "pbt": pbt if pbt is not None else None, "np": np_, "int_earned": ie,
                    **npa, "flags": []})
    ok = [c for c in out if not c["flags"] and c.get("nii")]
    if len(ok) >= 2:
        med = sorted(c["nii"] for c in ok)[len(ok) // 2]
        for c in ok:
            if not 0.45 <= c["nii"] / med <= 2.2:     # a bank's quarterly NII does not move 2x between adjacent quarters
                c["flags"].append("size does not fit the neighbouring quarters (column shift?)")
    return out


def _read_page(page, log, pn):
    """The page's words and parse. The text layer is tried first; if it is missing, or garbled enough that no valid
    results table comes out of it, the page is OCR'd instead (SBI's PDFs, for one, carry a text layer full of errors)."""
    text_words = page.extract_words(x_tolerance=1.5)
    attempts = []
    if len(text_words) >= 40:
        attempts.append(("text", lambda: text_words))
    smells_right = len(text_words) < 40 or re.search(r"(?i)financ\w*\s+res\w*|profit|npa|interest", " ".join(w["text"] for w in text_words))
    if smells_right:
        attempts.append(("ocr", lambda: bse.ocr_words(page)))
    best, best_score = (text_words, None, "none"), -1
    for kind, get in attempts:
        try:
            words = get()
        except Exception as e:
            log(f"  page {pn}: {kind} failed ({type(e).__name__})")
            continue
        p = parse_words(words)
        pl_cols = [c for c in columns(p) if "np" in c and not c["flags"]] if p and "int_earned" in p["found"] and len(p["dates"]) >= 2 else []
        q = None if pl_cols else parse_words(words, anchors=("gnpa", "gnpa_pct", "nnpa", "nnpa_pct"))
        has_npa = bool(q and any(k in q["found"] for k in ("gnpa", "gnpa_pct")))
        n = p["n"] if pl_cols else q["n"] if has_npa else 0
        score = (10 if pl_cols else 5 if has_npa else 0) + n
        if score >= best_score:                      # on a tie the later reader (OCR) wins: the text layer was the one that failed to give a full table
            best, best_score = (words, p, kind), score
        # a table with all its columns: no need to try the next reader. A page that also carries a profit and loss table the
        # text layer could not read (garbled scan) goes on to OCR even when its asset-quality rows came out fine.
        garbled_pl = not pl_cols and re.search(r"(?i)interest\s+(?:earned|income)|net\s+profit", " ".join(w["text"] for w in words))
        if (pl_cols or (has_npa and not garbled_pl)) and n >= 3:
            break
    return best


def parse_pdf(pdf_bytes, max_pages=16, log=lambda *a: None):
    """-> {'Standalone': [columns], 'Consolidated': [columns]} from a bank results PDF; NPA tables are attached to the
    standalone columns. Pages are read from the text layer, falling back to OCR."""
    res, tables, npa_tables = {"Standalone": [], "Consolidated": []}, [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn, page in enumerate(pdf.pages[:max_pages], 1):
            words, p, how = _read_page(page, log, pn)
            if p is None or "int_earned" not in p["found"]:
                q = parse_words(words, anchors=("gnpa", "gnpa_pct", "nnpa", "nnpa_pct"))
                if q and any(k in q["found"] for k in ("gnpa", "gnpa_pct")):
                    npa_tables.append({"n": q["n"], "dates": q["dates"], "found": q["found"], "page": pn, "unit_div": q.get("unit_div", 1.0)})
                    log(f"  page {pn}: asset-quality table ({how}), {q['n']} columns")
            if p and "int_earned" in p["found"] and len(p["dates"]) >= 2:
                blocks = [(p["basis"], 0)]
                if p["n"] >= 8 and len(p["dates"]) >= 6:             # standalone and consolidated side by side
                    blocks = [("Standalone", 0), ("Consolidated", 4)]
                for basis, off in blocks:
                    cols = columns(p, off)
                    if any(t["cols"] and cols and t["cols"][0].get("int_earned") == cols[0].get("int_earned") for t in tables):
                        continue     # the same table repeated elsewhere in the bundle
                    tables.append({"basis": basis, "cols": cols, "page": pn})
                    log(f"  page {pn}: table labelled {basis} ({how}), columns ok: {[c['qend'] for c in cols if not c['flags']]}")
            if len(tables) >= 2 and npa_tables:
                break
    # explicit labels win; unlabelled tables are assigned by order, standalone first then consolidated
    for t in tables:
        if t["basis"] and not res[t["basis"]]:
            res[t["basis"]] = t["cols"]
    for t in tables:
        if not t["basis"]:
            free = next((b for b in ("Standalone", "Consolidated") if not res[b]), None)
            if free:
                res[free] = t["cols"]
    # banks report NPAs standalone: attach each NPA column to the standalone P&L column with the same date, or, when the
    # NPA table has no dates of its own, to the same position (the two tables share one column order)
    for nt in npa_tables:
        if nt["dates"] and not valid_quarter_dates(nt["dates"][:3]):
            continue
        for i in range(min(3, nt["n"])):
            for col in res["Standalone"]:
                d = nt["dates"][i] if i < len(nt["dates"]) else None
                pos = res["Standalone"].index(col)
                if (d and col["qend"] == d) or (not d and pos == i):
                    for k in ("gnpa", "nnpa", "gnpa_pct", "nnpa_pct"):
                        v = nt["found"].get(k, [None] * 4)[i]
                        if v is not None and col.get(k) is None:
                            col[k] = abs(v) / (1.0 if k.endswith("_pct") else nt.get("unit_div", 1.0))
    # asset quality read from tables that carry no P&L (or whose P&L did not parse), by their own dates
    npa = []
    for nt in npa_tables:
        ds = nt["dates"]
        usable = valid_quarter_dates(ds[:3]) if len(ds) >= 3 else bool(ds) and all(
            is_quarter_end(d) and date.fromisoformat(d) <= date.today() for d in ds)
        if not usable:
            continue
        for i, d in enumerate(ds[:min(3, nt["n"])]):
            g = lambda k: abs(nt["found"][k][i]) / (1.0 if k.endswith("_pct") else nt.get("unit_div", 1.0)) \
                if nt["found"].get(k) and len(nt["found"][k]) > i and nt["found"][k][i] is not None else None
            c = {"qend": d, "gnpa": g("gnpa"), "nnpa": g("nnpa"), "gnpa_pct": g("gnpa_pct"), "nnpa_pct": g("nnpa_pct")}
            if (c["gnpa_pct"] is not None or c["nnpa_pct"] is not None) and npa_plausible(c["gnpa_pct"], c["nnpa_pct"], c["gnpa"], c["nnpa"]):
                npa.append(c)
    res["NPA"] = npa
    return res
