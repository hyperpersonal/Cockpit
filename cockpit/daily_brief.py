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
    """B5 close-detector. state/last_positions.json now stores {shares, avg, pnl_pct} per
    ticker (B34/B37 data layer; old float-only format still readable). Returns (closed, prev)
    so the caller can also detect opens / averaging-down."""
    p = ROOT / "state" / "last_positions.json"
    prev = {}
    try:
        raw = json.load(open(p, encoding="utf-8")).get("positions", {})
        for t, v in raw.items():
            prev[t] = v if isinstance(v, dict) else {"shares": None, "avg": None, "pnl_pct": v}
    except Exception:
        pass
    cur = {}
    for t, d in positions.items():
        if t in exclude:
            continue
        sh, av, mv = d.get("shares"), d.get("avg_price"), d.get("mv")
        pnl = round((mv / (sh * av) - 1) * 100, 1) if (mv and sh and av) else None
        cur[t] = {"shares": sh, "avg": av, "pnl_pct": pnl}
    closed = [t for t in prev if t not in cur]
    if cur and closed:
        for t in closed:
            last = (prev.get(t) or {}).get("pnl_pct")
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
    return closed, prev
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
         "| 候选 | 子板块 | 评分 | 形态 | vs50 | vs200 | 距高 | 等待价(−20%/50日) | 1%风险示例股数 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for c in (candidates or [])[:10]:
        sz = (c.get("size_1pct_stop8") or {}).get("shares", "-")
        wait = "%s/%s" % (c.get("wait_20pct") or "-", c.get("wait_ma50") or "-")
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            c.get("ticker"), c.get("subtheme"), c.get("score"), c.get("posture"),
            c.get("vs50"), c.get("vs200"), c.get("off_high"), wait, sz))
    if subs:
        lead = ", ".join("%s(%+.0f,%s%s)" % (r["subtheme"], r["rel_vs_spy"], r["lifecycle"],
                         "·过热" if r.get("overheated") else "") for r in subs[:3])
        lag = ", ".join("%s(%+.0f)" % (r["subtheme"], r["rel_vs_spy"]) for r in subs[-2:])
        L += ["", "**板块强弱（相对 SPY）** — 领先: " + lead, "落后: " + lag]
    L.append("> Serenity 14 点/VCP 需人工对基本面+盘面确认；示例股数 = 1%风险、止损设入场−8%、与 $30k 硬顶取 min。")
    return "\n".join(L)

def _opens_and_violations(prev, positions, exclude, setups, mem, today):
    """B34: log NEW positions with entry context to reflection memory.
    B37: flag averaging-down (shares up while position underwater) -- Livermore/L1 rule."""
    opened, violations = [], []
    if not prev:
        return opened, violations
    changed = False
    for t, d in positions.items():
        if t in exclude:
            continue
        sh = d.get("shares"); pv = prev.get(t); st = setups.get(t, {})
        if pv is None:
            opened.append(t)
            mem.add(situation="Opened NEW position %s (posture=%s, vs50=%s, off_high=%s)." % (
                        t, st.get("posture"), st.get("vs50"), st.get("off_high")),
                    lesson=("Entry logged for %s: posture was %s. If extended/wait-pullback, this was a "
                            "chase entry (L2 risk) -- review at exit vs the rule." % (t, st.get("posture"))),
                    source="auto: position-open detector (B34)", tags=["entry", t])
            changed = True
        else:
            psh, ppnl = pv.get("shares"), pv.get("pnl_pct")
            if psh and sh and sh > psh * 1.02 and ppnl is not None and ppnl < 0:
                violations.append("%s：亏损中加仓（%.4g→%.4g 股，加仓前浮亏 %.1f%%）" % (t, psh, sh, ppnl))
                mem.add(situation="AVERAGED DOWN on %s while underwater (%.1f%%): %s -> %s shares." % (
                            t, ppnl, psh, sh),
                        lesson="L1/Livermore violation recorded: added to a losing position. Compare outcome at exit vs the rule.",
                        source="auto: averaging-down detector (B37)", tags=["L1", "violation", t])
                changed = True
    if changed:
        try: mem.save()
        except Exception: pass
    return opened, violations

def _append_signal_log(today, net_liq, heat_pct, snapshot, caps):
    """B29 data layer: append today's per-holding shares/price/signals to
    state/signal_history.json so the biweekly adherence scoreboard can compare
    signal -> user action -> outcome. Bounded to 250 days. Fail-open."""
    p = ROOT / "state" / "signal_history.json"
    try:
        hist = json.load(open(p, encoding="utf-8")).get("days", [])
    except Exception:
        hist = []
    entries = {}
    for t, d in snapshot.items():
        c = caps.get(t, {})
        act = str(c.get("action", ""))
        sig = "broken" if d.get("already_broken_down") else None
        trim_usd = None
        if act.startswith("TRIM"):
            sig = (sig + "+trim") if sig else "trim"
            try: trim_usd = float(act.replace("TRIM $", "").replace(",", ""))
            except Exception: pass
        entries[t] = {"shares": d.get("shares"), "price": d.get("price"),
                      "signal": sig, "trim_usd": trim_usd}
    hist = [h for h in hist if h.get("date") != today] + [
        {"date": today, "net_liq": net_liq, "heat_pct": heat_pct, "holdings": entries}]
    try:
        json.dump({"days": hist[-250:]}, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass

def _lamps_md(heat_pct, cash, earn):
    """B42 status lamps: heat / cash-margin / earnings window. One glance, code-rendered."""
    heat_flag = "🔴" if (heat_pct or 0) >= 6 else "🟢"
    cash_flag = "🔴 保证金使用中，利息计息" if (cash or 0) < -100 else "🟢 现金"
    L = ["%s 组合热度 %s%%（预算 <6-8%%）｜ %s $%s" % (
        heat_flag, heat_pct if heat_pct is not None else "?", cash_flag, format(round(cash or 0), ","))]
    soon = []
    today_d = dt.date.today()
    for tk, e in (earn or {}).items():
        d = str(e.get("date", ""))[:10]
        try:
            dd = (dt.date.fromisoformat(d) - today_d).days
            if 0 <= dd <= 14:
                soon.append((dd, tk, d))
        except Exception:
            pass
    if soon:
        soon.sort()
        L.append("📅 财报窗口（≤14天）：" + "、".join("%s %s(%d天)" % (tk, d[5:], dd) for dd, tk, d in soon))
    return "\n".join(L) + "\n\n"

def _exceptions(snapshot, caps, earn):
    """B42 exception engine: a holding appears ONLY if broken / over-cap / near-stop /
    earnings<=14d / dilution-flagged. Healthy names collapse. Returns (md, plain_list)."""
    today_d = dt.date.today()
    exc, exc_plain, ok = [], [], []
    for tk, d in sorted(snapshot.items(), key=lambda kv: -(kv[1].get("market_value") or 0)):
        reasons = []
        act = str((caps.get(tk) or {}).get("action", ""))
        if act.startswith("TRIM"):
            reasons.append("超上限→减 " + act.replace("TRIM ", ""))
        if d.get("already_broken_down"):
            reasons.append("已破位（无有效止损位）")
        elif d.get("dist_to_stop_pct") is not None and d["dist_to_stop_pct"] < 5:
            reasons.append("距止损仅 %s%%（$%s）" % (d["dist_to_stop_pct"], d.get("stop_review_level")))
        ed = str(((earn or {}).get(tk) or {}).get("date", ""))[:10]
        try:
            dd = (dt.date.fromisoformat(ed) - today_d).days
            if 0 <= dd <= 14:
                reasons.append("财报 %s（%d天）" % (ed[5:], dd))
        except Exception:
            pass
        if d.get("dilution_flag"):
            reasons.append("稀释旗标（按 Serenity#7 核 EDGAR）")
        if reasons:
            line = "**%s** $%s（%s%%净值，盈亏 %s%%）：%s" % (
                tk, format(int(d.get("market_value") or 0), ","), d.get("pct_of_net_liq"),
                d.get("unreal_pnl_pct"), "；".join(reasons))
            exc.append("- 🟠 " + line)
            exc_plain.append(line.replace("**", ""))
        else:
            chg = d.get("day_chg_pct")
            ok.append("%s %s%%" % (tk, round(chg, 1) if isinstance(chg, (int, float)) else "-"))
    L = ["## ⚠️ 例外区（仅列状态异常/临界，B42）", ""]
    L += exc if exc else ["- 今日无例外。"]
    if ok:
        L += ["", "其余 %d 票正常：%s（明细见附录）" % (len(ok), "、".join(ok))]
    return "\n".join(L) + "\n\n---\n\n", exc_plain

def _snapshot_md(snapshot, net_liq, cash):
    """B42 appendix: compact 6-column snapshot (audit layer, not reading layer)."""
    L = ["## 📋 附录 · 组合快照（紧凑版）", "",
         "| 持仓 | 股数 | 市值$ | 盈亏% | 占净值% | 距止损% |", "|---|---|---|---|---|---|"]
    for tk, d in sorted(snapshot.items(), key=lambda kv: -(kv[1].get("market_value") or 0)):
        stop = "破位" if d.get("already_broken_down") else (
            d.get("dist_to_stop_pct") if d.get("dist_to_stop_pct") is not None else "-")
        sh = d.get("shares")
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            tk, round(sh, 2) if isinstance(sh, (int, float)) else "-",
            format(int(d.get("market_value") or 0), ","),
            d.get("unreal_pnl_pct"), d.get("pct_of_net_liq"), stop))
    L += ["", "净值 $%s ｜ 现金 $%s" % (format(int(net_liq), ","), format(round(cash or 0), ","))]
    return "\n".join(L) + "\n"

_MKT_ZONE_CN = {"high": ("高位/拥挤", "QQQ 定投：本月正常 1x，不加码；D1 回调子弹留好别动"),
                "neutral": ("中性", "QQQ 定投：本月正常 1x"),
                "deep_pullback": ("深度回调/恐慌", "QQQ 定投：符合 D1 子弹条件——核对 −10%/−15% GTC 挂单在位，可评估当月加投")}

def _action_plan(snapshot, caps, heat_pct, candidates, cash, hard_cap_usd, violations=None, mkt=None):
    """B32: deterministic TODAY-action list, code-rendered at the TOP of the email.
    Order: risk-off first (over-cap TRIM / broken-down), then a heat gate (>=6% -> no new buys),
    then at most 2 buyable-on-support candidates with 1%-risk sizing. Explicit "do nothing"
    when no rule fires. Prompts only -- the user executes manually (red line)."""
    L = ["## ✅ 今日行动清单（规则直出 · 提示非指令 · 手动执行）", ""]
    if mkt:
        zone_cn, hint = _MKT_ZONE_CN.get(mkt.get("zone"), ("?", ""))
        L.append("📍 **市场位置 %s/100（%s）** · %s（B39）" % (mkt.get("score"), zone_cn, hint))
        L.append("　↳ QQQ 距52周高 %s%% · vs200日 %s%% · VIX %s；规则透明：%s（估值分位待接入）" % (
            mkt.get("off_high_pct"), mkt.get("vs200_pct"), mkt.get("vix"), mkt.get("rule")))
        L.append("")
    if violations:
        L += ["**⛔ 纪律违规检测（上一交易日，B37）：**"] + [
            "- ⛔ " + v + "（规则=利弗莫尔/L1：只在浮盈中加仓，绝不亏损补仓）" for v in violations] + [""]
    sells = []
    for tkr, d in sorted(snapshot.items()):
        c = caps.get(tkr, {})
        act = str(c.get("action", ""))
        if act.startswith("TRIM"):
            over_hard = (c.get("current_usd") or 0) > hard_cap_usd
            why = "超风控上限" + ("+超$%dk硬顶" % int(hard_cap_usd / 1000) if over_hard else "")
            sells.append("- 🔴 **%s：减仓 %s**（%s；规则=vol×corr 动态上限）" % (tkr, act.replace("TRIM ", ""), why))
        if d.get("already_broken_down"):
            sells.append("- 🔴 **%s：已破位（无有效止损位）→ 按纪律评估减仓/清仓**（规则=破位纪律/L1）" % tkr)
    if sells:
        L += ["**先卖/减（风险优先）：**"] + sells + [""]
    if heat_pct is not None and heat_pct >= 6.0:
        L.append("**买入：今天不开新仓** —— 组合热度 %.1f%% 已达预算(6-8%%)上限；先执行减仓释放风险预算（规则=热度闸门）。" % heat_pct)
    else:
        buys = []
        for c in (candidates or []):
            if c.get("posture") != "buyable-on-support":
                continue
            sz = c.get("size_1pct_stop8") or {}
            if not sz.get("shares"):
                continue
            buys.append("- 🟢 **%s**（%s·评分%s）：示例 %s 股 ≈ $%s，止损=入场−8%%（规则=支撑区+1%%风险；买前人工核 Serenity14/VCP/稀释）"
                        % (c.get("ticker"), c.get("subtheme"), c.get("score"), sz.get("shares"), sz.get("position_value")))
            if len(buys) >= 2:
                break
        if buys:
            L += ["**可考虑买（形态在支撑区，至多两名）：**"] + buys
            if (cash or 0) < 2000:
                L.append("- ⚠️ 现金仅 $%.0f：任何买入以先完成上面的卖出为前提。" % (cash or 0))
        elif not sells:
            L.append("**今天不动**：无超限、无破位、无支撑区候选（规则=不追 extended）。")
        else:
            L.append("**买入：暂无支撑区候选**（规则=不追 extended）。")
    waits = [c for c in (candidates or []) if c.get("posture") == "extended/wait-pullback" and c.get("wait_20pct")][:2]
    if waits:
        L.append("")
        L.append("**等回调候选（到价再谈，B38）：**")
        for c in waits:
            L.append("- ⏳ **%s**：等 $%s（52周高−20%%）/ $%s（−25%%）/ 回踩50日线 $%s" % (
                c.get("ticker"), c.get("wait_20pct"), c.get("wait_25pct"), c.get("wait_ma50")))
    L += ["", "---", ""]
    return "\n".join(L)

def build() -> str:
    today = dt.date.today().isoformat()
    phase = calendars.market_phase()
    exclude = set(CFG.get("exclude", []))
    cfg_holdings = [h["ticker"] for h in CFG.get("holdings", [])]
    theme_of = _theme_of()
    bench = CFG.get("benchmark", "SPY")

    mem = ReflectionMemory(str(ROOT / "state" / "reflection_memory.json"))
    port = ibkr.get_portfolio()
    closed = []; as_of = None; drift_extra = []; drift_gone = []; prev_pos = {}
    if port:
        net_liq = port["net_liq"]; cash = port["cash"]; positions = port["positions"]; as_of = port.get("as_of")
        # B33: the ACTIVE BOOK is IBKR-DRIVEN -- every live Flex position (minus exclude) is
        # tracked; config.holdings is role annotation + offline fallback only.
        holdings = sorted({t for t in positions if t not in exclude})
        cur_mv = {t: p["mv"] for t, p in positions.items() if t not in exclude}
        port_note = ""
        _append_nav(today, net_liq)
        closed, prev_pos = _reflect_on_closes(positions, exclude, mem, today)
        drift_extra = sorted(set(holdings) - set(cfg_holdings))   # held, not yet annotated in config
        drift_gone = sorted(set(cfg_holdings) - set(holdings))    # stale config entries (can delete)
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        positions = {}; cur_mv = {}
        holdings = cfg_holdings                                   # fail-open: IBKR down -> config list
        port_note = "IBKR offline: shares/cost/P&L unknown (data gap); caps shown as room-from-flat."
    quotes = screener.quote_map(sorted(set(_universe()) | set(holdings) | {"^VIX"}))
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
    dil_on = CFG.get("risk", {}).get("dilution_atm_disqualifier", True)
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution, dil_on)
    heat_usd = sum((d["market_value"] or 0) * (d["dist_to_stop_pct"] or 0) / 100.0
                   for d in holdings_snapshot.values())
    portfolio_heat_pct = round(heat_usd / net_liq * 100, 1) if net_liq else None
    broken = [t for t, d in holdings_snapshot.items() if d["already_broken_down"]]
    opened, violations = _opens_and_violations(prev_pos, positions, exclude, setups, mem, today)
    _append_signal_log(today, net_liq, portfolio_heat_pct, holdings_snapshot, caps)

    xval = {t: crossval.verify_price(t, quotes[t]["price"]) for t in holdings if t in quotes}
    edgar = {t: crossval.edgar_dossier(t) for t in holdings}    # B17: SEC EDGAR deep check
    maxage = CFG.get("data", {}).get("news_max_age_days", 3)
    cutoff = (dt.date.today() - dt.timedelta(days=maxage)).isoformat()
    def _recent(n):
        d = str(n.get("publishedDate") or n.get("date") or "")[:10]
        return (not d) or d >= cutoff
    news = [n for n in fmp.stock_news(holdings, limit=25) if _recent(n)][:8]
    earn = {t: fmp.upcoming_earnings(t, today) for t in holdings}
    earn = {t: e for t, e in earn.items() if e}
    subs = screener.subtheme_strength(CFG["subthemes"], quotes, bench_vs200)
    candidates = screener.rank_candidates(CFG["subthemes"], quotes, bench_vs200,
                                          set(holdings) | exclude, top=10)
    maxpos_pct = round(hard_cap_usd / net_liq * 100, 1) if net_liq else None
    for c in candidates:
        q = quotes.get(c["ticker"], {}); px = q.get("price")
        c["size_1pct_stop8"] = risk.position_size(net_liq, px, px * 0.92, 1.0, maxpos_pct) if px else None
    mkt = screener.market_position(quotes)
    action_md = _action_plan(holdings_snapshot, caps, portfolio_heat_pct, candidates, cash, hard_cap_usd,
                             violations=violations, mkt=mkt)

    weak = [t for t, s in setups.items() if not s["stage2"]]
    situation = "Holdings " + ",".join(holdings) + "; weak/below-MA: %s; phase %s" % (weak, phase)
    lessons = mem.retrieve(situation, n=3)

    exc_md, exc_plain = _exceptions(holdings_snapshot, caps, earn)
    bundle = dict(date=today, phase=phase, phase_rule=calendars.PHASE_GUARDRAIL.get(phase, ""),
                  as_of=as_of, port_note=port_note, net_liq=net_liq, cash=cash,
                  portfolio_heat_pct=portfolio_heat_pct, market_position=mkt,
                  broken_down_holdings=broken, closed_positions=closed, opened_positions=opened,
                  discipline_violations=violations, exceptions=exc_plain,
                  cross_validation=xval, edgar=edgar, news=news, lessons=lessons)
    if phase == "non_trading":
        return "[%s] US market closed; no brief today." % today

    prompt = ("Write a SHORT Chinese addendum. 3 sections ONLY, no tables, never restate numbers "
              "already rendered elsewhere (action list/exceptions/appendix are code-rendered):\n"
              "(1) 重大消息 -- from news: ONLY material events (guidance change, M&A, regulatory action, "
              "earnings surprise, major product/customer win-loss). Max 3 bullets, one line each, "
              "source+date. If none qualify write exactly: 无重大消息。\n"
              "(2) 异常点评 -- 2-4 sentences on exceptions/discipline_violations/market_position only; "
              "no per-holding tour.\n"
              "(3) 待验证 -- numbers lacking cross-validation (cross_validation mismatches, "
              "edgar.available=false, dilution filings needing manual SEC check). Max 5 bullets.\n"
              "Obey phase_rule. Never output buy/sell orders. Do not use prior knowledge for prices.\n\n"
              "DATA(JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)[:60000])
    body = llm.run(prompt, model=CFG["models"]["daily"], max_tokens=1800)
    header = ("> ⏱️ 持仓数据截至 %s（IBKR Flex 上一交易日；**当日交易可能未反映**——如需当日，Flex 周期改 Today）。\n\n"
              % as_of) if as_of else ""
    drift = ""
    if drift_extra or drift_gone:
        drift = ("> 🟡 **config 注释提醒（B33）**：持仓名单已由 IBKR 实时驱动，跟踪不受影响——"
                 + ("新持仓待补注释/子板块归属: **" + ", ".join(drift_extra) + "**；" if drift_extra else "")
                 + ("config 中已不再持有(可删): " + ", ".join(drift_gone) + "。" if drift_gone else "")
                 + "\n\n")
    title = "# 美股投研日报 — %s%s\n\n" % (today, "（盘中快照：未收盘，勿当收盘复盘）" if phase == "intraday" else "")
    lamps = _lamps_md(portfolio_heat_pct, cash, earn)
    return (header + title + drift + action_md + lamps + exc_md
            + "## 📰 消息与点评（LLM 附录）\n\n" + body + "\n\n---\n\n"
            + _snapshot_md(holdings_snapshot, net_liq, cash)
            + _candidates_md(candidates, subs))   # B42: 决策/例外/附录全代码直出，LLM 只写消息+点评+待验证

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
