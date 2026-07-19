"""Two-layer screener (P1 spec, code-deepened).
Layer 1 (top-down): rank AI sub-themes by relative strength vs SPY + breadth + lifecycle.
Layer 2 (bottom-up): scan the whole universe, rank NEW candidates by trend strength, no-chase,
and theme leadership. Serenity 14-pt + VCP contraction remain LLM/manual (need fundamentals/
intraday structure not pulled in the daily scan) -- flagged as such, not faked. ASCII-only.

Lifecycle/overheated (B10, calibrated 2026-06-23): driven by SHORT-term extension (avg dist above
50DMA = 乖离) + proximity to 52w high (avg off_high), NOT raw distance above the 200DMA (which in a
year-long bull run flags everything 'exhausting'). 'overheated' = stretched above 50DMA AND near
highs (parabolic/chase risk); a theme that ran far but has pulled back off its highs is NOT 'overheated'."""
from __future__ import annotations
from . import fmp

def _ext(q: dict) -> dict:
    p = q.get("price"); a50 = q.get("priceAvg50"); a200 = q.get("priceAvg200")
    hi = q.get("yearHigh"); lo = q.get("yearLow")
    def pct(x, y): return round((x / y - 1) * 100, 1) if (x and y) else None
    pos52 = round((p - lo) / (hi - lo) * 100, 0) if (p and hi and lo and hi > lo) else None
    return {"price": p, "vs50": pct(p, a50), "vs200": pct(p, a200), "off_high": pct(p, hi),
            "pos_52w": pos52, "chg": q.get("changePercentage"),
            "above200": bool(p and a200 and p > a200)}

def _lifecycle(avg_vs50, avg_off_high) -> str:
    v = avg_vs50 if avg_vs50 is not None else 0.0
    oh = avg_off_high if avg_off_high is not None else -99.0
    if oh <= -20:             return "correcting"     # theme well (>20%) off its highs -> cooled
    if v >= 25 and oh >= -6:  return "exhausting"     # very stretched above 50DMA AND at the highs
    if v >= 10:               return "trending"       # solid uptrend
    if v >= 0:                return "accelerating"    # early / just turning up
    return "weak"                                     # below 50DMA on average

def _overheated(avg_vs50, avg_off_high) -> bool:
    return (avg_vs50 is not None and avg_vs50 >= 25
            and avg_off_high is not None and avg_off_high >= -6)   # parabolic: stretched + at highs

def quote_map(symbols: list) -> dict:
    return {q["symbol"]: q for q in fmp.batch_quote(sorted(set(symbols))) if "symbol" in q}

def subtheme_strength(subthemes: dict, quotes: dict, bench_vs200: float = 0.0) -> list:
    rows = []
    for name, v in subthemes.items():
        syms = (v.get("etfs") or v.get("names"))
        ext = [_ext(quotes[s]) for s in syms if s in quotes and quotes[s].get("priceAvg200")]
        if not ext:
            continue
        vs50 = [e["vs50"] for e in ext if e["vs50"] is not None]
        vs200 = [e["vs200"] for e in ext if e["vs200"] is not None]
        offhi = [e["off_high"] for e in ext if e["off_high"] is not None]
        avg50 = round(sum(vs50) / len(vs50), 1) if vs50 else 0.0
        avg200 = round(sum(vs200) / len(vs200), 1) if vs200 else 0.0
        avg_off = round(sum(offhi) / len(offhi), 1) if offhi else None
        breadth = round(sum(1 for e in ext if e["above200"]) / len(ext) * 100, 0)
        rows.append({"subtheme": name, "avg_vs50": avg50, "avg_vs200": avg200,
                     "rel_vs_spy": round(avg200 - bench_vs200, 1),     # relative strength vs SPY (200d)
                     "breadth_above200_pct": breadth, "avg_off_high": avg_off,
                     "lifecycle": _lifecycle(avg50, avg_off), "overheated": _overheated(avg50, avg_off),
                     "lead": (avg200 - bench_vs200) > 0, "members": len(ext)})
    return sorted(rows, key=lambda r: r["rel_vs_spy"], reverse=True)

def name_setup(symbol: str, q: dict, bias_threshold: float = 5.0, bench_vs200: float = 0.0) -> dict:
    e = _ext(q)
    stage2 = (e["vs50"] or -9) > 0 and (e["vs200"] or -9) > 0          # Minervini-ish Stage 2
    chasing = (e["vs50"] or 0) > bias_threshold * 1.5                  # no-chase (strong-trend 1.5x)
    posture = "extended/wait-pullback" if chasing else ("buyable-on-support" if stage2 else "trend-unconfirmed/watch")
    return {**e, "stage2": stage2, "chasing": chasing, "posture": posture,
            "rs_vs_spy": round((e["vs200"] or 0) - bench_vs200, 1)}

def rank_candidates(subthemes: dict, quotes: dict, bench_vs200: float,
                    exclude: set, top: int = 8) -> list:
    sub_of = {}
    for name, v in subthemes.items():
        for s in v.get("names", []):
            sub_of.setdefault(s, name)
    cands = []
    for s, sub in sub_of.items():
        if s in exclude or s not in quotes or not quotes[s].get("priceAvg200"):
            continue
        st = name_setup(s, quotes[s], bench_vs200=bench_vs200)
        if not st["stage2"]:
            continue
        score = (st["rs_vs_spy"] or 0)
        if st["chasing"]:        score -= 30
        if (st["off_high"] or -99) > -8: score -= 10
        if (st["pos_52w"] or 0) > 95:    score -= 10
        q = quotes[s]; _hi = q.get("yearHigh"); _a50 = q.get("priceAvg50")
        cands.append({"ticker": s, "subtheme": sub, "score": round(score, 1),
                      "posture": st["posture"], "vs50": st["vs50"], "vs200": st["vs200"],
                      "off_high": st["off_high"], "rs_vs_spy": st["rs_vs_spy"],
                      "wait_20pct": round(_hi * 0.80, 2) if _hi else None,      # B38: leader-pullback entries
                      "wait_25pct": round(_hi * 0.75, 2) if _hi else None,
                      "wait_ma50": round(_a50, 2) if _a50 else None,
                      "serenity_14": "LLM/manual (needs fundamentals+filings)",
                      "vcp_state": "LLM/manual (needs intraday base structure)"})
    return sorted(cands, key=lambda c: c["score"], reverse=True)[:top]


def market_position(quotes: dict):
    """B39 v1: transparent 0-100 market-position score (higher = hotter / chase-risk) from
    (a) QQQ distance to 52w high mapped [-25%..0] -> [0..100], (b) QQQ vs 200DMA mapped
    [-20..+20] -> [0..100], (c) VIX mapped [40..12] -> [0..100]; equal-weight mean of the
    components available. Valuation (CAPE) percentile NOT included yet (external data, see
    BACKLOG B39). Fail-open: None without QQQ quote; VIX optional. No leverage anywhere."""
    q = quotes.get("QQQ", {}); v = quotes.get("^VIX", {})
    p, hi, a200 = q.get("price"), q.get("yearHigh"), q.get("priceAvg200")
    if not (p and hi and a200):
        return None
    off_high = (p / hi - 1) * 100
    vs200 = (p / a200 - 1) * 100
    vix = v.get("price")
    comp = [max(0.0, min(100.0, 100 + off_high * 4)),
            max(0.0, min(100.0, 50 + vs200 * 2.5))]
    if vix:
        comp.append(max(0.0, min(100.0, 100 - (vix - 12) * 100.0 / 28)))
    score = round(sum(comp) / len(comp))
    zone = "high" if score >= 70 else ("neutral" if score >= 40 else "deep_pullback")
    return {"score": score, "zone": zone, "off_high_pct": round(off_high, 1),
            "vs200_pct": round(vs200, 1), "vix": vix,
            "rule": "mean(offhigh[-25..0]->[0..100], vs200[-20..+20]->[0..100], VIX[40..12]->[0..100])"}
