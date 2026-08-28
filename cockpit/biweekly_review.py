"""Bi-weekly review (China Sat, every 2 weeks; 14-day anchor). Now PARITY with daily_brief:
real IBKR holdings_snapshot (shares/cost/MV/P&L), upgraded EWMA vol x correlation risk caps,
sub-theme rotation, 选股雷达 candidates, reflection memory, and REAL performance-vs-benchmark
from self-tracked NAV history (state/nav_history.json, appended daily by daily_brief).
LLM writes the review in Chinese (7-section 宪法 format); scoreboard/radar are code-rendered."""
from __future__ import annotations
import os, sys, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, llm, notify, calendars
from .memory import ReflectionMemory
from .daily_brief import (_theme_of, _universe, _hist_window, _holdings_snapshot, _candidates_md,
                          _corr_universe, _theme_exposure, _position_audit, _audit_md,
                          _exit_tracking, _exit_track_md)

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

def _attribution(window_days: int = 14) -> dict | None:
    """B49 performance attribution from state/signal_history.json.

    Why not just NAV: `_performance()` reports the raw NAV change, which is polluted by deposits,
    withdrawals and margin interest -- the 2026-08-22 review printed +8.03% with no adjustment and
    the user still could not answer 「我到底靠什么赚/亏的」.

    signal_history stores, per day, net_liq plus every holding's {shares, price}. That is enough to
    separate MARKET P&L from EXTERNAL CASH FLOW without any Flex cash-flow query:
        market P&L(t)  = SUM_i shares_i(t-1) * (px_i(t) - px_i(t-1))      <- what the market did
        residual(t)    = NAV(t) - NAV(t-1) - market P&L(t)                <- deposits / interest / fees
    Trades do not move NAV (ex-commission), so they fall out of both terms; they show up in the
    timing bucket instead.

    Three buckets over the window (each answers a different question):
        选股 = equal-weight return of the names held   -> were these the right names?
        尺寸 = actual-weight return - equal-weight      -> was the weighting right?
        择时 = actual P&L - P&L if day-1 shares never changed -> did the adds/trims help?

    HONEST LIMITS, carried into the rendered output:
      - the window starts 2026-07-20 (when signal_history began); this is NOT since-inception
      - prices are the daily brief's snapshot price. Until B57 moved the brief post-close those were
        INTRADAY snapshots, so early days carry intraday noise
      - commissions and slippage are not visible here
      - `residual` lumps external flows together with interest and fees; it is not a clean flow figure
    Fail-open: returns None if there is not enough history."""
    try:
        raw = json.load(open(ROOT / "state" / "signal_history.json", encoding="utf-8")).get("days") or []
    except Exception:
        return None
    days = [d for d in raw if d.get("net_liq") and d.get("holdings")]
    if len(days) < 3:
        return None
    days = days[-(window_days + 1):] if window_days else days
    first, last = days[0], days[-1]

    mkt_total = 0.0; resid_total = 0.0; twr = 1.0
    per = {}
    for a, b in zip(days, days[1:]):
        ha, hb = a["holdings"], b["holdings"]
        day_mkt = 0.0
        for t, va in ha.items():
            vb = hb.get(t)
            if not vb: continue
            sh, pa, pb = va.get("shares"), va.get("price"), vb.get("price")
            if sh is None or pa is None or pb is None: continue
            c = sh * (pb - pa)
            day_mkt += c
            per[t] = per.get(t, 0.0) + c
        nav_a, nav_b = a["net_liq"], b["net_liq"]
        mkt_total += day_mkt
        resid_total += (nav_b - nav_a) - day_mkt
        if nav_a:
            twr *= (1 + day_mkt / nav_a)

    # 择时：把首日股数冻结，重算同一段行情
    frozen = {}
    for a, b in zip(days, days[1:]):
        ha, hb = a["holdings"], b["holdings"]
        for t, va in ha.items():
            vb = hb.get(t); v0 = first["holdings"].get(t)
            if not vb or not v0: continue
            pa, pb, sh0 = va.get("price"), vb.get("price"), v0.get("shares")
            if pa is None or pb is None or sh0 is None: continue
            frozen[t] = frozen.get(t, 0.0) + sh0 * (pb - pa)

    # 选股 / 尺寸：窗口内价格收益 + 平均权重
    names, rets, wts = [], [], []
    for t, v0 in first["holdings"].items():
        vN = last["holdings"].get(t)
        if not vN: continue
        p0, pN = v0.get("price"), vN.get("price")
        if not p0 or not pN: continue
        w = []
        for d in days:
            h = d["holdings"].get(t)
            if h and h.get("shares") and h.get("price") and d.get("net_liq"):
                w.append(h["shares"] * h["price"] / d["net_liq"])
        if not w: continue
        names.append(t); rets.append(pN / p0 - 1); wts.append(sum(w) / len(w))
    eq = sum(rets) / len(rets) if rets else 0.0
    wtd = sum(w * r for w, r in zip(wts, rets))
    return {"d0": first.get("date"), "dN": last.get("date"), "n_days": len(days),
            "nav0": first["net_liq"], "navN": last["net_liq"],
            "mkt_pnl": mkt_total, "residual": resid_total,
            "twr_pct": (twr - 1) * 100, "raw_nav_pct": (last["net_liq"] / first["net_liq"] - 1) * 100,
            "pick_pct": eq * 100, "size_pct": (wtd - eq) * 100,
            "timing_usd": mkt_total - sum(frozen.values()),
            "per": sorted(per.items(), key=lambda kv: -abs(kv[1]))}

def _attribution_md(a) -> str:
    """B49 rendering. Code-rendered, never handed to the LLM (B22 lesson)."""
    if not a:
        return ""
    L = ["## 🧮 业绩归因（B49 · 规则直出 · %s → %s，%d 个交易日）" % (a["d0"], a["dN"], a["n_days"]), "",
         "| 口径 | 数值 | 它回答什么 |", "|---|---|---|",
         "| 净值变动（未调整） | %+.2f%% | 旧口径，**被出入金污染，不可直接当业绩** |" % a["raw_nav_pct"],
         "| **TWR（剔除出入金）** | **%+.2f%%** | 你的**投资**表现 |" % a["twr_pct"],
         "| 市场盈亏 | $%s | 持仓本身赚/亏的钱 |" % format(a["mkt_pnl"], "+,.0f"),
         "| 残差（出入金/利息/费用） | $%s | 不是投资结果，是钱进出与融资成本 |" % format(a["residual"], "+,.0f"),
         "", "**三分归因**", "",
         "| 来源 | 贡献 | 读法 |", "|---|---|---|",
         "| 选股（等权持有这批票） | %+.2f%% | 这批**票**本身怎么样 |" % a["pick_pct"],
         "| 尺寸（实际权重 − 等权） | %+.2f%% | 你的**权重分配**赚了还是亏了 |" % a["size_pct"],
         "| 择时（实际 − 首日股数不动） | $%s | 期间的**加减仓**帮了还是害了 |" % format(a["timing_usd"], "+,.0f"),
         ""]
    top = a["per"][:6]
    if top:
        L += ["**贡献最大的持仓（市场盈亏，按绝对值排序）**", "",
              "| 票 | 贡献$ |", "|---|---|"]
        L += ["| %s | $%s |" % (t, format(v, "+,.0f")) for t, v in top]
        L.append("")
    L += ["> 口径边界：窗口 = signal_history 里**最近的这些交易日**（该台账 2026-07-20 才开始记，故**绝非成立以来**）；",
          "> 价格取自日报快照，B57（2026-08-27）改盘后之前那些日子是**盘中价**，早期数据带盘中噪音；",
          "> 不含佣金与滑价；残差把出入金、利息、费用混在一起，**不是干净的现金流数字**。", ""]
    return "\n".join(L) + "\n---\n\n"

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
    # B24: risk-table MV on the SAME price basis as the snapshot -- shares x FMP
    # current price; Flex prior-day mv only when no live quote (fail-open).
    cur_mv = {t: ((positions.get(t, {}).get("shares") or 0) * quotes[t]["price"])
                 if (positions.get(t, {}).get("shares") and quotes.get(t, {}).get("price")) else mv
              for t, mv in cur_mv.items()}
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd, theme_of)
    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    dilution = {t: fmp.shares_growth(t) for t in holdings}
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution,
                                           closes=closes)          # B48 parity
    heat_usd = sum((d["market_value"] or 0) * (d["dist_to_stop_pct"] or 0) / 100.0
                   for d in holdings_snapshot.values())
    portfolio_heat_pct = round(heat_usd / net_liq * 100, 1) if net_liq else None
    theme_alerts, _ = _theme_exposure(holdings_snapshot, theme_of, net_liq)   # B36 parity
    audit_rows, audit_tot = _position_audit(holdings_snapshot, caps, net_liq, theme_of, hard_cap_usd)  # B48
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=12)
    performance = _performance(net_liq if port else None, bench, today)
    attribution = _attribution()                                          # B49

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    lessons = mem.retrieve("biweekly review: which holdings lag the leading main-line; rotation; "
                           "what worked vs not; trim laggards; correlation concentration", n=4)

    bundle = dict(date=today, benchmark=bench, bench_vs200=bench_vs200, performance=performance,
                  net_liq=net_liq, cash=cash, port_note=port_note, single_name_hard_cap_usd=hard_cap_usd,
                  portfolio_heat_pct=portfolio_heat_pct, holdings_snapshot=holdings_snapshot,
                  risk_caps=caps, subthemes=subs, new_candidates=candidates, lessons=lessons,
                  theme_exposure_alerts=theme_alerts, position_audit_totals=audit_tot)
    prompt = ("Write a CHINESE biweekly review from the REAL data below. 7-section 宪法 format:\n"
              "(1) 业绩 vs 基准 -- use performance (portfolio_return_pct vs benchmark_return_pct over "
              "window; alpha_pct). If performance.status says accumulating, say 业绩待积累(NAV历史不足).\n"
              "(2) 主线/板块轮动 -- subthemes ranked by rel_vs_spy (lifecycle/breadth/overheated): "
              "which sub-themes lead vs lag.\n"
              "(3) 逐票逻辑复查 -- per holding in holdings_snapshot: still on the leading main-line? "
              "use rs_vs_spy/posture/vs200 + market_value/unreal_pnl/pct_of_net_liq; flag laggards.\n"
              "(4) 风险敞口 -- risk_caps (EWMA vol x corr; market_value vs cap_usd vs hard cap), "
              "portfolio_heat_pct (open risk to stops, keep <6-8%), theme_exposure_alerts (B36 "
              "sub-theme concentration incl leverage factors -- flag prominently), any dilution_flag.\n"
              "(5) 反思记忆 -- from lessons: what worked / didn't + the applicable lesson.\n"
              "(6) 下阶段打法 + 操作提示(满足/注意/不满足 checklist) -- incl top new_candidates to "
              "rotate toward (NOT held; with size_... not given here, just rank/posture).\n"
              "(7) 待验证. Never output buy/sell orders. Do not use prior knowledge for prices.\n\n"
              "DATA(JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)[:95000])
    body = llm.run(prompt, model=CFG["models"]["biweekly"], max_tokens=4600)
    exit_rows = _exit_tracking(quotes)                                    # B50
    return (body + _attribution_md(attribution) + _exit_track_md(exit_rows) + _audit_md(audit_rows, audit_tot)
            + _adherence_md() + _candidates_md(candidates, subs))   # 体检+记分板+雷达 code-rendered

def main():
    if not _is_review_week() and os.getenv("FORCE_RUN", "false").lower() != "true":
        print("non-review week, skip."); return
    try:
        body = build()
    except Exception as e:
        body = "system degraded: biweekly review error (%s). check data/config." % e
    ok = notify.send("biweekly review %s" % dt.date.today().isoformat(), body)
    print(body)
    # B59 (2026-08-28): a failed send used to be SILENT. notify.send() catches every exception and
    # returns False; both mains discarded the return value, so the job stayed green and the email
    # just never arrived. Real incident: the Google account password was changed 2026-08-26 16:20 UTC,
    # which revokes all app passwords, so EMAIL_PASSWORD in GitHub Secrets went stale. The 08-26 run
    # went green, pushed state, and sent nothing -- and nothing anywhere said so.
    # Exiting non-zero turns it into a RED run, and GitHub's own failure notification does not depend
    # on this Gmail app password, so the alarm still gets through.
    if not ok:
        print("EMAIL SEND FAILED -- check EMAIL_PASSWORD (a Google password change revokes app passwords)")
        sys.exit(1)


if __name__ == "__main__":
    main()
