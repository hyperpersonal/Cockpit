"""Daily brief orchestrator. Runs ~US midday (China 00:00, Tue-Sat). Fail-open everywhere.
Flow: trading-day gate -> IBKR portfolio (or labeled gap) -> FMP universe quotes/news/earnings
-> sub-theme RS vs SPY + breadth + lifecycle -> ranked NEW candidates -> vol*corr caps ($30k hard
ceiling) -> cross-validate prices -> retrieve memory lessons -> Claude -> email.
ASCII-only source; LLM is instructed to write the brief in Chinese (8-section 宪法 format)."""
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
        net_liq = port["net_liq"]; cash = port["cash"]
        cur_mv = {t: p["mv"] for t, p in port["positions"].items() if t not in exclude}
        port_note = ""
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        cur_mv = {}
        port_note = "IBKR offline: position $ unknown (data gap); caps shown as room-from-flat."

    total_assets = CFG["account"].get("total_assets_usd", 250000)
    hard_cap_usd = total_assets * CFG["risk"]["single_name_hard_cap_pct_of_total"] / 100.0
    closes = _hist_window(set(holdings) | set(CFG["subthemes"]["semis_gpu_asic"]["names"][:3]))
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd)

    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    xval = {t: crossval.verify_price(t, quotes[t]["price"]) for t in holdings if t in quotes}
    news = fmp.stock_news(holdings, limit=8)
    earn = {t: fmp.upcoming_earnings(t, today) for t in holdings}
    earn = {t: e for t, e in earn.items() if e}                       # only those with a date
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=8)

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    weak = [t for t, s in setups.items() if not s["stage2"]]
    situation = "Holdings " + ",".join(holdings) + "; weak/below-MA: %s; phase %s" % (weak, phase)
    lessons = mem.retrieve(situation, n=3)

    bundle = dict(date=today, phase=phase, phase_rule=calendars.PHASE_GUARDRAIL.get(phase, ""),
                  port_note=port_note, net_liq=net_liq, cash=cash, total_assets=total_assets,
                  single_name_hard_cap_usd=hard_cap_usd, benchmark=bench, bench_vs200=bench_vs200,
                  holdings=quotes, setups=setups, risk_caps=caps, cross_validation=xval,
                  earnings_calendar=earn, news=news[:8], subthemes=subs,
                  new_candidates=candidates, lessons=lessons)
    if phase == "non_trading":
        return "[%s] US market closed; no brief today." % today

    prompt = ("Write a CHINESE daily brief from the REAL data below. Use the 8-section 宪法 format, "
              "in this order: (1) 组合快照 holdings snapshot, (2) 持仓关键消息 news, (3) 大盘/宏观 "
              "(use benchmark+bench_vs200), (4) 财报/事件日历 earnings_calendar, (5) 技术位/支撑阻力 "
              "from setups (vs50/vs200/off_high), (6) 风控触发 from risk_caps (single_name_hard_cap_usd "
              "is the $ ceiling per name), (7) 今日操作提示 with a satisfy/caution/fail checklist + "
              "also surface top new_candidates by subtheme/score, (8) 待验证 mark any number not in "
              "cross_validation as 待验证. Obey phase_rule. Never output buy/sell orders. Do not use "
              "prior knowledge for current prices.\n\nDATA(JSON):\n"
              + json.dumps(bundle, ensure_ascii=False, default=str)[:90000])
    return llm.run(prompt, model=CFG["models"]["daily"], max_tokens=3800)

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
