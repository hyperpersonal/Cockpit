"""Daily brief orchestrator. Runs ~US midday (China 00:00, Tue-Sat). Fail-open everywhere.
Flow: trading-day gate -> IBKR portfolio (or labeled gap) -> detect closed positions & auto-log a
reflection lesson (B5) -> FMP universe quotes/news(age-filtered)/earnings -> sub-theme RS + breadth
-> ranked candidates -> EWMA vol x correlation (same-theme floored) caps w/ $30k hard ceiling ->
per-holding REAL stop + portfolio heat -> append NAV history -> Claude (sections 1-8) -> CODE-render
the 选股雷达 candidate table + as-of label and append (so it's NEVER dropped by the LLM). Chinese."""
from __future__ import annotations
import os, sys, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, risk, screener, scanner, crossval, llm, notify, calendars
from .memory import ReflectionMemory
from .ledger import performance as ledger
from .domain import policy
from .rules import account as r_account, thesis as r_thesis, \
                   concentration as r_conc, sizing as r_sizing, \
                   exit as r_exit, profit as r_profit
from .engine.resolve import total_sell_value
from .engine import pipeline
from .render import action_list

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

def _theme_of() -> dict:
    """Ticker -> layer for EVERY name the system knows, risk.theme_overrides included.

    R5 (2026-08-28): this map is what `risk.position_caps()` and `_corr_universe()` receive.
    It used to be built from config.subthemes ONLY, so all seven theme_overrides names were
    themeless to the risk engine -- SKHY (31% of NAV) and MU (24.5%) are both memory_hbm and
    the >=0.60 same-theme correlation floor never applied to the most concentrated pair in
    the book. Overrides win, exactly as in cockpit.rules.concentration.layer_of.
    """
    out = {}
    for name, v in CFG.get("subthemes", {}).items():
        for s in v.get("names", []):
            out.setdefault(s, name)
    for t, layer in ((CFG.get("risk", {}) or {}).get("theme_overrides", {}) or {}).items():
        if layer:
            out[t] = layer                    # override wins over any subthemes membership
    return out

def _universe() -> list:
    syms = set([CFG.get("benchmark", "SPY")])
    for v in CFG.get("subthemes", {}).values():
        syms |= set(v.get("etfs", [])) | set(v.get("names", []))
    syms |= {h["ticker"] for h in CFG.get("holdings", [])}
    # B50 (2026-08-27): exited names must stay in the quote universe or post-exit tracking silently
    # drops them. SPCX belongs to no subtheme and is no longer a holding, so without this the one
    # row B50 most needs (a real exit) would just vanish -- the Rule 6 failure mode again.
    try:
        syms |= set(_reentry_load().keys())
    except Exception:
        pass
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

def _append_nav(date_str, net_liq, run_date=None):
    """R3 (2026-08-28): file NAV under the Flex `as_of`, not the run date.

    The brief titled 2026-08-28 carried portfolio data stamped as_of=20260827 and wrote
    159,528.31 -- the broker's 08-27 net liquidation value -- into nav_history under
    "2026-08-28". nav_history has no 2026-08-27 entry at all, so every window computed from
    it is shifted by a trading day. Delegates to the ledger so there is one implementation.
    """
    try:
        ledger.append_nav_at_as_of(date_str, net_liq, run_date=run_date)
    except Exception:
        pass

# B60 (2026-08-28): ONE-SHOT content must not be consumed by a brief that was never DELIVERED.
# Incident: a 06:14 UTC run on 2026-08-28 detected the NVDA/AVGO exits, wrote last_positions.json and
# stamped reentry_watch["NVDA"]["prompted"] -- then its email silently failed (stale app password).
# The 06:55 run compared against the ALREADY-updated snapshot, saw no closes, and the exit postmortem
# plus the NVDA re-entry prompt were gone for good. B59 (exit 1 on send failure) and the workflow's
# `if: always()` Persist step make this WORSE rather than better: the run turns red but the state that
# consumed the content is committed anyway. So every write that CONSUMES a one-shot signal is deferred
# and flushed only after notify.send() confirms delivery. Measurement-only writes (nav_history,
# signal_history, scanner_state) are deliberately NOT deferred -- they must record every session.
_PENDING_WRITES = []

def _defer(fn):
    """Queue a state write that may happen ONLY if the brief is actually delivered."""
    _PENDING_WRITES.append(fn)

def flush_pending_writes():
    """B60: called only after notify.send() returns True."""
    for fn in _PENDING_WRITES:
        try:
            fn()
        except Exception:
            pass
    _PENDING_WRITES.clear()

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
    # B50 (2026-08-27): a >=90% quantity cut is an EXIT for measurement purposes even though a stub
    # remains. 2026-08-26 MRVL went 54.741 -> 0.741 shares and was invisible to every exit-side rule:
    # not "closed", so no postmortem, no re-entry watch, no exit tracking. The system could see the
    # position shrink and had nothing to say about it.
    trimmed_out = []
    for t, pv in prev.items():
        c = cur.get(t)
        if not c:
            continue
        ps, cs = pv.get("shares"), c.get("shares")
        if ps and cs is not None and cs <= ps * 0.10:
            trimmed_out.append(t)
    if cur and trimmed_out:
        for t in trimmed_out:
            mem.add(situation="Near-exit %s: position cut to <=10%% of prior size." % t,
                    lesson=("%s was reduced by >=90%% but a stub remains. Treat as an exit for review "
                            "purposes: did the exit follow the thesis, and how did it do afterwards?" % t),
                    source="auto: near-exit detector (B50)", tags=["postmortem", "exit", "partial", t])
    if cur and closed:
        for t in closed:
            last = (prev.get(t) or {}).get("pnl_pct")
            mem.add(situation="Closed/exited position %s (last unrealized %s%%)." % (t, last),
                    lesson=("Position %s left the book at ~%s%%. Review: did the exit follow the thesis "
                            "and stop discipline? Record realized outcome and what to repeat/avoid." % (t, last)),
                    source="auto: position-close detector", tags=["postmortem", "exit", t])
        _defer(mem.save)                                     # B60: memory.add has no dedupe
    # B60: writing this snapshot is what CONSUMES the close/near-exit detection -- once `cur` is on
    # disk the next run sees no closes. Defer it: an undelivered brief must leave the detector armed.
    _defer(lambda _p=p, _t=today, _c=dict(cur): json.dump(
        {"date": _t, "positions": _c}, open(_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2))
    return closed, trimmed_out, prev
def _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution, dilution_on=True, closes=None):
    """B20: market value & P&L use the CURRENT FMP price x IBKR shares (not the stale Flex price).
    B48 (2026-08-23, user decision): the EXECUTION stop is the 20-day closing low x0.99 -- a level
    that survives normal volatility, unlike max(200DMA, cost*0.8) which is either far below or an
    anchored pseudo-stop. The 200DMA keeps its own life as a TREND flag (`below_200dma`) so the
    'trend is broken' signal is not lost. Falls back to the old rule when history is missing."""
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
        hist = (closes or {}).get(t) or []                       # B48: FMP light rows are NEWEST-first
        low20 = min(hist[:20]) if len(hist) >= 5 else None
        if low20 and price:
            stop_level = round(low20 * 0.99, 2)                  # the level to PLACE a GTC stop at
            # B58 (2026-08-27): the BREACH test must use the stop a GTC order would ALREADY be
            # sitting at -- i.e. derived from the PRIOR 20 closes. Testing today's price against a
            # window that CONTAINS today's price is unsatisfiable: min <= price always, so
            # `price <= min*0.99` can never be true. `already_broken_down` was therefore DEAD from
            # B48 (2026-08-23) until this fix, and with it every downstream 破位 signal
            # (exception list, action plan, snapshot label, B29 signal history).
            # B51's replay measured it: 82 real breaches across 11 holdings in one year, 0 detected.
            prior20 = min(hist[1:21]) if len(hist) >= 6 else low20
            already_broken = price <= round(prior20 * 0.99, 2)
        else:                                                    # fail-open: pre-B48 rule
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
                   "stop_basis": ("20日低" if low20 else "200日线/成本×0.8(史缺)"),
                   "below_200dma": bool(price and a200 and price < a200),
                   "risk_usd": round((mv or 0) * (price - stop_level) / price) if (mv and price and stop_level and price > stop_level) else None,
                   "dilution_yoy_pct": round(dil * 100, 1) if dil is not None else None,
                   "dilution_flag": bool(dilution_on and dil is not None and dil > 0.05)}
    return snap

def _candidates_md(candidates, subs):
    """Deterministic 选股雷达 table appended to the email so the LLM can never drop it."""
    L = ["", "---", "## 📡 选股雷达 / 观察池（系统直出 · 未持有 · 不经 LLM，保证显示）", "",
         "| 候选 | 子板块 | 评分 | 形态 | vs50 | vs200 | 距高 | 等待价(−20%/50日) |",
         "|---|---|---|---|---|---|---|---|"]
    for c in (candidates or [])[:10]:
        wait = "%s/%s" % (c.get("wait_20pct") or "-", c.get("wait_ma50") or "-")
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            c.get("ticker"), c.get("subtheme"), c.get("score"), c.get("posture"),
            c.get("vs50"), c.get("vs200"), c.get("off_high"), wait))
    if subs:
        lead = ", ".join("%s(%+.0f,%s%s)" % (r["subtheme"], r["rel_vs_spy"], r["lifecycle"],
                         "·过热" if r.get("overheated") else "") for r in subs[:3])
        lag = ", ".join("%s(%+.0f)" % (r["subtheme"], r["rel_vs_spy"]) for r in subs[-2:])
        L += ["", "**板块强弱（相对 SPY）** — 领先: " + lead, "落后: " + lag]
    L.append("> 本表是**观察池**：评分、形态与等待价，**不含任何可执行数字**。"
             "股数、金额、目标仓位与止损只出现在首屏行动清单里，且只来自统一裁决（Decision）。"
             "Serenity 14 点 / VCP / 稀释仍需人工核实；未核实的候选只会以「观察」出现。")
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
        _defer(mem.save)                                     # B60: same one-shot `prev` snapshot
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

def _theme_exposure(snapshot, theme_of, net_liq):
    """B36: aggregate sub-theme exposure vs net liq. Leveraged ETFs count at their leverage
    factor (config risk.leverage_factors + risk.theme_overrides), so a 2x DRAM ETF adds 2x its
    MV to memory_hbm (red-line v2 clause 6). Returns (alerts_over_threshold, full_map)."""
    rk = CFG.get("risk", {}) or {}
    overrides = rk.get("theme_overrides", {}) or {}
    lev = rk.get("leverage_factors", {}) or {}
    thr = float(rk.get("theme_exposure_alert_pct", 40))
    agg = {}
    for t, d in (snapshot or {}).items():
        mv = d.get("market_value") or 0
        th = overrides.get(t) or theme_of.get(t) or "unmapped"
        agg[th] = agg.get(th, 0) + mv * float(lev.get(t, 1.0))
    alerts = []
    if net_liq:
        for th, usd in sorted(agg.items(), key=lambda kv: -kv[1]):
            pct = round(usd / net_liq * 100, 1)
            if pct >= thr and th != "unmapped":
                alerts.append({"subtheme": th, "usd": round(usd), "pct": pct, "threshold": thr})
    return alerts, agg

def _reentry_load():
    try:
        return json.load(open(ROOT / "state" / "reentry_watch.json", encoding="utf-8")).get("watch", {})
    except Exception:
        return {}

def _reentry_dump(watch):
    try:
        json.dump({"watch": watch}, open(ROOT / "state" / "reentry_watch.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass

def _reentry_update(closed, quotes, today, net_liq, maxpos_pct, trimmed=None):
    """B44 re-entry watch: every fully-closed position enters a watch list; when it later CLOSES
    back above its 50DMA the action plan proposes a rule-based re-entry with fresh 1%-risk
    sizing. Turns stop-out whipsaw into a bounded, rule-governed round trip (prompt once,
    expire after risk.reentry_watch_days). Fail-open; state committed by the workflow."""
    watch = _reentry_load()
    rk = CFG.get("risk", {}) or {}
    days = int(rk.get("reentry_watch_days", 90))                 # B44: how long we still PROMPT
    track_days = int(rk.get("exit_track_days", 365))             # B50: how long we still MEASURE
    for t in (closed or []):
        if t and t not in watch:
            watch[t] = {"exit_date": today, "exit_price": (quotes.get(t) or {}).get("price"),
                        "kind": "full", "prompted": None}
    for t in (trimmed or []):                                    # B50: >=90% cut, stub remains
        if t and t not in watch:
            watch[t] = {"exit_date": today, "exit_price": (quotes.get(t) or {}).get("price"),
                        "kind": "partial", "prompted": "n/a"}    # no re-entry prompt: still holding
    cutoff = (dt.date.today() - dt.timedelta(days=track_days)).isoformat()
    watch = {t: w for t, w in watch.items() if str(w.get("exit_date") or today) >= cutoff}
    prompt_cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    prompts, stamp_after_send = [], []
    for t, w in watch.items():
        if w.get("prompted") or w.get("kind") == "partial":
            continue
        if str(w.get("exit_date") or today) < prompt_cutoff:      # past the prompt window, keep measuring
            continue
        q = quotes.get(t) or {}
        px, a50 = q.get("price"), q.get("priceAvg50")
        if px and a50 and px > a50:
            sz = risk.position_size(net_liq, px, px * 0.92, 1.0, maxpos_pct) or {}
            prompts.append({"ticker": t, "price": px, "ma50": round(a50, 2),
                            "exit_date": w.get("exit_date"),
                            "shares": sz.get("shares"), "value": sz.get("position_value")})
            stamp_after_send.append(t)          # B60: a prompt fires ONCE -- spend it on delivery only
    _reentry_dump(watch)                        # new entries + pruning are measurement: persist now
    if stamp_after_send:
        def _stamp(_ts=tuple(stamp_after_send), _d=today):
            w2 = _reentry_load()
            for t in _ts:
                if t in w2:
                    w2[t]["prompted"] = _d
            _reentry_dump(w2)
        _defer(_stamp)
    return prompts

def _exit_tracking(quotes):
    """B50 post-exit tracking: measure how every exited name did AFTER we left it.

    The system logged closes (B5) and offered re-entries (B44) but never measured whether the exit
    itself was right. That is a structurally one-sided book: B29 only prices "ignored a sell signal",
    never "sold too early", so the system could only ever say 你该卖 and never 你卖早了.
    Reads the same ledger B44 writes; reports both directions with equal weight."""
    watch = _reentry_load()
    rows = []
    for t, w in sorted(watch.items()):
        ex = w.get("exit_price")
        px = (quotes.get(t) or {}).get("price")
        if not (ex and px):
            continue
        try:
            d0 = dt.date.fromisoformat(str(w.get("exit_date")))
            days = (dt.date.today() - d0).days
        except Exception:
            days = None
        rows.append({"ticker": t, "exit_date": w.get("exit_date"), "kind": w.get("kind") or "full",
                     "exit_price": ex, "price": px, "since_pct": round((px / ex - 1) * 100, 1),
                     "days": days})
    return sorted(rows, key=lambda r: -(r["since_pct"] or 0))

def _exit_track_md(rows):
    """B50 rendering. Code-rendered; both directions reported, no softening either way."""
    if not rows:
        return ""
    L = ["## 🔭 离场后跟踪（B50 · 卖对了还是卖早了）", "",
         "| 票 | 离场日 | 类型 | 离场价 | 现价 | 离场后 | 天数 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        mark = "📈 卖早了" if r["since_pct"] > 0 else ("📉 卖对了" if r["since_pct"] < 0 else "—")
        L.append("| %s | %s | %s | %.2f | %.2f | **%+.1f%%** %s | %s |" % (
            r["ticker"], r["exit_date"], {"partial": "近乎清仓", "backfill": "回填⚠️"}.get(r["kind"], "清仓"),
            r["exit_price"], r["price"], r["since_pct"], mark, r["days"] if r["days"] is not None else "-"))
    up = [r for r in rows if r["since_pct"] > 0]
    dn = [r for r in rows if r["since_pct"] < 0]
    med = sorted(r["since_pct"] for r in rows)[len(rows) // 2]
    L += ["", "**离场 %d 次：卖早了 %d 次 / 卖对了 %d 次，离场后涨跌中位数 %+.1f%%**" % (
        len(rows), len(up), len(dn), med),
        "> 与 B29 记分板配对使用：B29 算「无视卖出信号的代价」，本表算「卖出本身的代价」。"
        "**只看 B29 会让系统结构性偏向卖出。**",
        "> ⚠️ 标「回填」的行是从 signal_history 补出来的（B44 上线前的离场），**离场价用的是最后一次观察到的快照价、不是成交价**，误差更大。",
        "> 这不是让你别卖——止损的代价本来就是「多数时候卖了会涨」（见 B51：一年 82 次触发，72% 在 20 日内上涨）。"
        "本表的用途是让代价可见、可累计，而不是凭一次卖飞改规则。", ""]
    return "\n".join(L) + "\n---\n\n"

OUTSIDE_LAYER = "outside_framework"   # B53: the ONLY value meaning "really outside the framework"

def _layer_of(t, theme_of):
    """B53: 'unmapped' means THE CONFIG IS MISSING A MAPPING -- a data gap, NOT a verdict on the
    holding. Say 'no framework holds this' with OUTSIDE_LAYER (risk.theme_overrides ->
    outside_framework). Conflating the two once put SKHY, the largest position, at the top of a
    liquidation list labelled 体系外."""
    rk = CFG.get("risk", {}) or {}
    return (rk.get("theme_overrides", {}) or {}).get(t) or theme_of.get(t) or "unmapped"

def _position_audit(snapshot, caps, net_liq, theme_of, hard_cap_usd):
    """B48 position audit: for every HOLDING answer the question the risk engine never answered --
    'given a stop you can actually live with, how big should this be?'

    Per name: execution stop (20d low) -> $ at risk -> % of NAV -> risk tier (2% for names with
    proven relative leadership RS >= high_conviction_rs, else 1%) -> target position = min(risk-derived,
    vol x corr cap, $30k hard cap) -> how much to cut. Plus within-layer RS rank so the LAYER'S
    WEAKEST name is named (duplicate holdings in one layer were invisible before). Fail-open."""
    rk = CFG.get("risk", {}) or {}
    hi_rs = float(rk.get("high_conviction_rs", 20))
    pct_hi = float(rk.get("risk_pct_high", 2.0))
    pct_base = float(rk.get("risk_pct_base", 1.0))
    layers = {}
    for t, d in (snapshot or {}).items():
        layers.setdefault(_layer_of(t, theme_of), []).append((t, d.get("rs_vs_spy")))
    rank = {}
    for L, members in layers.items():
        ordered = sorted(members, key=lambda kv: -(kv[1] if kv[1] is not None else -999))
        for i, (t, _rs) in enumerate(ordered, 1):
            rank[t] = (L, i, len(ordered))
    rows, tot_risk, tot_cut = [], 0.0, 0.0
    for t, d in sorted(snapshot.items(), key=lambda kv: -(kv[1].get("market_value") or 0)):
        mv, px, stop = d.get("market_value"), d.get("price"), d.get("stop_review_level")
        rs = d.get("rs_vs_spy")
        L, i, n = rank.get(t, ("unmapped", 1, 1))
        tier = pct_hi if (rs is not None and rs >= hi_rs) else pct_base
        risk_usd = d.get("risk_usd")
        target = None
        if px and stop and px > stop:
            per_pct = (px - stop) / px
            target = min(net_liq * tier / 100.0 / per_pct, hard_cap_usd)
            cap_usd = (caps.get(t) or {}).get("cap_usd")
            if cap_usd:
                target = min(target, cap_usd)
            # SAFETY (smoke-test finding 2026-08-23): a stop sitting 1-4% away yields a huge
            # risk-derived size. That is arithmetically true but reads as 'you may add' -- and a
            # name whose stop is about to trigger is the last one to size UP. This table answers
            # 'cut how much', never 'buy how much'; adding is governed by the action plan alone.
            target = min(target, mv or target)
        cut = max((mv or 0) - target, 0) if target is not None else None
        tot_risk += risk_usd or 0
        tot_cut += cut or 0
        rows.append({"ticker": t, "layer": L, "rank": "%d/%d" % (i, n),
                     "near_stop": bool(d.get("dist_to_stop_pct") is not None and d["dist_to_stop_pct"] < 5),
                     "weakest": bool(n >= 2 and i == n), "mv": mv, "pct_nav": d.get("pct_of_net_liq"),
                     "rs": rs, "stop": stop, "stop_basis": d.get("stop_basis"),
                     "dist_pct": d.get("dist_to_stop_pct"), "risk_usd": risk_usd,
                     "risk_pct_nav": round((risk_usd or 0) / net_liq * 100, 2) if net_liq else None,
                     "tier_pct": tier, "target_usd": round(target) if target else None,
                     "cut_usd": round(cut) if cut else None,
                     "below_200dma": d.get("below_200dma")})
    return rows, {"total_risk_usd": round(tot_risk), "total_cut_usd": round(tot_cut),
                  "total_risk_pct_nav": round(tot_risk / net_liq * 100, 1) if net_liq else None}

def _audit_md(rows, tot, budget_hi=8.0, sell_total=None):
    """B48 table, R10 (2026-08-28): DIAGNOSTIC ONLY.

    The 目标仓位$ and 该减$ columns are gone. They were a second, independently computed
    answer to "how much do I sell", and on 2026-08-28 they disagreed with the action list
    on SKHY (40,154 vs 35,911) and ORCL (14,630 vs 9,056) inside one email. The appendix
    may explain the arithmetic; it may not produce a rival instruction. The one number to
    sell is the action list's, restated here verbatim so the reader can check they match."""
    if not rows:
        return ""
    L = ["## 🩺 持仓体检（诊断表 · 不产生交易金额）", "",
         "| 票 | 层(排名) | 现仓$ | 净值% | RS | 止损价 | 距止损% | 在险$ | 在险%净值 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        flags = ("🔻" if r["weakest"] else "") + ("📉" if r["below_200dma"] else "") + ("🚨" if r.get("near_stop") else "")
        L.append("| %s%s | %s(%s) | %s | %s | %s | %s | %s | %s | %s%% |" % (
            r["ticker"], flags, r["layer"], r["rank"],
            format(int(r["mv"] or 0), ","), r["pct_nav"], r["rs"],
            r["stop"] if r["stop"] else "-", r["dist_pct"],
            format(int(r["risk_usd"]), ",") if r["risk_usd"] else "-",
            r["risk_pct_nav"]))
    flag = "🔴" if (tot["total_risk_pct_nav"] or 0) > budget_hi else "🟢"
    L += ["", "%s **组合总在险 $%s = 净值 %.1f%%**（预算 6-8%%）——**警示口径，不产生卖出金额**" % (
        flag, format(tot["total_risk_usd"], ","), tot["total_risk_pct_nav"] or 0)]
    if sell_total is not None:
        L.append("本日唯一卖出总额见首屏行动清单：**$%s**（本表不重算）。" % format(int(sell_total), ","))
    L += ["> 🔻=本层 RS 最弱（**复查候选，不是清仓依据**）｜📉=跌破200日线（趋势旗标，与执行止损分开）｜🚨=距止损<5%",
          "> ⚠️ **在险$ 小 ≠ 安全**：止损近在咫尺同样让在险金额变小（见 🚨 行）。两者要一起读。",
          "> **止损位不是问题，仓位才是**：能扛住波动的止损必然离得远，仓位不缩就必然超预算。",
          "> 参考口径（不驱动指令）：按风险档反推的仓位与 vol×corr 上限见每条决策的「参考口径」行。", ""]
    return "\n".join(L) + "\n---\n\n"

# R10 (2026-08-28): _dispose_order() DELETED. It produced its own ordered sell list with its
# own amounts (hardness 1-2 => whole position, 3-5 => cut amount) and on 2026-08-27 data it
# totalled 109,543 while the action plan totalled 72,858 and the audit table 86,442 -- three
# answers, one book. Ordering the sells is now the action list's job, driven by the binding
# rule's tier. The historical output is preserved in tests/fixtures/brief_20260828_observed.json
# so the defect stays demonstrable without keeping the code that caused it.

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

# R6 (2026-08-28): the Schwab side is 100% user-managed and explicitly OUT OF SCOPE here.
# These three strings used to carry monthly dollar-cost-averaging guidance for that other
# account, printed at the top of the IBKR action list -- advice this system has no business
# giving and no data to support. (The exact removed wording is preserved in
# tests/fixtures/brief_20260828_observed.json, not here: quoting it in a comment would defeat
# the source-level guard that keeps it out.) The market-position score stays, because it gates
# IBKR buying; the instruction attached to it is now about THIS account.
_MKT_ZONE_CN = {"high": ("高位/拥挤", "新开仓从严：只接受支撑区形态，不追 extended"),
                "neutral": ("中性", "常规节奏"),
                "deep_pullback": ("深度回调/恐慌", "支撑区候选优先，仍受热度与现金闸门约束")}

# R10 (2026-08-28): _action_plan() DELETED. It rendered sells straight from
# risk.position_caps() actions ("TRIM $X"), which was the THIRD independent sizing path. Its
# job now belongs to cockpit/render/action_list.py, which may only read Decision fields.


def _staleness(as_of_label, today, earn, phase):
    """Say out loud how old the data is. Never dress a lagged snapshot as a live conclusion.

    Two independent lags, and they are NOT the same thing:
      * positions/NAV come from the IBKR Flex statement, whose reportDate can be the previous
        trading day even on a post-close run;
      * prices are FMP end-of-day closes and exclude after-hours entirely, so an earnings move
        that happened after the close is invisible until the next session.
    """
    out = []
    try:
        d0 = dt.date.fromisoformat(ledger.normalize_date(as_of_label))
        d1 = dt.date.fromisoformat(today)
        lag = calendars.trading_days_between(d0, d1) if hasattr(calendars, "trading_days_between") \
            else (d1 - d0).days
    except Exception:
        d0 = d1 = None
        lag = 0
    if lag and lag > 0:
        out.append("持仓数据滞后 %d 个日历日（Flex 期末日 %s，本次运行 %s）——"
                   "股数、成本、净值都是那一天的，**不是实时的**" % (lag, as_of_label, today))
    out.append("价格为 FMP 收盘价，**不含盘后与隔夜**")
    soon = []
    for tk, e in (earn or {}).items():
        ed = str((e or {}).get("date", ""))[:10]
        if not ed or not d0:
            continue
        try:
            edd = dt.date.fromisoformat(ed)
        except Exception:
            continue
        if d0 <= edd <= (d1 or d0):
            soon.append("%s（%s）" % (tk, ed))
    if soon:
        out.append("⚠️ 以下标的在本表数据时点之后发布财报，**盘后波动完全不反映在本表价格里**，"
                   "任何基于价格的判断对它们降级为参考：" + "、".join(soon))
    if phase == "intraday":
        out.append("⚠️ 盘中快照：未收盘，勿当收盘复盘")
    return out


def _decision_inputs(snapshot, closes):
    """Flatten the holdings snapshot into the shape the rule modules expect.

    One place, one vocabulary. Rules never read the snapshot's field names directly, so a
    rename in the snapshot cannot silently change what a rule sees.
    """
    pos, low10 = {}, {}
    for t, d in (snapshot or {}).items():
        mv, px = d.get("market_value"), d.get("price")
        if mv is None and d.get("shares") and px:
            mv = d["shares"] * px
        pos[t] = {"shares": d.get("shares"), "price": px, "market_value": mv,
                  "cost_price": d.get("avg_cost"), "unreal_pnl_pct": d.get("unreal_pnl_pct"),
                  "rs": d.get("rs_vs_spy"), "stop_level": d.get("stop_review_level"),
                  "dist_to_stop_pct": d.get("dist_to_stop_pct"),
                  "below_200dma": d.get("below_200dma"),
                  "already_broken_down": d.get("already_broken_down")}
        lows = (closes.get(t) or [])[:10]
        if lows:
            low10[t] = round(min(lows), 2)
    return pos, low10


def _build_decisions(snapshot, caps, net_liq, cash, as_of, closes,
                     candidates=None, reentries=None, heat_pct=None):
    """Ordered, portfolio-level adjudication. See cockpit/engine/pipeline.py.

    The rules are NOT pre-collected here any more. The pipeline calls each tier against the
    book the tiers above it have already left behind, because a ceiling that sizes itself
    against exposure another ticker is already giving up over-sells: on 2026-08-28 that shape
    produced a $70,000 instruction where $60,000 was the whole requirement."""
    pos, low10 = _decision_inputs(snapshot, closes)
    layers = r_conc.theme_map(list(pos), CFG)
    ctx = pipeline.Context(net_liq=net_liq, cash=cash, as_of=as_of, cfg=CFG, layers=layers,
                           caps=caps, low10=low10,
                           stops={t: d.get("stop_level") for t, d in pos.items()},
                           candidates=candidates or [], reentries=reentries or [],
                           heat_pct=heat_pct)
    decisions, props, trace = pipeline.adjudicate(pos, ctx)
    reasons = {}
    for p in props:
        reasons.setdefault(p.rule_id, p.reason)
    portfolio_flags = [p.reason for p in props if p.ticker == "__portfolio__"]
    return decisions, reasons, layers, portfolio_flags

def _cash_routing_md(sell_total, cash):
    """B46 卖出所得流向. Kept, but it no longer computes anything: the amount is the action
    list's single total, passed in. Previously this line lived inside _action_plan() and summed
    the risk-engine TRIM actions itself, which is how the email ended up with a 72,858 total
    sitting above an 86,442 one."""
    if not sell_total:
        return ""
    L = ["**\U0001F4B8 卖出所得流向（规则=资金优先级 B46）：**"]
    if (cash or 0) < -100:
        L.append("- 执行上面的决策共 **$%s** → ① 优先偿还保证金负债（当前 $%s，先停利息）；"
                 "② 负债清零前不开新仓、不补杠杆产品（红线 v2 ③）；③ 现金转正后：等回调候选到价分批 → 余款留作弹药。"
                 "　执行后现金 ≈ $%s。"
                 % (format(int(sell_total), ","), format(round(cash), ","),
                    format(int((cash or 0) + sell_total), ",")))
    else:
        L.append("- 执行上面的决策共 **$%s** → ① 等回调候选到价分批；② 其余留作现金弹药。"
                 % format(int(sell_total), ","))
    return "\n".join(L) + "\n\n"


def _followups_md(candidates, reentries, heat_pct, mkt, theme_alerts, may_buy):
    """Everything that is NOT an adjudicated position decision: buy candidates, wait prices,
    re-entry prompts, theme lamp. None of these may print a sell amount -- the action list
    above is the only place a number to sell appears."""
    L = []
    if mkt:
        zone_cn, hint = _MKT_ZONE_CN.get(mkt.get("zone"), ("?", ""))
        L += ["\U0001F4CD **市场位置 %s/100（%s）** · %s" % (mkt.get("score"), zone_cn, hint),
              "　↳ QQQ 距52周高 %s%% · vs200日 %s%% · VIX %s（该指数只用作本账户的开仓闸门）" % (
                  mkt.get("off_high_pct"), mkt.get("vs200_pct"), mkt.get("vix")), ""]
    if theme_alerts:
        L += ["**\U0001F7E3 主题集中（已计入上面的决策，此处只作说明）：**"] + [
            "- \U0001F7E3 **%s** 合计 $%s = **%.1f%% 净值**（上限 %.0f%%）" % (
                a["subtheme"], format(a["usd"], ","), a["pct"], a["threshold"])
            for a in theme_alerts] + [""]
    if reentries:
        L += ["**\U0001F501 再入场观察（B44 · 状态，不是订单）：**"]
        for r in reentries:
            L.append("- \U0001F501 **%s**：%s 离场后已收盘重新站上 50 日线（$%s > $%s）。"
                     "是否买、买多少，只由首屏行动清单里的 Decision 决定。" % (
                         r["ticker"], r.get("exit_date") or "?", r["price"], r["ma50"]))
        L.append("")
    if may_buy:
        # NO share counts, NO dollar amounts, NO stops here. A buy that is executable is a
        # Decision and appears in the action list; anything that is not executable yet is an
        # observation and must not be dressed in numbers that look like an order.
        watch = [c for c in (candidates or []) if c.get("posture") == "buyable-on-support"][:2]
        if watch:
            L += ["**形态在支撑区的候选（观察；可执行与否见首屏 Decision）：**"] + [
                "- \U0001F7E2 **%s**（%s·评分%s）——买前须人工核 Serenity14 / VCP / 稀释"
                % (c.get("ticker"), c.get("subtheme"), c.get("score")) for c in watch] + [""]
    waits = [c for c in (candidates or [])
             if c.get("posture") == "extended/wait-pullback" and c.get("wait_20pct")][:2]
    if waits:
        L += ["**等回调候选（到价再谈，B38）：**"] + [
            "- \u23F3 **%s**：等 $%s（52周高−20%%）/ $%s（−25%%）/ 回踩50日线 $%s" % (
                c.get("ticker"), c.get("wait_20pct"), c.get("wait_25pct"), c.get("wait_ma50"))
            for c in waits] + [""]
    return ("\n".join(L) + "\n---\n\n") if L else ""


def build() -> str:
    today = dt.date.today().isoformat()
    phase = calendars.market_phase()
    exclude = set(CFG.get("exclude", []))
    cfg_holdings = [h["ticker"] for h in CFG.get("holdings", [])]
    theme_map = _theme_of()          # R5: overrides included
    theme_of = theme_map
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
        _append_nav(as_of or today, net_liq, run_date=today)   # R3: as_of, not run date
        closed, trimmed_out, prev_pos = _reflect_on_closes(positions, exclude, mem, today)
        drift_extra = sorted(set(holdings) - set(cfg_holdings))   # held, not yet annotated in config
        drift_gone = sorted(set(cfg_holdings) - set(holdings))    # stale config entries (can delete)
    else:
        net_liq = CFG["account"]["net_liq_fallback"]; cash = 0.0
        positions = {}; cur_mv = {}
        holdings = cfg_holdings                                   # fail-open: IBKR down -> config list
        port_note = "IBKR offline: shares/cost/P&L unknown (data gap); caps shown as room-from-flat."
    reentry_prev = _reentry_load()                              # B44: keep quotes for watched names
    quotes = screener.quote_map(sorted(set(_universe()) | set(holdings) | set(reentry_prev) | {"^VIX"}))
    bench_vs200 = 0.0
    if bench in quotes and quotes[bench].get("priceAvg200"):
        bench_vs200 = round((quotes[bench]["price"] / quotes[bench]["priceAvg200"] - 1) * 100, 1)

    hard_cap_usd = policy.hard_cap_usd(CFG)   # fixed $30,000; see cockpit/domain/policy.py
    closes = _hist_window(_corr_universe(holdings, theme_of))
    # B24: risk-table MV on the SAME price basis as the snapshot -- shares x FMP
    # current price; Flex prior-day mv only when no live quote (fail-open).
    cur_mv = {t: ((positions.get(t, {}).get("shares") or 0) * quotes[t]["price"])
                 if (positions.get(t, {}).get("shares") and quotes.get(t, {}).get("price")) else mv
              for t, mv in cur_mv.items()}
    caps = risk.position_caps(closes, net_liq, cur_mv, cash, set(holdings), hard_cap_usd, theme_map)

    setups = {t: screener.name_setup(t, quotes[t], CFG["risk"]["no_chase_bias_threshold_pct"], bench_vs200)
              for t in holdings if t in quotes}
    dilution = {t: fmp.shares_growth(t) for t in holdings}
    dil_on = CFG.get("risk", {}).get("dilution_atm_disqualifier", True)
    holdings_snapshot = _holdings_snapshot(holdings, quotes, setups, positions, net_liq, dilution, dil_on,
                                           closes=closes)          # B48: 20-day-low stops
    heat_usd = sum((d["market_value"] or 0) * (d["dist_to_stop_pct"] or 0) / 100.0
                   for d in holdings_snapshot.values())
    portfolio_heat_pct = round(heat_usd / net_liq * 100, 1) if net_liq else None
    broken = [t for t, d in holdings_snapshot.items() if d["already_broken_down"]]
    opened, violations = _opens_and_violations(prev_pos, positions, exclude, setups, mem, today)
    _append_signal_log(ledger.normalize_date(as_of) or today, net_liq,   # R3: as_of
                       portfolio_heat_pct, holdings_snapshot, caps)

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
    # R25: candidates carry a PRICE for the entry rule to size against; the brief itself no
    # longer computes an "example" share count. An example share count next to a candidate is
    # an order in everything but name, and it was being produced outside the Decision layer.
    for c in candidates:
        c["price"] = (quotes.get(c["ticker"], {}) or {}).get("price")
    mkt = screener.market_position(quotes)
    theme_alerts, _theme_map = _theme_exposure(holdings_snapshot, theme_of, net_liq)   # B36
    pt_trig = float(CFG.get("risk", {}).get("profit_take_trigger_pct", 25))            # B45
    pt_frac = float(CFG.get("risk", {}).get("profit_take_fraction", 0.33))
    profit_takes = []
    for t, d in holdings_snapshot.items():
        pp = d.get("unreal_pnl_pct")
        if pp is not None and pp >= pt_trig and d.get("market_value"):
            lows = (closes.get(t) or [])[:10]          # FMP hist rows are newest-first
            profit_takes.append({"ticker": t, "pnl_pct": pp, "trigger": pt_trig, "frac": pt_frac,
                                 "trim_usd": round(d["market_value"] * pt_frac),
                                 "low10": round(min(lows), 2) if lows else None})
    reentries = _reentry_update(closed, quotes, today, net_liq, maxpos_pct, trimmed_out)  # B44 + B50
    audit_rows, audit_tot = _position_audit(holdings_snapshot, caps, net_liq, theme_of, hard_cap_usd)  # B48
    # R2/R10 (2026-08-28): ONE adjudicated decision per ticker, rendered once.
    as_of_label = ledger.normalize_date(as_of) or today
    decisions, dec_reasons, dec_layers, port_flags = _build_decisions(
        holdings_snapshot, caps, net_liq, cash, as_of_label, closes,
        candidates=candidates, reentries=reentries, heat_pct=portfolio_heat_pct)
    sell_total = total_sell_value(decisions)
    lev_usd = sum((holdings_snapshot.get(t, {}).get("market_value") or 0)
                  for t in holdings_snapshot if r_conc.leverage_of(t, CFG) > 1.0)
    # Heat has ONE meaning and one only (user decision 2026-08-28): it is a gate on ADDING
    # risk, and it never produces a sell amount. The previous wording showed a ⛔ next to the
    # sentence "this is a warning, not a prohibition" -- the reader could not tell which half
    # to believe. It does prohibit new buying; what it does NOT do is demand selling.
    buy_block = None
    if (cash or 0) < -100:
        buy_block = ("保证金使用中（现金 $%s）→ **暂停新增风险**：负债清零前不开新仓、不补杠杆产品"
                     "（红线 v2 ③）" % format(round(cash), ","))
    elif portfolio_heat_pct is not None and portfolio_heat_pct >= 6.0:
        buy_block = ("组合在险 %.1f%% 高于 6-8%% 预算 → **暂停新增风险**：今天不开新仓、不加仓。"
                     "**它不要求你为了把在险打回 6-8%% 而机械卖出**——卖出金额只由上面的绑定规则决定"
                     "（本次合计 $%s）。" % (portfolio_heat_pct, format(int(sell_total), ",")))
    action_md = action_list.render(
        decisions, dec_reasons, as_of=as_of_label, net_liq=net_liq, cash=cash,
        leverage_pct=(lev_usd / net_liq * 100) if net_liq else 0,
        risk_usd=heat_usd, risk_pct=portfolio_heat_pct,
        buying_allowed=(buy_block is None),
        buying_reason=(buy_block or "无约束阻止新增风险；仍须过形态与现金闸门"),
        price_note="",
        staleness=_staleness(as_of_label, today, earn, phase),
        watch_notes=(["⛔ 纪律违规（B37）：" + v for v in (violations or [])]
                     + port_flags
                     + ["本清单只管这个 IBKR 账户；另一个券商账户由你自管，不在本系统职能范围内。"]))
    action_md += _cash_routing_md(sell_total, cash)
    action_md += _followups_md(candidates, reentries, portfolio_heat_pct, mkt, theme_alerts,
                               buy_block is None)

    weak = [t for t, s in setups.items() if not s["stage2"]]
    situation = "Holdings " + ",".join(holdings) + "; weak/below-MA: %s; phase %s" % (weak, phase)
    lessons = mem.retrieve(situation, n=3)

    exc_md, exc_plain = _exceptions(holdings_snapshot, caps, earn)
    bundle = dict(date=today, phase=phase, phase_rule=calendars.PHASE_GUARDRAIL.get(phase, ""),
                  as_of=as_of, port_note=port_note, net_liq=net_liq, cash=cash,
                  portfolio_heat_pct=portfolio_heat_pct, market_position=mkt,
                  broken_down_holdings=broken, closed_positions=closed, opened_positions=opened,
                  discipline_violations=violations, exceptions=exc_plain,
                  theme_exposure_alerts=theme_alerts, profit_take_prompts=profit_takes,
                  reentry_prompts=reentries, position_audit_totals=audit_tot,
                  layer_weakest=[r["ticker"] for r in audit_rows if r["weakest"]],
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
    # B60: the old text told the reader to "set the Flex period to Today". There IS no Today option
    # (verified 2026-08-28 against the Flex Delivery Configuration dropdown), so it was an instruction
    # nobody could follow. as_of is the reportDate of the LAST EquitySummary row, i.e. the newest
    # statement IBKR has produced. Prices are FMP EOD closes and exclude after-hours moves --
    # verified 2026-08-28: MU 935.39 / MRVL 241.45 matched the 08-27 regular close exactly.
    header = ("> ⏱️ 持仓数据截至 %s（IBKR Flex 统计期末日 = IBKR 已出具报表的最新交易日；Flex 无 Today 选项）。"
              "价格为 FMP 收盘价，**不含盘后/隔夜**（财报后的波动不会反映在本表）。\n\n"
              % as_of) if as_of else ""
    drift = ""
    if drift_extra or drift_gone:
        drift = ("> 🟡 **config 注释提醒（B33）**：持仓名单已由 IBKR 实时驱动，跟踪不受影响——"
                 + ("新持仓待补注释/子板块归属: **" + ", ".join(drift_extra) + "**；" if drift_extra else "")
                 + ("config 中已不再持有(可删): " + ", ".join(drift_gone) + "。" if drift_gone else "")
                 + "\n\n")
    title = "# 美股投研日报 — %s%s\n\n" % (today, "（盘中快照：未收盘，勿当收盘复盘）" if phase == "intraday" else "")
    lamps = _lamps_md(portfolio_heat_pct, cash, earn)
    # R10: the audit table is now DIAGNOSTIC ONLY -- no target, no 该减$. The disposal ladder
    # is gone entirely: ordering the sells is the action list's job, and a second ordered list
    # with its own arithmetic is exactly how one book produced three totals.
    audit_md = _audit_md(audit_rows, audit_tot, sell_total=sell_total)
    return (header + title + drift + action_md + lamps + audit_md + exc_md
            + "## 📰 消息与点评（LLM 附录）\n\n" + body + "\n\n---\n\n"
            + _snapshot_md(holdings_snapshot, net_liq, cash)
            + _candidates_md(candidates, subs)
            + scanner.daily_scan(bench_vs200))   # B42 直出 + B28/B41 地图外扫描（fail-open）

def main():
    if not calendars.is_us_trading_day() and os.getenv("FORCE_RUN", "false").lower() != "true":
        print("not a US trading day, skip."); return
    try:
        body = build()
    except Exception as e:
        body = "system degraded: daily brief error (%s). check data/config." % e
    ok = notify.send("daily brief %s" % dt.date.today().isoformat(), body)
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
    flush_pending_writes()   # B60: one-shot state (close detection, re-entry prompts) is spent ONLY
                             # after the brief actually reached the inbox.


if __name__ == "__main__":
    main()
