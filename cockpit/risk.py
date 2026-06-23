"""Volatility x correlation position caps. Ported from ai-hedge-fund risk_manager.py
(read in full) + validated in this project's risk_calc.py dry-run.
Hard cap is an ABSOLUTE dollar ceiling (e.g. 12% of TOTAL assets = $30k), passed in."""
from __future__ import annotations
import numpy as np

def daily_returns(closes_desc: list) -> np.ndarray:
    c = np.array(list(reversed(closes_desc)), dtype=float)
    return np.diff(c) / c[:-1]

def annual_vol(closes_desc: list) -> float:
    r = daily_returns(closes_desc)
    if len(r) < 2:
        return 0.25
    return float(np.std(r, ddof=1) * np.sqrt(252))

def vol_adjusted_limit(av: float) -> float:
    """Exact replication of ai-hedge-fund logic. Returns pct of net liq (0.05-0.25)."""
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

def _corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 5: return 0.0
    return float(np.corrcoef(a[-n:], b[-n:])[0, 1])

def position_caps(closes_by_ticker: dict, net_liq: float, current_mv: dict, cash: float,
                  active: set, hard_cap_usd: float) -> dict:
    """closes_by_ticker: ticker -> close series (date-desc). current_mv: ticker -> $ held.
    hard_cap_usd: absolute single-name ceiling (e.g. 12% of total assets)."""
    rets = {t: daily_returns(c) for t, c in closes_by_ticker.items() if len(c) > 2}
    out = {}
    for t, r in rets.items():
        av = annual_vol(closes_by_ticker[t])
        peers = [o for o in active if o != t and o in rets] or [o for o in rets if o != t]
        corrs = [_corr(r, rets[o]) for o in peers] if peers else []
        avg_corr = float(np.mean(corrs)) if corrs else 0.0
        max_corr = max(corrs) if corrs else 0.0
        comb = vol_adjusted_limit(av) * corr_multiplier(avg_corr)
        cap = min(net_liq * comb, hard_cap_usd)        # vol*corr cap, floored by absolute hard ceiling
        mv = current_mv.get(t, 0.0)
        room = cap - mv
        out[t] = dict(annual_vol=av, vol_limit_pct=vol_adjusted_limit(av) * 100,
                      avg_corr=avg_corr, max_corr=max_corr, corr_mult=corr_multiplier(avg_corr),
                      combined_pct=comb * 100, cap_usd=cap, current_usd=mv,
                      now_pct=mv / net_liq * 100 if net_liq else 0,
                      action=("ADD up to ${:,.0f}".format(min(room, cash if cash else room)) if room > 0
                              else "TRIM ${:,.0f}".format(-room)))
    return out
