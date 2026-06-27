"""Cross-validation gate (acceptance criterion #1). Sanity-check key FMP numbers against an
independent source (Yahoo) and flag mismatches as 待验证.
B17: SEC EDGAR deep check -- real shares-outstanding trend (dilution, split-aware), recent
shelf/ATM-type filings (S-3/424B5/FWP -> Serenity #7), and latest key filings (10-K/Q, 8-K).
Fail-open everywhere: if a check can't run, mark unverified/unavailable, never block."""
from __future__ import annotations
import os, logging, datetime as dt, requests
log = logging.getLogger("cockpit.crossval")

# ---------- Yahoo price cross-check ----------
def yahoo_price(symbol: str) -> float | None:
    """Independent last price from Yahoo (quote). Fail-open."""
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
                         params={"range": "1d", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception as e:
        log.warning("yahoo %s failed: %s", symbol, e)
        return None

def verify_price(symbol: str, fmp_price: float, tol_pct: float = 1.5) -> dict:
    y = yahoo_price(symbol)
    if y is None:
        return {"verified": False, "note": "待验证(Yahoo 不可用)"}
    diff = abs(fmp_price - y) / y * 100 if y else 999
    return {"verified": diff <= tol_pct, "yahoo": y, "diff_pct": round(diff, 2),
            "note": "" if diff <= tol_pct else f"待验证(FMP vs Yahoo 差 {diff:.1f}%)"}

# ---------- SEC EDGAR (B17) ----------
# SEC fair-access requires a User-Agent with a real contact; reuse EMAIL_SENDER (a secret already in
# the workflow env) so we never hard-code a personal email in a public repo.
def _ua() -> dict:
    contact = os.getenv("EDGAR_USER_AGENT") or os.getenv("EMAIL_SENDER") or "research@example.com"
    return {"User-Agent": f"Cockpit/1.0 ({contact})", "Accept-Encoding": "gzip, deflate"}

_CIK_CACHE: dict = {}
DILUTION_FORMS = ("S-3", "S-3/A", "S-1", "S-1/A", "424B5", "424B3", "424B2", "FWP")
KEY_FORMS = ("10-K", "10-Q", "8-K")

def _cik_map() -> dict:
    if _CIK_CACHE:
        return _CIK_CACHE
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=_ua(), timeout=20)
        r.raise_for_status()
        for row in r.json().values():
            _CIK_CACHE[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    except Exception as e:
        log.warning("EDGAR cik map failed: %s", e)
    return _CIK_CACHE

def _cik(ticker: str) -> str | None:
    return _cik_map().get(ticker.upper())

def edgar_shares(ticker: str) -> dict | None:
    """Latest common-shares-outstanding vs ~1yr earlier, from EDGAR XBRL. Split-aware: a jump
    >100% is flagged likely_split (NOT dilution). Fail-open -> None."""
    cik = _cik(ticker)
    if not cik:
        return None
    try:
        r = requests.get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/"
                         "EntityCommonStockSharesOutstanding.json", headers=_ua(), timeout=20)
        r.raise_for_status()
        pts = [p for p in r.json().get("units", {}).get("shares", []) if p.get("val") and p.get("end")]
        pts.sort(key=lambda p: p["end"])
        if not pts:
            return None
        latest = pts[-1]
        ago = (dt.date.fromisoformat(latest["end"]) - dt.timedelta(days=300)).isoformat()
        prior = [p for p in pts if p["end"] <= ago] or pts[:-1]
        out = {"latest": latest["val"], "asof": latest["end"]}
        if prior:
            prev = prior[-1]
            out.update(prev=prev["val"], prev_asof=prev["end"])
            if prev["val"]:
                yoy = round((latest["val"] / prev["val"] - 1) * 100, 1)
                out["yoy_pct"] = yoy
                out["likely_split"] = abs(yoy) > 100      # split, not dilution
        return out
    except Exception as e:
        log.warning("EDGAR shares %s failed: %s", ticker, e)
        return None

def edgar_filings(ticker: str, since_days: int = 180, forms: tuple | None = None,
                  limit: int = 6) -> list:
    """Recent filings (optionally filtered to `forms`) within `since_days`. Fail-open -> []."""
    cik = _cik(ticker)
    if not cik:
        return []
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_ua(), timeout=20)
        r.raise_for_status()
        rec = r.json().get("filings", {}).get("recent", {})
        form = rec.get("form", []); dates = rec.get("filingDate", [])
        accn = rec.get("accessionNumber", []); docs = rec.get("primaryDocument", [])
        cutoff = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
        rows = []
        for i, f in enumerate(form):
            d = dates[i] if i < len(dates) else ""
            if d < cutoff:
                continue
            if forms and f not in forms:
                continue
            a = (accn[i].replace("-", "") if i < len(accn) else "")
            doc = docs[i] if i < len(docs) else ""
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{a}/{doc}"
                   if a and doc else f"https://www.sec.gov/cgi-bin/browse-edgar?"
                   f"action=getcompany&CIK={cik}&type={f}&dateb=&owner=include&count=20")
            rows.append({"form": f, "date": d, "url": url})
        rows.sort(key=lambda x: x["date"], reverse=True)
        return rows[:limit]
    except Exception as e:
        log.warning("EDGAR filings %s failed: %s", ticker, e)
        return []

def edgar_dossier(ticker: str) -> dict:
    """B17 one-shot EDGAR cross-check for a holding/candidate. Fail-open."""
    cik = _cik(ticker)
    if not cik:
        return {"available": False, "note": "EDGAR: CIK 未找到(可能非美国注册/ADR,人工核对)"}
    shares = edgar_shares(ticker)
    dil = edgar_filings(ticker, since_days=180, forms=DILUTION_FORMS, limit=6)
    recent = edgar_filings(ticker, since_days=120, forms=KEY_FORMS, limit=5)
    yoy = (shares or {}).get("yoy_pct")
    split = (shares or {}).get("likely_split")
    # Dilution truth = actual share-count growth; 424B5/424B2 can be DEBT (esp. mega-caps), so the
    # flag keys off shares YoY, with the filings listed only as context for human review.
    flag = bool(yoy is not None and not split and yoy > 5)
    note = []
    if split:
        note.append(f"流通股 YoY {yoy:+.1f}% 但疑似拆股(非稀释),人工核对")
    elif yoy is not None and yoy > 5:
        note.append(f"流通股 YoY {yoy:+.1f}% — 实际稀释")
    elif yoy is not None:
        note.append(f"流通股 YoY {yoy:+.1f}%(无明显稀释)")
    if dil:
        note.append(f"近180天 {len(dil)} 份 S-3/424B5/FWP(可能含债券发行,以流通股变化为准),人工核对")
    return {"available": True, "cik": cik, "shares_outstanding": shares,
            "dilution_filings": dil, "recent_filings": recent,
            "dilution_flag": flag, "note": "; ".join(note) or "无明显稀释信号"}
