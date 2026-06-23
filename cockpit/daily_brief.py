"""Daily brief orchestrator. Runs ~US midday (China 00:00, Tue-Sat). Fail-open everywhere.
Flow: trading-day gate -> IBKR portfolio (or labeled gap) -> FMP universe quotes/news/earnings
-> sub-theme RS vs SPY + breadth + lifecycle -> ranked NEW candidates -> vol*corr caps ($30k hard
ceiling) -> cross-validate prices -> retrieve memory lessons -> Claude -> email.
Section 1 shows ONLY real holdings w/ IBKR shares/cost/MV/unrealized P&L (P&L% from IBKR cost
basis); a dedicated 选股雷达 section always lists the watchlist/candidates. LLM writes Chinese."""
from __future__ import annotations
import os, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, crossval, llm, notify, calendars
from .memory import ReflectionMemory

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

def _universe() -> list:
    syms = set([CFG.get("benchmark", "SPY")])
    for v in CFG.get("subthemes", {}).values():
        syms |= set(v.get("etfs", [])) | set(v.get("names", []))
    syms |= {h["ticker"] for h in CFG.get("holdings", [])}
    return sorted(syms)

def _hist_window(tickers, days=95):
    frm = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}
    for t in tickers:
        rows = fmp.hist_light(t, frm)
        if rows:
            out[t] = [r["price"] for r in rows]
    return out

def _holdings_snapshot(holdings, quotes, setups, positions, net_liq):
    """ONLY real holdings. P&L $ and P&L % BOTH derived from IBKR cost basis (consistent)."""
    snap = {}
    for t in holdings:
        s = setups.get(t, {})
        p = positions.get(t, {})
        q = quotes.get(t, {})
        price = q.get("price")                              # FMP last close (for technicals/day-chg)
        shares = p.get("shares"); avg = p.get("avg_price"); mv = p.get("mv")
        cost_basis = shares * avg if (shares and avg) else None
        pnl = (mv - cost_basis) if (mv and cost_basis) else None
        pnl_pct = round(pnl / cost_basis * 100, 1) if (pnl is not None and cost_basis) else None
        snap[t] = {"shares": shares, "avg_cost": avg, "market_value": round(mv, 0) if mv else None,
                   "ibkr_price": round(mv / shares, 2) if (mv and shares) else None,
                   "fmp_close": price, "day_chg_pct": q.get("changePercentage"),
                   "unreal_pnl": round(pnl, 0) if pnl is not None else None,
                   "unreal_pnl_pct": pnl_pct,
                   "pct_of_net_liq": round(mv / net_liq * 100, 1) if (mv and net_liq) else None,
                   "vs50": s.get("vs50"), "vs200": s.get("vs200"), "off_high": s.get("off_high"),
                   "rs_vs_spy": s.get("rs_vs_spy"), "posture": s.get("posture")}
    return snap

def build() -> str:
    today = dt.date.today().isoformat()
    phase = calendars.market_phase()
    holdings = [h["ticker"] for h in CFG["holdings"]]
    exclude = set(CFG.get("exclude", []))
    quotes = screener.quote_map(_universe())
    bench = CFG.get("benchmark", "SPY")
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
        port_note = "IBKR offline: shares/cost/P&L unknown (data gap); caps shown as room-from-flat."

    total_assets = CFG["account"].get("total_assets_usd", 250000)
    hard_cap_usd = total_assets * CFG["risk"]["single_name_hard_cap_pct_of_total"] / 100.0
    closes = _hist_window(set(holdings) | set(CFG["subthemes"]["semis_gpu_asic"]["names"][:3]))
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd)

    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq)
    xval = {t: crossval.verify_price(t, quotes[t]["price"]) for t in holdings if t in quotes}
    news = fmp.stock_news(holdings, limit=8)
    earn = {t: fmp.upcoming_earnings(t, today) for t in holdings}
    earn = {t: e for t, e in earn.items() if e}
    macro = {s: screener._ext(quotes[s]) for s in [bench, "QQQ", "SMH", "SOXX", "XLK", "IGV", "XLU"]
             if s in quotes}
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=10)

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    weak = [t for t, s in setups.items() if not s["stage2"]]
    situation = "Holdings " + ",".join(holdings) + "; weak/below-MA: %s; phase %s" % (weak, phase)
    lessons = mem.retrieve(situation, n=3)

    bundle = dict(date=today, phase=phase, phase_rule=calendars.PHASE_GUARDRAIL.get(phase, ""),
                  port_note=port_note, net_liq=net_liq, cash=cash, total_assets=total_assets,
                  single_name_hard_cap_usd=hard_cap_usd, benchmark=bench, bench_vs200=bench_vs200,
                  holdings_snapshot=holdings_snapshot, risk_caps=caps, cross_validation=xval,
                  earnings_calendar=earn, news=news[:8], macro=macro, subthemes=subs,
                  new_candidates=candidates, lessons=lessons)
    if phase == "non_trading":
        return "[%s] US market closed; no brief today." % today

    prompt = ("Write a CHINESE daily brief from the REAL data below. 9 sections in order:\n"
              "(1) 组合快照 -- ONLY holdings_snapshot names (your real holdings); per name give "
              "shares/avg_cost/market_value/unreal_pnl/unreal_pnl_pct/pct_of_net_liq + fmp_close/day_chg. "
              "Also net_liq/cash/single_name_hard_cap_usd. Do NOT list watchlist names here.\n"
              "(2) 持仓关键消息 from news.\n(3) 大盘/宏观 from macro + bench_vs200.\n"
              "(4) 财报/事件日历 from earnings_calendar.\n"
              "(5) 技术位/支撑阻力 for holdings (vs50/vs200/off_high/rs_vs_spy/posture).\n"
              "(6) 风控触发 from risk_caps (single_name_hard_cap_usd = $ ceiling/name; cap_usd = "
              "vol*corr cap). State each holding's market_value vs its cap.\n"
              "(7) 今日操作提示 -- per flagged holding a 满足/注意/不满足 checklist.\n"
              "(8) 待验证 -- mark any number NOT in cross_validation as 待验证.\n"
              "(9) 选股雷达/观察池 (MANDATORY, never omit) -- a table of ALL new_candidates "
              "(ticker/subtheme/score/posture/vs50/vs200/off_high), explicitly NOT held; plus the "
              "leading vs lagging subthemes from subthemes (rel_vs_spy/lifecycle/breadth/overheated). "
              "This is the screening output -- always render it fully.\n"
              "Obey phase_rule. Never output buy/sell orders. Do not use prior knowledge for prices.\n\n"
              "DATA(JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)[:90000])
    return llm.run(prompt, model=CFG["models"]["daily"], max_tokens=4600)

def main():
    if not calendars.is_us_trading_day() and os.getenv("FORCE_RUN", "false").lower() != "true":
        print("not a US trading day, skip."); return
    try:
        body = build()
    except Exception as e:
        body = "system degraded: daily brief error (%s). check data/config." % e
    notify.send("daily brief %s" % dt.date.today().isoformat(), body)
    print(body)

if __name__ == "__main__":
    main()
