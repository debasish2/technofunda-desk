"""A more forgiving reader for the results tables in BSE filings, built for the big companies' PDFs.

bse.py reads one clean layout well. Large-company filings differ: the PDF is often a scan with a noisy text layer ("opernlions",
"depredation"), the revenue line is split into sub-rows with no total, a long label wraps so the numbers sit on the next line, and
a filing carries a dozen pages that mention the results. This module keeps bse.py's arithmetic checks (it hands its table to
bse._build and bse._check_date) but changes how the table is found and read:

  * every page that looks like a results table is read, and the cleanest one wins (consolidated first among equals);
  * row labels are matched fuzzily, so a misspelt label still lands on the right line;
  * a label whose numbers wrap onto the next line is joined back together;
  * revenue is the sum of its sub-rows when no total line exists;
  * OCR is only run on the few pages most likely to hold the table.

It does not touch what the site stores; backend/verify.py is its only user so far.
"""
import difflib
import io
import re
from datetime import date, timedelta

import pdfplumber

from . import bse

CONCEPTS = {
    "sales": ["total revenue from operations", "revenue from operations", "net sales", "income from operations", "revenue from operation"],
    "other": ["other income"],
    "tinc": ["total income"],
    "texp": ["total expenses", "total expenditure"],
    "assoc": ["share of profit of associates and joint ventures", "share of profit of associate", "share of net profit of associates"],
    "fin": ["finance costs", "finance cost", "interest expense", "interest and finance charges", "borrowing costs"],
    "dep": ["depreciation and amortisation expense", "depreciation amortisation and impairment", "depreciation and amortization", "depreciation"],
    "pbe": ["profit before exceptional items and tax", "profit before share of profit of associates exceptional items and tax",
            "profit loss before exceptional items and tax", "profit before exceptional item and tax", "profit before tax and exceptional items",
            "profit before exceptional items share of profit of associates joint ventures and tax",
            "profit loss before exceptional items share of profit loss of associates joint ventures and tax"],
    "exc": ["exceptional items", "exceptional item"],
    "pbt": ["profit before tax", "profit loss before tax"],
    "tax": ["total tax expense", "tax expense", "total tax", "income tax expense"],
    "np": ["profit after tax", "profit loss after tax", "profit for the period", "profit for the quarter", "net profit for the period", "profit after tax", "profit loss for the period",
           "profit for the quarter year", "profit for the period year", "net profit loss for the period", "profit for the year"],
    "owners": ["owners of the company", "owners of the parent", "equity holders of the parent", "owners of the group"],
    "eps": ["basic"],
}
THRESH = 0.80
UNIT2 = re.compile(r"(?:\b\w?in\b|₹|~|\brs\.?|\binr)\s*(millions?|mn|lakhs?|lacs?|crores?|cr)\b", re.I)


def norm(label):
    t = re.sub(r"[^a-z ]+", " ", label.lower())
    t = re.sub(r"\s+", " ", t).strip()
    for _ in range(2):                                   # leading enumerators: "i", "iv", "a", "x"
        t = re.sub(r"^(?:[ivx]{1,5}|[a-z])\s+(?=\S)", "", t) if re.match(r"^(?:[ivx]{1,5}|[a-z])\s+\w{3,}", t) else t
    return t


def _has_word(tokens, word):
    return any(difflib.SequenceMatcher(None, tk, word).ratio() >= 0.8 for tk in tokens)


def concept_of(label):
    """-> (concept, score) for the best fuzzy match of a row label, or (None, 0)."""
    t = norm(label)
    if len(t) < 4:
        return None, 0
    if re.search(r"(?:owners|equity holders|shareholders) of the (?:parent|company|group|holding)", t) or re.match(r"^owners of", t):
        return "owners", 1.0
    toks = t.split()[:14]
    l_before, l_after = _has_word(toks, "before"), _has_word(toks, "after")
    best, score = None, 0.0
    for key, phrases in CONCEPTS.items():
        for p in phrases:
            w = t[:len(p) + 4]
            if w[:3] != p[:3] and difflib.SequenceMatcher(None, w[:6], p[:6]).ratio() < 0.6:
                continue
            r = difflib.SequenceMatcher(None, w, p).ratio()
            if (l_before and "before" not in p) or (l_after and "after" not in p and key == "pbt") or ("before" in p and not l_before):
                r *= 0.8                                     # "after tax" is not "before tax"
            if r > score:
                best, score = key, r
    return (best, score) if score >= THRESH else (None, score)


def _unit(*texts):
    for t in texts:
        m = bse.UNIT_RE.search(t or "") or UNIT2.search(t or "")
        if m:
            return bse.UNIT_DIV[m.group(1).lower()], True
    for t in texts:                                          # garbled prefix: "(lNR crore)", "(t in Crores)"
        m = re.search(r"\b(crores?|millions?|lakhs?|lacs?)\b", (t or "")[:1500], re.I)
        if m:
            return bse.UNIT_DIV[m.group(1).lower()], True
    return 1.0, False


def _qend(head, lines):
    m = re.search(r"ENDED\s+(?:ON\s+)?(?:\d{1,2}\s*(?:ST|ND|RD|TH)?[\s,]+)?([A-Z]+),?\s*(?:[\dIlO]{1,2},?\s*)?(\d{4})", head)
    if m and m.group(1) in bse.MONTHS:
        mon, yr = bse.MONTHS[m.group(1)], int(m.group(2))
    else:
        # no readable "quarter ended <date>" title: the dates printed above the columns say it
        hd = header_dates(lines)
        if hd:
            return hd[0]
        hdr = " ".join(w["text"] for l in lines[:40] for w in l).upper()
        md = re.search(r"\b(" + "|".join(bse.MONTHS) + r")\s+\d{1,2},?", hdr)
        my = re.search(r"\b(20\d\d)\b", hdr[md.end():]) if md else None
        if not md or not my:
            m2 = re.search(r"\b(\d{1,2})[-./](\d{1,2})[-./](20\d\d)\b", hdr)          # 30-06-2026
            if not m2:
                return None
            mon, yr = int(m2.group(2)), int(m2.group(3))
        else:
            mon, yr = bse.MONTHS[md.group(1)], int(my.group(1))
    if not 1 <= mon <= 12:
        return None
    return (date(yr + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)).isoformat()


def parse_page(page, text, words=None):
    """-> the same dict bse.parse_page returns ('basis', 'qend', 'found', 'n', 'div', 'unit_known'), or None."""
    lines = bse._lines(page, words, tol=2.5 if words is None else 4.0)
    head = text[:1500].upper() if words is None else " ".join(w["text"] for l in lines[:25] for w in l).upper()
    qend = _qend(head, lines)
    if not qend:
        return None
    isnum = lambda w: bse.NUMTOK.match(w["text"]) and w["x0"] > 150

    def figures(l):
        out = []
        for i, w in enumerate(l):
            if isnum(w) and not (i and re.search(r"^(?:notes?|refer|ref\.?|no\.?)$", l[i - 1]["text"].strip("()."), re.I)):
                out.append(w)
        return out

    def premerge(l):
        out = []
        for w in l:
            if (out and isnum(w) and isnum(out[-1]) and re.fullmatch(r"\d{1,2}", out[-1]["text"])
                    and re.match(r"[\d,]", w["text"]) and w["x0"] - out[-1]["x1"] < 18):
                a = out.pop()
                w = {**w, "text": a["text"] + w["text"], "x0": a["x0"]}
            out.append(w)
        return out
    lines = [premerge(l) for l in lines]

    rows = []                                                    # [label, numeric words]
    for l in lines:
        nums = figures(l)
        lab = bse._label([w for w in l if not bse.NUMTOK.match(w["text"]) or w["x0"] <= 150])
        if len(nums) < 2:                                       # one stray number ("(refer note 4)") is not a row of figures
            nums = []
        rows.append([lab, nums, l])
    merged = []
    for lab, nums, l in rows:                                    # a label whose numbers wrap onto the next line
        if merged and not merged[-1][1] and nums and len(re.sub(r"[^a-z]", "", lab)) <= 3 and len(merged[-1][0]) > 12:
            merged[-1][1] = nums
            merged[-1][2] = merged[-1][2] + l
        else:
            merged.append([lab, nums, l])

    keyrows = []
    for lab, nums, l in merged:
        c, sc = concept_of(lab)
        if c in ("sales", "tinc", "texp", "pbt", "np", "tax", "pbe", "other", "fin", "dep") and len(nums) >= 2:
            keyrows.append((len(nums), c, nums))
    if not keyrows:
        return None
    pref = [k for k in keyrows if k[1] in ("sales", "tinc", "texp")] or keyrows
    nums = max(pref, key=lambda k: k[0])[2]
    groups, cur = [], [nums[0]]
    for w in nums[1:]:
        if w["x0"] - cur[-1]["x1"] < 12:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    groups.append(cur)
    if len(groups) >= 3 and len(groups[0]) == 1 and re.fullmatch(r"\d{1,2}", groups[0][0]["text"]) and groups[1][0]["x0"] - groups[0][0]["x1"] > 10:
        groups = groups[1:]
    edges = [max(w["x1"] for w in g) for g in groups]
    left = min(w["x0"] for w in groups[0]) - 12

    def cols_of(words_):
        vals = [[] for _ in edges]
        for w in words_:
            if w["x0"] < left or not bse.NUMTOK.match(w["text"]):
                continue
            i = min(range(len(edges)), key=lambda k: abs(edges[k] - w["x1"]))
            if -45 <= w["x1"] - edges[i] <= 8:
                vals[i].append(w["text"])
        return [(None if not v else 0.0 if all(re.fullmatch(r"[-–—]+", t) for t in v) else bse._num(v)) for v in vals]      # a lone dash is a nil entry, not a missing one

    found, assigned, owners = {}, [], []
    for idx, (lab, nums_, l) in enumerate(merged):
        c, sc = concept_of(lab)
        vals = cols_of(nums_) if nums_ else None
        assigned.append((c, vals))
        if c == "owners" and vals and any(v is not None for v in vals):
            owners.append(vals)                              # profit, other comprehensive income and total comprehensive income each have an "owners" line
        elif c and vals and any(v is not None for v in vals) and c not in found:
            found[c] = vals
    if owners:
        o = _pick_owners(owners, found)
        if o is not None:
            found["owners"] = o
    # revenue split into sub-rows with no total line: add them up until other income / total income
    if "sales" not in found:
        for idx, (lab, nums_, l) in enumerate(merged):
            c, sc = concept_of(lab)
            if c == "sales" and not nums_:
                acc, n = None, 0
                for lab2, nums2, l2 in merged[idx + 1: idx + 8]:
                    c2, _ = concept_of(lab2)
                    if c2 in ("other", "tinc") or re.match(r"^(?:ii|2)\b", lab2.lower()):
                        break
                    v = cols_of(nums2) if nums2 else None
                    if v and any(x is not None for x in v):
                        acc = v if acc is None else [(a or 0) + (b or 0) if (a is not None or b is not None) else None for a, b in zip(acc, v)]
                        n += 1
                if acc and n:
                    found["sales"] = acc
                break
    repair(found, len(edges))
    dates = header_dates(lines)
    div, known = _unit(text[:3000], head)
    consolidated = "CONSOLIDATED" in head and not re.search(r"STANDALONE", head[:head.find("CONSOLIDATED")])
    return {"basis": "Consolidated" if consolidated else "Standalone", "qend": qend, "found": found, "n": len(edges), "div": div,
            "unit_known": known, "dates": dates}


MON3 = {m[:3]: i for m, i in bse.MONTHS.items()}
DATE_RE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})|(\d{1,2})(?:st|nd|rd|th)?[\s,.-]*([A-Za-z]{3,9})[\s,.-]*(\d{4})|([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{4})")


def _qe(y, m):
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()


def header_dates(lines):
    """The quarter-end dates printed above the columns, left to right ('30.06.2026  31.03.2026  30.06.2025'), when one header row carries three or more.
    Filings order their columns differently (current, preceding, year ago - or current, year ago, preceding), so the order is read, not assumed."""
    best = []
    for l in lines[:45]:
        ds = []
        for m in DATE_RE.finditer(" ".join(w["text"] for w in l)):
            if m.group(1):
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif m.group(4):
                d, mo, y = int(m.group(4)), MON3.get(m.group(5)[:3].upper()), int(m.group(6))
            else:
                d, mo, y = int(m.group(8)), MON3.get(m.group(7)[:3].upper()), int(m.group(9))
            if mo and mo % 3 == 0 and d >= 28 and 2015 <= y <= 2035:
                ds.append(_qe(y, mo))
        if len(ds) >= 3 and len(ds) > len(best):
            best = ds
    return best


def _pick_owners(cands, found):
    """The 'owners of the company' line that belongs to the profit: the one closest to the period's profit (the others are comprehensive income)."""
    ref = found.get("np")
    if ref is None and found.get("pbt") and found.get("tax"):
        ref = [None if a is None or b is None else a - b for a, b in zip(found["pbt"], found["tax"])]
    if ref is None:
        return cands[0] if len(cands) == 1 else None

    def miss(v):
        d = [abs(abs(a) - abs(b)) / (abs(b) + 1) for a, b in zip(v, ref) if a is not None and b is not None]
        return sum(d) / len(d) if d else 9e9
    best = min(cands, key=miss)
    return best if miss(best) <= 0.4 else None                 # nothing near the profit: the profit's own owners line was not read, so use the profit


FACTORS = (0.001, 0.01, 0.1, 10, 100, 1000)


def repair(found, n):
    """Some filings lose or move a decimal point in the text layer ("7147" for 71.47, "899,18" for 899.18): the digits are right, the scale is wrong.
    Each column of a results table has to add up, so a cell that breaks an identity and is put right by a power of ten (and no other cell can do it) is rescaled.
        revenue + other income = total income      profit before tax - tax = profit      profit before exceptional items + exceptional items = profit before tax
        total income - total expenses = profit before exceptional items (expenses may leave out interest and depreciation, and profit may include associates)
    Values are changed in place; anything that stays inconsistent is still caught by the checks in bse._build."""
    def get(k, i):
        return found[k][i] if k in found and i < len(found[k]) else None

    def tol(*vals):
        return max(1.5, 0.004 * max(abs(v) for v in vals if v is not None))

    for _ in range(3):
        changed = False
        for i in range(n):
            ids = []                                                       # (members {key: sign}, target key)
            ids.append(({"sales": 1, "other": 1}, "tinc"))
            ids.append(({"pbt": 1, "tax": -1}, "np"))
            ids.append(({"pbe": 1, "exc": 1}, "pbt"))
            ids.append(({"tinc": 1, "texp": -1}, "pbe"))
            for members, tgt in ids:
                keys = list(members) + [tgt]
                vals = {k: get(k, i) for k in keys}
                if any(v is None for v in vals.values()):
                    continue
                if tgt == "pbe":                                           # texp may or may not carry interest and depreciation, and pbe may carry associates
                    extra = [0.0, (get("fin", i) or 0) + (get("dep", i) or 0)]
                    ok = lambda v: any(abs(v["tinc"] - v["texp"] - e + a - v["pbe"]) <= tol(*v.values()) for e in extra for a in (0.0, get("assoc", i) or 0.0))
                else:
                    ok = lambda v: abs(sum(s * v[k] for k, s in members.items()) - v[tgt]) <= tol(*v.values())
                if ok(vals):
                    continue
                fixes = []
                for k in keys:
                    for f in FACTORS:
                        t = dict(vals)
                        t[k] = vals[k] * f
                        if ok(t):
                            fixes.append((k, f))
                if len({k for k, _ in fixes}) == 1:                        # one cell explains it; with several candidates, nothing is changed
                    k, f = fixes[0] if len({f for _, f in fixes}) == 1 else min(fixes, key=lambda x: abs(x[1] - 1))
                    found[k][i] = vals[k] * f
                    changed = True
        if not changed:
            break
    # profit attributable to owners has no identity of its own: it should be about the size of the period's profit
    for i in range(n):
        o = get("owners", i)
        ref = get("np", i)
        if ref is None and get("pbt", i) is not None:
            ref = get("pbt", i) - (get("tax", i) or 0)
        if o is None or not ref or abs(ref) < 1 or o == 0:
            continue
        if not 0.1 <= abs(o) / abs(ref) <= 10 and o * ref > 0:
            best = min(((f, abs(abs(o * f) / abs(ref) - 1)) for f in FACTORS), key=lambda x: x[1])
            if best[1] < 0.6:
                found["owners"][i] = o * best[0]


def _build_op(best):
    """bse._build, with operating profit taken as revenue less operating costs (total expenses without interest and depreciation).
    That needs no 'other income' or 'total income' line, which are the cells most often garbled, and it excludes the share of associates' profit.
    The older formula (profit before exceptional items + interest + depreciation - other income) is used when total expenses are missing,
    and a quarter whose two answers disagree is flagged."""
    out = bse._build(best)
    f, div = best["found"], best["div"]
    for i, q in enumerate(out):
        def g(k):
            return f[k][i] / div if k in f and f[k][i] is not None else None
        sales, fin, dep, texp, tinc, pbe = g("sales"), g("fin"), g("dep"), g("texp"), g("tinc"), g("pbe")
        if None in (sales, texp, fin, dep):
            continue
        assoc = g("assoc") or 0.0
        carries = True                                                        # total expenses include finance costs and depreciation, as Schedule III lays them out
        if tinc is not None and pbe is not None:
            carries = abs(tinc - texp + assoc - pbe) <= max(2.0, 0.004 * abs(tinc))
        new = round(sales - (texp - fin - dep if carries else texp), 1)
        if q["op"] is not None and abs(new - q["op"]) > max(2.0, 0.01 * abs(sales)) and abs(assoc) < 1e-9:
            q["flags"].append("operating profit two ways disagrees")
        q["op"] = new
        if "missing rows" in q["flags"]:       # the profit-before-tax line is not needed for operating profit when total expenses are there
            q["flags"].remove("missing rows")
    for q in out:                                  # profit or operating profit bigger than sales is a scale slip, not a result
        s_ = abs(q["sales"] or 0)
        if s_ and (abs(q["op"] or 0) > 1.5 * s_ or (q["op"] or 0) > 0.8 * s_):
            q["flags"].append("operating margin out of proportion to sales (units?)")
    return out


def _build(best):
    """The quarters of one table: operating profit as in _build_op, each column dated from the header row when it prints them, and a `strong`
    mark on every quarter whose figures check out on three or more of the table's own identities (none failing). Such a quarter is the company's
    arithmetic, whatever another source says."""
    out = _build_op(best)
    dates = best.get("dates") or []
    if len(dates) >= len(out) and dates and dates[0] == best["qend"] and len(set(dates[:len(out)])) == len(out):
        for q, d in zip(out, dates):
            q["qend"] = d
    f, div = best["found"], best["div"]
    for i, q in enumerate(out):
        def g(k):
            return f[k][i] / div if k in f and f[k][i] is not None else None
        sales, other, tinc, texp, fin, dep = g("sales"), g("other"), g("tinc"), g("texp"), g("fin"), g("dep")
        pbe, exc, pbt, tax, np_ = g("pbe"), g("exc") or 0.0, g("pbt"), g("tax"), g("np")
        assoc = g("assoc") or 0.0
        ok = bad = 0
        tl = lambda *v: max(2.0, 0.004 * max(abs(x) for x in v))
        for have, diff, size in (
                (None not in (sales, other, tinc), None if None in (sales, other, tinc) else sales + other - tinc, (sales, tinc) if sales is not None and tinc is not None else (1,)),
                (None not in (tinc, texp, pbe), None if None in (tinc, texp, pbe) else min(abs(tinc - texp + assoc - pbe), abs(tinc - texp + assoc - pbe - (fin or 0) - (dep or 0))), (tinc,) if tinc is not None else (1,)),
                (None not in (pbt, tax, np_), None if None in (pbt, tax, np_) else pbt - tax - np_, (pbt,) if pbt is not None else (1,)),
                (None not in (pbe, pbt) and exc is not None, None if None in (pbe, pbt) else pbe + exc - pbt, (pbt,) if pbt is not None else (1,))):
            if not have:
                continue
            if abs(diff) <= tl(*size):
                ok += 1
            else:
                bad += 1
        q["strong"] = bool(not q["flags"] and ok >= 3 and bad == 0)
        q["owners_ok"] = g("owners") is not None
    return out


def _looks_like_results(text):
    low = text.lower()
    hits = sum(k in low for k in ("revenue", "income", "expenses", "profit", "tax", "depreciation", "finance", "ended"))
    nums = len(re.findall(r"\d[\d,]*\.?\d*", text))
    return hits >= 4 and nums >= 25 and re.search(r"quarter|three months|3 months|months ended|ended", low)


def _hit_score(text):
    low = text.lower()
    return sum(k in low for k in ("revenue", "total income", "expenses", "profit before", "tax", "depreciation", "finance", "profit for"))


def quarters_from_pdf(pdf_bytes, use_ocr=False, filed=None, basis=None, max_pages=60, ocr_pages=3):
    """-> list of quarter dicts like bse.quarters_from_pdf, from the cleanest results table in the filing."""
    best, best_key = [], None

    def consider(r):
        nonlocal best, best_key
        if not r or not ({"sales"} <= set(r["found"])) or not ({"np", "pbt", "pbe"} & set(r["found"])):
            return
        if basis and r["basis"] != basis:
            return
        out = bse._check_date(_build(r), filed)
        key = (bse._nflags(out), r["basis"] != "Consolidated", -len(r["found"]))
        if best_key is None or key < best_key:
            best, best_key = out, key

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        cands, blank = [], []
        for page in pdf.pages[:max_pages]:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            if _looks_like_results(text):
                cands.append((page, text))
            elif len(text.strip()) < 40 and page.images:                  # a scanned page with no text layer at all
                blank.append((page, text))
        for page, text in cands:
            try:
                consider(parse_page(page, text))
            except Exception:
                continue
            if best and not any(q["flags"] for q in best) and best[0]["basis"] == "Consolidated":
                break
        if use_ocr and (not best or any(q["flags"] for q in best)):
            todo = sorted(cands, key=lambda c: -_hit_score(c[1]))[:ocr_pages] + blank[:6]
            for page, text in todo:
                try:
                    r = parse_page(page, text, words=bse.ocr_words(page))
                except Exception:
                    continue
                if r:
                    r["ocr"] = True
                consider(r)
                if best and not any(q["flags"] for q in best):
                    break
    return best


def quarters_by_basis(pdf_bytes, use_ocr=False, filed=None, max_pages=60):
    """-> {'Consolidated': [...], 'Standalone': [...]}: the cleanest results table the filing holds for each basis."""
    best = {}

    def consider(r):
        if not r or "sales" not in r["found"] or not ({"np", "pbt", "pbe"} & set(r["found"])):
            return
        out = bse._check_date(_build(r), filed)
        key = (bse._nflags(out), -len(r["found"]))
        if r["basis"] not in best or key < best[r["basis"]][0]:
            best[r["basis"]] = (key, out)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        cands = []
        for page in pdf.pages[:max_pages]:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            if _looks_like_results(text):
                cands.append((page, text))
        for page, text in cands:
            try:
                consider(parse_page(page, text))
            except Exception:
                continue
        if use_ocr and any(not v[1] or any(q["flags"] for q in v[1]) for v in best.values()) or (use_ocr and not best):
            for page, text in sorted(cands, key=lambda c: -_hit_score(c[1]))[:3]:
                try:
                    r = parse_page(page, text, words=bse.ocr_words(page))
                except Exception:
                    continue
                if r:
                    r["ocr"] = True
                consider(r)
    return {b: v[1] for b, v in best.items()}
