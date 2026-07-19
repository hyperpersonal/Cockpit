"""Bi-weekly review (China Sat, every 2 weeks; 14-day anchor). Now PARITY with daily_brief:
real IBKR holdings_snapshot (shares/cost/MV/P&L), upgraded EWMA vol x correlation risk caps,
sub-theme rotation, 选股雷达 candidates, reflection memory, and REAL performance-vs-benchmark
from self-tracked NAV history (state/nav_history.json, appended daily by daily_brief).
LLM writes the review in Chinese (7-section 宪法 format); scoreboard/radar are code-rendered."""
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

def _adherence_md() -> str:
    """B29 adherence scoreboard, code-rendered (never touches the LLM). Reads
    state/signal_history.json (written daily by daily_brief). An episode = the first day a
    ticker shows a signal until it clears/position leaves. acted = shares later cut >=5%.
    Cost-of-ignoring: trim episodes use the suggested trim amount at signal price; broken
    episodes use the full position. Positive = ignoring cost money; negative = ignoring
    happened to pay off -- shown honestly either way."""
    p = ROOT / "state" / "signal_history.json"
    try:
        days = json.load(open(p, encoding="utf-8")).get("days", [])
    except Exception:
        days = []
    head = "\n\n---\n## 🧾 依从性记分板（B29 · 代码直出）\n"
    if len(days) < 2:
        return head + "数据积累中（signal_history 需≥2 个交易日）。\n"
    latest = days[-1].get("holdings", {})
    episodes, active = [], {}
    for day in days:
        h = day.get("holdings", {})
        for t_, e in h.items():
            if e.get("signal") and t_ not in active:
                active[t_] = {"t": t_, "date": day["date"], "signal": e["signal"],
                              "shares0": e.get("shares"), "price0": e.get("price"),
                              "trim_usd": e.get("trim_usd")}
            elif not e.get("signal") and t_ in active:
                episodes.append(active.pop(t_))
        for t_ in list(active):
            if t_ not in h:
                episodes.append(active.pop(t_))
    episodes += list(active.values())
    rows, tot_cost, n_act = [], 0.0, 0
    for ep in episodes:
        sh0, p0 = ep.get("shares0"), ep.get("price0")
        if not (sh0 and p0):
            continue
        cur = latest.get(ep["t"]) or {}
        cur_sh = cur.get("shares") or 0.0
        cur_p = cur.get("price") or p0
        acted = cur_sh < sh0 * 0.95
        chg = (cur_p / p0 - 1) * 100
        if acted:
            n_act += 1; cost_s = "—"
        else:
            n_tr = (ep["trim_usd"] / p0) if ep.get("trim_usd") else sh0
            cost = n_tr * (p0 - cur_p)
            tot_cost += cost; cost_s = "$%+.0f" % cost
        rows.append("| %s | %s | %s | %s | %+.1f%% | %s |" % (
            ep["date"], ep["t"], ep["signal"], "✅ 执行" if acted else "❌ 无视", chg, cost_s))
    if not rows:
        return head + "本期无信号事件。\n"
    out = [head.rstrip("\n"),
           "> 「无视的代价」= 若在信号日按建议执行（trim 按建议金额 / broken 按全仓）对比最新价的差额；正数=无视多亏了这么多，负数=无视反而占了便宜（如实展示）。数据自 %s 起积累。" % days[0]["date"],
           "", "| 信号日 | 票 | 信号 | 执行? | 信号日至今价格 | 无视的代价 |", "|---|---|---|---|---|---|"]
    out += rows
    out += ["", "**合计：执行 %d 条 / 无视 %d 条；无视信号的净代价 ≈ $%+.0f**" % (n_act, len(rows) - n_act, tot_cost), ""]
    return "\n".join(out)

def build() -> str:
    today = dt.date.today().isoformat()
    cfg_holdings = [h["ticker"] for h in CFG.get("holdings", [])]
    exclude = set(CFG.get("exclude", []))
    theme_of = _theme_of()
    bench = CFG.get("benchmark", "SPY")

    port = ibkr.get_portfolio()
    if port:
        net_liq = port["net_liq"]; cash = port["cash"]; positions = port["positions"]
        holdings = sorted({t for t in positions if t not in exclude})   # B33: IBKR-driven active book
        cur_mv = {t: p["mv"] for t, p in positions.items() if t not in exclude}
        port_note = ""
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        positions = {}; cur_mv = {}
        holdings = cfg_holdings                                          # fail-open fallback
        port_note = "IBKR offline: positions/P&L unknown (data gap)."
    quotes = screener.quote_map(sorted(set(_universe()) | set(holdings)))
    bench_vs200 = 0.0
    if bench in quotes and quotes[bench].get("priceAvg200"):
        bench_vs200 = round((quotes[bench]["price"] / quotes[bench]["priceAvg200"] - 1) * 100, 1)

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
    return body + _adherence_md() + _candidates_md(candidates, subs)   # 记分板+雷达 code-rendered

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
