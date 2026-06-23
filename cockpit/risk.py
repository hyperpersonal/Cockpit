"""Volatility x correlation position caps. Ported from ai-hedge-fund risk_manager.py + this
project's dry-run, then hardened (2026-06-23) against the P2 caveats:
 - vol: EWMA (lambda~0.98, ~60-day effective) over up to ~1yr of returns, with per-day returns
   winsorized at +/-12% so a single earnings gap can't blow up the estimate (was: 6-wk equal window).
 - correlation: long (up to ~1yr) window + SAME-SUBTHEME FLOOR (>=0.60) so an earnings-gap can't
   make two same-theme names look uncorrelated and under-count concentration.
 - position_size(): risk-based per-trade share count (Fixed Fractional, Minervini), complements caps.
Hard cap is an ABSOLUTE dollar ceiling (e.g. 12% of TOTAL assets = $30k), passed in."""
from __future__ import annotations
import numpy as np

def _returns(closes_desc: list) -> np.ndarray:
    c = np.array(list(reversed(closes_desc)), dtype=float)   # chronological oldest->newest
    return np.diff(c) / c[:-1]

def annual_vol_simple(closes_desc: list, lookback: int = 252) -> float:
    r = _returns(closes_desc)[-lookback:]
    if len(r) < 2: return 0.25
    return float(np.std(r, ddof=1) * np.sqrt(252))

def ewma_annual_vol(closes_desc: list, lam: float = 0.98, lookback: int = 252,
                    winsor: float = 0.12) -> float:
    """RiskMetrics-style EWMA of squared (winsorized) returns. lam~0.98 => ~60-day effective window;
    recency-aware but no abrupt cliff, and a clipped single jump can't dominate."""
    r = np.clip(_returns(closes_desc)[-lookback:], -winsor, winsor)
    n = len(r)
    if n < 2: return 0.25
    w = lam ** np.arange(n - 1, -1, -1)        # newest gets weight lam^0=1
    w = w / w.sum()
    var = float(np.sum(w * r * r))
    return float(np.sqrt(var * 252))

# keep old name as an alias to EWMA (callers use this)
def annual_vol(closes_desc: list) -> float:
    return ewma_annual_vol(closes_desc)

def vol_adjusted_limit(av: float) -> float:
    base = 0.20
    if av < 0.15:   m = 1.25
    elif av < 0.30: m = 1.0 - (av - 0.15) * 0.5
    elif av < 0.50: m = 0.75 - (av - 0.30) * 0.5
    else:           m = 0.50
    m = max(0.25, min(1.25, m))
    return base * m

def corr_multiplier(avg_corr: float) -> float:
    if avg_corr >= 0.80: return 0.70
    if avg_corr >= 0.60: return 0.85
    if avg_corr >= 0.40: return 1.00
    if avg_corr >= 0.20: return 1.05
    return 1.10

def _corr(a: np.ndarray, b: np.ndarray, lookback: int = 252) -> float:
    n = min(len(a), len(b), lookback)
    if n < 20: return 0.0                      # need enough overlap to be meaningful
    return float(np.corrcoef(a[-n:], b[-n:])[0, 1])

SAME_THEME_CORR_FLOOR = 0.60

def position_caps(closes_by_ticker: dict, net_liq: float, current_mv: dict, cash: float,
                  active: set, hard_cap_usd: float, theme_of: dict | None = None) -> dict:
    """theme_of: ticker -> subtheme name; same-subtheme pairs get correlation floored at 0.60."""
    theme_of = theme_of or {}
    rets = {t: _returns(c) for t, c in closes_by_ticker.items() if len(c) > 2}
    out = {}
    for t, r in rets.items():
        av = ewma_annual_vol(closes_by_ticker[t])
        peers = [o for o in active if o != t and o in rets] or [o for o in rets if o != t]
        def pair_corr(o):
            c = _corr(r, rets[o])
            if theme_of.get(t) and theme_of.get(t) == theme_of.get(o):
                c = max(c, SAME_THEME_CORR_FLOOR)     # don't let an earnings gap mask concentration
            return c
        corrs = [pair_corr(o) for o in peers] if peers else []
        avg_corr = float(np.mean(corrs)) if corrs else 0.0
        max_corr = max(corrs) if corrs else 0.0
        comb = vol_adjusted_limit(av) * corr_multiplier(avg_corr)
        cap = min(net_liq * comb, hard_cap_usd)
        mv = current_mv.get(t, 0.0)
        room = cap - mv
        out[t] = dict(annual_vol=av, annual_vol_simple_1y=annual_vol_simple(closes_by_ticker[t]),
                      vol_limit_pct=vol_adjusted_limit(av) * 100,
                      avg_corr=avg_corr, max_corr=max_corr, corr_mult=corr_multiplier(avg_corr),
                      combined_pct=comb * 100, cap_usd=cap, current_usd=mv,
                      now_pct=mv / net_liq * 100 if net_liq else 0,
                      action=("ADD up to ${:,.0f}".format(min(room, cash if cash else room)) if room > 0
                              else "TRIM ${:,.0f}".format(-room)))
    return out

def position_size(account: float, entry: float, stop: float, risk_pct: float = 1.0,
                  max_position_pct: float | None = None) -> dict | None:
    """Fixed-Fractional per-trade sizing (tradermonty position-sizer, read in full).
    shares = floor(account*risk% / (entry-stop)); optional max-position cap; binding constraint noted."""
    if not (entry and stop and entry > stop):
        return None
    rps = entry - stop
    dollar_risk = account * risk_pct / 100.0
    shares = int(dollar_risk // rps)                       # always round DOWN
    binding = "risk_based"
    if max_position_pct:
        cap_sh = int((account * max_position_pct / 100.0) // entry)
        if cap_sh < shares:
            shares = cap_sh; binding = "max_position_pct"
    return {"shares": shares, "position_value": round(shares * entry, 0),
            "dollar_risk": round(shares * rps, 0),
            "risk_pct_actual": round(shares * rps / account * 100, 2) if account else 0,
            "binding": binding}
