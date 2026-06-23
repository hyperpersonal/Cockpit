"""FMP data client -- fail-open: any error -> log + return empty, never raise into the pipeline.
Endpoint paths target FMP's `stable` API (field names verified live 2026-06-22)."""
from __future__ import annotations
import os, requests, logging

log = logging.getLogger("cockpit.fmp")
BASE = "https://financialmodelingprep.com/stable"
KEY = os.getenv("FMP_API_KEY", "")

def _get(path: str, **params):
    params["apikey"] = KEY
    try:
        r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("FMP %s failed: %s", path, e)
        return None

def batch_quote(symbols: list) -> list:
    return _get("batch-quote", symbols=",".join(symbols)) or []

def hist_light(symbol: str, frm: str) -> list:
    return _get("historical-price-eod/light", symbol=symbol, **{"from": frm}) or []

def stock_news(symbols: list, limit: int = 8) -> list:
    return _get("news/stock", symbols=",".join(symbols), limit=limit) or []

def price_target(symbol: str) -> dict:
    d = _get("price-target-consensus", symbol=symbol)
    return (d or [{}])[0] if isinstance(d, list) else (d or {})

def key_metrics_ttm(symbol: str) -> dict:
    d = _get("key-metrics-ttm", symbol=symbol)
    return (d or [{}])[0] if isinstance(d, list) else (d or {})

def earnings(symbol: str) -> list:
    """Earnings history+estimates for a symbol (verified: date/epsActual/epsEstimated/revenue*)."""
    return _get("earnings", symbol=symbol) or []

def upcoming_earnings(symbol: str, today: str) -> dict:
    """Next scheduled earnings (epsActual is None) on/after today, else {}."""
    rows = [r for r in earnings(symbol) if r.get("date", "") >= today and r.get("epsActual") is None]
    return min(rows, key=lambda r: r["date"]) if rows else {}

def screener(**filters) -> list:
    rows = _get("company-screener", **filters) or []
    return [r for r in rows if r.get("exchangeShortName") in ("NASDAQ", "NYSE", "AMEX")
            and not r.get("isFund") and not r.get("isEtf", False)]
