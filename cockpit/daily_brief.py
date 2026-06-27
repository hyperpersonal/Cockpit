"""Daily brief orchestrator. Runs ~US midday (China 00:00, Tue-Sat). Fail-open everywhere.
Flow: trading-day gate -> IBKR portfolio (or labeled gap) -> detect closed positions & auto-log a
reflection lesson (B5) -> FMP universe quotes/news(age-filtered)/earnings -> sub-theme RS + breadth
-> ranked candidates -> EWMA vol x correlation (same-theme floored) caps w/ $30k hard ceiling ->
per-holding REAL stop + portfolio heat -> append NAV history -> Claude (sections 1-8) -> CODE-render
the 选股雷达 candidate table + as-of label and append (so it's NEVER dropped by the LLM). Chinese."""
from __future__ import annotations
import os, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, crossval, llm, notify, calendars
from .memory import ReflectionMemory

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

def _theme_of() -> dict:
    out = {}
    for name, v in CFG.get("subthemes", {}).items():
        for s in v.get("names", []):
            out.setdefault(s, name)
    return out

def _universe() -> list:
    syms = set([CFG.get("benchmark", "SPY")])
    for v in CFG.get("subthemes", {}).values():
        syms |= set(v.get("etfs", [])) | set(v.get("names", []))
    syms |= {h["ticker"] for h in CFG.get("holdings", [])}
    return sorted(syms)

def _hist_window(tickers, days=None):
    days = days or CFG.get("risk", {}).get("hist_window_days", 380)
    frm = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}
    for t in tickers:
        rows = fmp.hist_light(t, frm)
        if rows:
            out[t] = [r["price"] for r in rows]
    return out

def _corr_universe(holdings, theme_of):
    """B19: correlation universe = holdings + every constituent of the subthemes the holdings
    belong to (not just 4 semis), so each holding's same-theme crowding is measurable. Bounded to
    the themes actually held, so the FMP history fan-out stays small."""
    hold = set(holdings)
    hold_themes = {theme_of.get(h) for h in hold if theme_of.get(h)}
    peers = set()
    for name, v in CFG.get("subthemes", {}).items():
        if name in hold_themes:
            peers |= set(v.get("names", []))
    return hold | peers

def _append_nav(date_str, net_liq):
    p = ROOT / "state" / "nav_history.json"
    try:
        d = json.load(open(p, encoding="utf-8")) if p.exists() else {"navs": {}}
    except Exception:
        d = {"navs": {}}
    d.setdefault("navs", {})[date_str] = round(float(net_liq), 2)
    try:
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

def _reflect_on_closes(positions, exclude, mem, today):
    p = ROOT / "state" / "last_positions.json"
    prev = {}
    try:
        prev = json.load(open(p, encoding="utf-8")).get("positions", {})
    except Exception:
        pass
    cur = {}
    for t, d in positions.items():
        if t in exclude:
            continue
        sh, av, mv = d.get("shares"), d.get("avg_price"), d.get("mv")
        cur[t] = round((mv / (sh * av) - 1) * 100, 1) if (mv and sh and av) else None
    closed = [t for t in prev if t not in cur]
    if cur and closed:
        for t in closed:
            last = prev.get(t)
            mem.add(situation="Closed/exited position %s (last unrealized %s%%)." % (t, last),
                    lesson=("Position %s left the book at ~%s%%. Review: did the exit follow the thesis "
                            "and stop discipline? Record realized outcome and what to repeat/avoid." % (t, last)),
                    source="auto: position-close detector", tags=["postmortem", "exit", t])
        try: mem.save()
        except Exception: pass
    try:
        json.dump({"date": today, "positions": cur}, open(p, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass
    return closed

def _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution, dilution_on=True):
    """B20: market value & P&L use the CURRENT FMP price x IBKR shares (not the stale Flex price)."""
    snap = {}
    for t in holdings:
        s = setups.get(t, {})
        p = positions.get(t, {})
        q = quotes.get(t, {})
        price = q.get("price"); a200 = q.get("priceAvg200")
        shares = p.get("shares"); avg = p.get("avg_price"); ibkr_mv = p.get("mv")
        mv = (shares * price) if (shares and price) else ibkr_mv     # prefer current FMP price
        cost_basis = shares * avg if (shares and avg) else None
        pnl = (mv - cost_basis) if (mv and cost_basis) else None
        pnl_pct = round(pnl / cost_basis * 100, 1) if (pnl is not None and cost_basis) else None
        cand_levels = [x for x in [a200, (avg * 0.8 if avg else None)] if x]
        below = [L for L in cand_levels if price and price > L]
        stop_level = max(below) if below else None
        already_broken = bool(cand_levels and price and not below)
        dist_to_stop_pct = round((price - stop_level) / price * 100, 1) if (price and stop_level) else None
        dil = dilution.get(t)
        snap[t] = {"shares": shares, "avg_cost": avg, "market_value": round(mv, 0) if mv else None,
                   "price": price, "ibkr_mv_refonly": round(ibkr_mv, 0) if ibkr_mv else None,
                   "day_chg_pct": q.get("changePercentage"),
                   "unreal_pnl": round(pnl, 0) if pnl is not None else None, "unreal_pnl_pct": pnl_pct,
                   "pct_of_net_liq": round(mv / net_liq * 100, 1) if (mv and net_liq) else None,
                   "vs50": s.get("vs50"), "vs200": s.get("vs200"), "off_high": s.get("off_high"),
                   "rs_vs_spy": s.get("rs_vs_spy"), "posture": s.get("posture"),
                   "stop_review_level": round(stop_level, 2) if stop_level else None,
                   "dist_to_stop_pct": dist_to_stop_pct, "already_broken_down": already_broken,
                   "dilution_yoy_pct": round(dil * 100, 1) if dil is not None else None,
                   "dilution_flag": bool(dilution_on and dil is not None and dil > 0.05)}
    return snap

def _candidates_md(candidates, subs):
    """Deterministic 选股雷达 table appended to the email so the LLM can never drop it."""
    L = ["", "---", "## 📡 选股雷达 / 观察池（系统直出 · 未持有 · 不经 LLM，保证显示）", "",
         "| 候选 | 子板块 | 评分 | 形态 | vs50 | vs200 | 距高 | 1%风险示例股数 |",
         "|---|---|---|---|---|---|---|---|"]
    for c in (candidates or [])[:10]:
        sz = (c.get("size_1pct_stop8") or {}).get("shares", "-")
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            c.get("ticker"), c.get("subtheme"), c.get("score"), c.get("posture"),
            c.get("vs50"), c.get("vs200"), c.get("off_high"), sz))
    if subs:
        lead = ", ".join("%s(%+.0f,%s%s)" % (r["subtheme"], r["rel_vs_spy"], r["lifecycle"],
                         "·过热" if r.get("overheated") else "") for r in subs[:3])
        lag = ", ".join("%s(%+.0f)" % (r["subtheme"], r["rel_vs_spy"]) for r in subs[-2:])
        L += ["", "**板块强弱（相对 SPY）** — 领先: " + lead, "落后: " + lag]
    L.append("> Serenity 14 点/VCP 需人工对基本面+盘面确认；示例股数 = 1%风险、止损设入场−8%、与 $30k 硬顶取 min。")
    return "\n".join(L)

def build() -> str:
    today = dt.date.today().isoformat()
    phase = calendars.market_phase()
    holdings = [h["ticker"] for h in CFG["holdings"]]
    exclude = set(CFG.get("exclude", []))
    theme_of = _theme_of()
    quotes = screener.quote_map(_universe())
    bench = CFG.get("benchmark", "SPY")
    bench_vs200 = 0.0
    if bench in quotes and quotes[bench].get("priceAvg200"):
        bench_vs200 = round((quotes[bench]["price"] / quotes[bench]["priceAvg200"] - 1) * 100, 1)

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    port = ibkr.get_portfolio()
    closed = []; as_of = None
    if port:
        net_liq = port["net_liq"]; cash = port["cash"]; positions = port["positions"]; as_of = port.get("as_of")
        cur_mv = {t: p["mv"] for t, p in positions.items() if t not in exclude}
        port_note = ""
        _append_nav(today, net_liq)
        closed = _reflect_on_closes(positions, exclude, mem, today)
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        positions = {}; cur_mv = {}
        port_note = "IBKR offline: shares/cost/P&L unknown (data gap); caps shown as room-from-flat."

    total_assets = CFG["account"].get("total_assets_usd", 250000)
    hard_cap_usd = total_assets * CFG["risk"]["single_name_hard_cap_pct_of_total"] / 100.0
    closes = _hist_window(_corr_universe(holdings, theme_of))
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd, theme_of)

    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    dilution = {t: fmp.shares_growth(t) for t in holdings}
    dil_on = CFG.get("risk", {}).get("dilution_atm_disqualifier", True)
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution, dil_on)
    heat_usd = sum((d["market_value"] or 0) * (d["dist_to_stop_pct"] or 0) / 100.0
                   for d in holdings_snapshot.values())
    portfolio_heat_pct = round(heat_usd / net_liq * 100, 1) if net_liq else None
    broken = [t for t, d in holdings_snapshot.items() if d["already_broken_down"]]

    xval = {t: crossval.verify_price(t, quotes[t]["price"]) for t in holdings if t in quotes}
    maxage = CFG.get("data", {}).get("news_max_age_days", 3)
    cutoff = (dt.date.today() - dt.timedelta(days=maxage)).isoformat()
    def _recent(n):
        d = str(n.get("publishedDate") or n.get("date") or "")[:10]
        return (not d) or d >= cutoff
    news = [n for n in fmp.stock_news(holdings, limit=25) if _recent(n)][:8]
    earn = {t: fmp.upcoming_earnings(t, today) for t in holdings}
    earn = {t: e for t, e in earn.items() if e}
    macro = {s: screener._ext(quotes[s]) for s in [bench, "QQQ", "SMH", "SOXX", "XLK", "IGV", "XLU"]
             if s in quotes}
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=10)
    maxpos_pct = round(hard_cap_usd / net_liq * 100, 1) if net_liq else None
    for c in candidates:
        q = quotes.get(c["ticker"], {}); px = q.get("price")
        c["size_1pct_stop8"] = risk.position_size(net_liq, px, px * 0.92, 1.0, maxpos_pct) if px else None

    weak = [t for t, s in setups.items() if not s["stage2"]]
    situation = "Holdings " + ",".join(holdings) + "; weak/below-MA: %s; phase %s" % (weak, phase)
    lessons = mem.retrieve(situation, n=3)

    bundle = dict(date=today, phase=phase, phase_rule=calendars.PHASE_GUARDRAIL.get(phase, ""),
                  as_of=as_of, port_note=port_note, net_liq=net_liq, cash=cash, total_assets=total_assets,
                  single_name_hard_cap_usd=hard_cap_usd, portfolio_heat_pct=portfolio_heat_pct,
                  broken_down_holdings=broken, closed_positions=closed, benchmark=bench,
                  bench_vs200=bench_vs200, holdings_snapshot=holdings_snapshot, risk_caps=caps,
                  cross_validation=xval, earnings_calendar=earn, news=news, macro=macro,
                  subthemes=subs, lessons=lessons)
    if phase == "non_trading":
        return "[%s] US market closed; no brief today." % today

    prompt = ("Write a CHINESE daily brief from the REAL data below. 8 sections (the 选股雷达 is appended "
              "by code, do NOT write it):\n"
              "(1) 组合快照 -- ONLY holdings_snapshot names; per name shares/avg_cost/market_value/"
              "unreal_pnl/unreal_pnl_pct/pct_of_net_liq + price/day_chg (price=current FMP). Also net_liq/"
              "cash/single_name_hard_cap_usd and the as_of date. If closed_positions non-empty, note them.\n"
              "(2) 持仓关键消息 from news.\n(3) 大盘/宏观 from macro + bench_vs200.\n"
              "(4) 财报/事件日历 from earnings_calendar.\n"
              "(5) 技术位/支撑阻力: per holding vs50/vs200/off_high/rs_vs_spy/posture + stop_review_level "
              "(real stop BELOW price) + dist_to_stop_pct; if already_broken_down=true say 已跌破技术位/"
              "成本止损，按纪律评估减仓.\n"
              "(6) 风控触发 from risk_caps (market_value vs cap_usd vs single_name_hard_cap_usd). "
              "portfolio_heat_pct = open risk to stops / net liq, keep <6-8%. Note dilution_flag/"
              "dilution_yoy_pct + broken_down_holdings.\n"
              "(7) 今日操作提示 -- per flagged holding a 满足/注意/不满足 checklist.\n"
              "(8) 待验证 -- mark any number NOT in cross_validation as 待验证.\n"
              "Obey phase_rule. Never output buy/sell orders. Do not use prior knowledge for prices.\n\n"
              "DATA(JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)[:95000])
    body = llm.run(prompt, model=CFG["models"]["daily"], max_tokens=4200)
    header = ("> ⏱️ 持仓数据截至 %s（IBKR Flex 上一交易日；**当日交易可能未反映**——如需当日，Flex 周期改 Today）。\n\n"
              % as_of) if as_of else ""
    return header + body + "\n" + _candidates_md(candidates, subs)   # 选股雷达 code-rendered, guaranteed

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
