"""Cross-validation gate (acceptance criterion #1). Sanity-check key FMP numbers against an
independent source (Yahoo) and flag mismatches as 待验证. SEC EDGAR for filings/dilution is
a deeper manual step. Fail-open: if the check can't run, mark unverified, never block."""
from __future__ import annotations
import logging, requests
log = logging.getLogger("cockpit.crossval")

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
