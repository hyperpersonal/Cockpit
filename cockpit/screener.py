"""Two-layer screener (P1 spec, code-deepened).
Layer 1 (top-down): rank AI sub-themes by relative strength vs SPY + breadth + lifecycle.
Layer 2 (bottom-up): scan the whole universe, rank NEW candidates by trend strength, no-chase,
and theme leadership. Serenity 14-pt + VCP contraction remain LLM/manual (need fundamentals/
intraday structure not pulled in the daily scan) -- flagged as such, not faked. ASCII-only."""
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

def _lifecycle(avg_vs200) -> str:
    v = avg_vs200 or 0
    if v < 10:  return "emerging"
    if v < 25:  return "accelerating"
    if v < 45:  return "trending"
    if v < 70:  return "mature"
    return "exhausting"

def quote_map(symbols: list) -> dict:
    return {q["symbol"]: q for q in fmp.batch_quote(sorted(set(symbols))) if "symbol" in q}

def subtheme_strength(subthemes: dict, quotes: dict, bench_vs200: float = 0.0) -> list:
    rows = []
    for name, v in subthemes.items():
        syms = (v.get("etfs") or v.get("names"))
        ext = [_ext(quotes[s]) for s in syms if s in quotes and quotes[s].get("priceAvg200")]
        if not ext:
            continue
        vs200 = [e["vs200"] for e in ext if e["vs200"] is not None]
        offhi = [e["off_high"] for e in ext if e["off_high"] is not None]
        avg200 = round(sum(vs200) / len(vs200), 1) if vs200 else 0.0
        breadth = round(sum(1 for e in ext if e["above200"]) / len(ext) * 100, 0)
        rows.append({"subtheme": name, "avg_vs200": avg200,
                     "rel_vs_spy": round(avg200 - bench_vs200, 1),     # relative strength vs SPY
                     "breadth_above200_pct": breadth,
                     "avg_off_high": round(sum(offhi) / len(offhi), 1) if offhi else None,
                     "lifecycle": _lifecycle(avg200), "overheated": avg200 > 70,
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
    """Scan every name in the universe, score, return top NEW candidates (not held/excluded).
    Score favors: strong vs SPY, Stage 2, near (not far past) highs, not parabolic-extended."""
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
            continue                                       # only uptrends
        score = (st["rs_vs_spy"] or 0)                     # relative strength vs SPY
        if st["chasing"]:        score -= 30               # penalize chasing
        if (st["off_high"] or -99) > -8: score -= 10       # right at highs = less room
        if (st["pos_52w"] or 0) > 95:    score -= 10
        cands.append({"ticker": s, "subtheme": sub, "score": round(score, 1),
                      "posture": st["posture"], "vs50": st["vs50"], "vs200": st["vs200"],
                      "off_high": st["off_high"], "rs_vs_spy": st["rs_vs_spy"],
                      "serenity_14": "LLM/manual (needs fundamentals+filings)",
                      "vcp_state": "LLM/manual (needs intraday base structure)"})
    return sorted(cands, key=lambda c: c["score"], reverse=True)[:top]
