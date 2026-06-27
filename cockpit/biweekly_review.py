"""Bi-weekly review (China Sat, every 2 weeks; 14-day anchor). Now PARITY with daily_brief:
real IBKR holdings_snapshot (shares/cost/MV/P&L), upgraded EWMA vol x correlation risk caps,
sub-theme rotation, 选股雷达 candidates, reflection memory, and REAL performance-vs-benchmark
from self-tracked NAV history (state/nav_history.json, appended daily by daily_brief).
ASCII-only source; LLM writes the review in Chinese (7-section 宪法 format)."""
from __future__ import annotations
import os, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, llm, notify, calendars
from .memory import ReflectionMemory
from .daily_brief import _theme_of, _universe, _hist_window, _holdings_snapshot, _candidates_md, _corr_universe

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

def _is_review_week() -> bool:
    anchor = dt.date.fromisoformat(CFG["schedule"]["biweekly_anchor_date"])
    return (dt.date.today() - anchor).days % 14 == 0

def _performance(net_liq_now, bench, today) -> dict:
    """Period return from NAV history vs SPY price return over the same window. Fail-open."""
    p = ROOT / "state" / "nav_history.json"
    navs = {}
    try:
        navs = json.load(open(p, encoding="utf-8")).get("navs", {})
    except Exception:
        pass
    if net_liq_now:
        navs[today] = round(float(net_liq_now), 2)
    ds = sorted(navs)
    if len(ds) < 2:
        return {"status": "NAV history accumulating (need >=2 daily points) -> 业绩待积累"}
    end = ds[-1]
    cutoff = (dt.date.fromisoformat(end) - dt.timedelta(days=16)).isoformat()
    base = [d for d in ds if d >= cutoff] or ds
    sd = base[0]
    port_ret = round((navs[end] / navs[sd] - 1) * 100, 2)
    spy_ret = None
    rows = fmp.hist_light(bench, sd)
    if rows:
        cl = {r["date"]: r["price"] for r in rows}
        ks = sorted(cl)
        if len(ks) >= 2:
            spy_ret = round((cl[ks[-1]] / cl[ks[0]] - 1) * 100, 2)
    return {"window": sd + ".." + end, "portfolio_return_pct": port_ret, "benchmark": bench,
            "benchmark_return_pct": spy_ret,
            "alpha_pct": round(port_ret - spy_ret, 2) if spy_ret is not None else None,
            "note": "approx period net-liq return (not deposit-adjusted); SPY price return same window"}

def build() -> str:
    today = dt.date.today().isoformat()
    holdings = [h["ticker"] for h in CFG["holdings"]]
    exclude = set(CFG.get("exclude", []))
    theme_of = _theme_of()
    bench = CFG.get("benchmark", "SPY")
    quotes = screener.quote_map(_universe())
    bench_vs200 = 0.0
    if bench in quotes and quotes[bench].get("priceAvg200"):
        bench_vs200 = round((quotes[bench]["price"] / quotes[bench]["priceAvg200"] - 1) * 100, 1)

    port = ibkr.get_portfolio()
    if port:
        net_liq = port["net_liq"]; cash = port["cash"]; positions = port["positions"]
        cur_mv = {t: p["mv"] for t, p in positions.items() if t not in exclude}
        port_note = ""
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        positions = {}; cur_mv = {}
        port_note = "IBKR offline: positions/P&L unknown (data gap)."

    total_assets = CFG["account"].get("total_assets_usd", 250000)
    hard_cap_usd = total_assets * CFG["risk"]["single_name_hard_cap_pct_of_total"] / 100.0
    closes = _hist_window(_corr_universe(holdings, theme_of))
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd, theme_of)
    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    dilution = {t: fmp.shares_growth(t) for t in holdings}
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution)
    heat_usd = sum((d["market_value"] or 0) * (d["dist_to_stop_pct"] or 0) / 100.0
                   for d in holdings_snapshot.values())
    portfolio_heat_pct = round(heat_usd / net_liq * 100, 1) if net_liq else None
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=12)
    performance = _performance(net_liq if port else None, bench, today)

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    lessons = mem.retrieve("biweekly review: which holdings lag the leading main-line; rotation; "
                           "what worked vs not; trim laggards; correlation concentration", n=4)

    bundle = dict(date=today, benchmark=bench, bench_vs200=bench_vs200, performance=performance,
                  net_liq=net_liq, cash=cash, port_note=port_note, single_name_hard_cap_usd=hard_cap_usd,
                  portfolio_heat_pct=portfolio_heat_pct, holdings_snapshot=holdings_snapshot,
                  risk_caps=caps, subthemes=subs, new_candidates=candidates, lessons=lessons)
    prompt = ("Write a CHINESE biweekly review from the REAL data below. 7-section 宪法 format:\n"
              "(1) 业绩 vs 基准 -- use performance (portfolio_return_pct vs benchmark_return_pct over "
              "window; alpha_pct). If performance.status says accumulating, say 业绩待积累(NAV历史不足).\n"
              "(2) 主线/板块轮动 -- subthemes ranked by rel_vs_spy (lifecycle/breadth/overheated): "
              "which sub-themes lead vs lag.\n"
              "(3) 逐票逻辑复查 -- per holding in holdings_snapshot: still on the leading main-line? "
              "use rs_vs_spy/posture/vs200 + market_value/unreal_pnl/pct_of_net_liq; flag laggards.\n"
              "(4) 风险敞口 -- risk_caps (EWMA vol x corr; market_value vs cap_usd vs hard cap), "
              "portfolio_heat_pct (open risk to stops, keep <6-8%), any dilution_flag.\n"
              "(5) 反思记忆 -- from lessons: what worked / didn't + the applicable lesson.\n"
              "(6) 下阶段打法 + 操作提示(满足/注意/不满足 checklist) -- incl top new_candidates to "
              "rotate toward (NOT held; with size_... not given here, just rank/posture).\n"
              "(7) 待验证. Never output buy/sell orders. Do not use prior knowledge for prices.\n\n"
              "DATA(JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)[:95000])
    body = llm.run(prompt, model=CFG["models"]["biweekly"], max_tokens=4600)
    return body + _candidates_md(candidates, subs)   # 选股雷达 code-rendered, guaranteed

def main():
    if not _is_review_week() and os.getenv("FORCE_RUN", "false").lower() != "true":
        print("non-review week, skip."); return
    try:
        body = build()
    except Exception as e:
        body = "system degraded: biweekly review error (%s). check data/config." % e
    notify.send("biweekly review %s" % dt.date.today().isoformat(), body)
    print(body)

if __name__ == "__main__":
    main()
