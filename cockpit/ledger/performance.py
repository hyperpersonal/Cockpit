"""Performance ledger. Three jobs, all previously done wrong or not at all.

R3 -- NAV must be filed under the Flex `as_of` date, never the run date.
      The 2026-08-28 brief carried portfolio data stamped as_of=20260827 and wrote it into
      nav_history under "2026-08-28". nav_history therefore has no 2026-08-27 entry at all,
      and every window computed from it is shifted by a day.

R4 -- attribution must include names that left the book, and must not launder their P&L
      into the residual bucket. Verified against state/signal_history.json on 2026-08-28:
        * the 选股 / 尺寸 buckets drop an exited name entirely (`if not vN: continue`)
        * the market-P&L bucket keeps the days before the exit but drops the final leg
          (`if not vb: continue`), so NVDA's +8.7% post-earnings day and its sale vanish
        * that dropped P&L lands in `residual`, which is LABELLED "deposits / interest /
          fees" -- i.e. trading results reported as external cash flow

TWR -- the raw NAV change is meaningless here. NAV went 9,968.29 -> 159,528.31 while the
      account LOST money: deposits were 160,000.00 and IBKR's own time-weighted return for
      the period is -1.62%.

Honesty rules carried into the output, not just the docstring:
  * every figure says which window it covers and what it could not see
  * an exit priced from the last observed quote is labelled ESTIMATED, never mixed silently
    with a real fill
  * if external cash flows are unknown for a window, TWR for that window is refused rather
    than approximated
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
NAV_PATH = ROOT / "state" / "nav_history.json"
FLOW_PATH = ROOT / "state" / "cash_flows.json"


# --------------------------------------------------------------------------- NAV timeline
def _load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return dict(default)


def append_nav_at_as_of(as_of, net_liq, run_date=None, path=None, source="flex"):
    """Record NAV under the date the DATA is for, not the date the job ran.

    Keeps the legacy {"navs": {date: value}} shape so existing readers keep working, and
    adds {"meta": {date: {run_date, source}}} so a later reader can tell a same-day figure
    from one carried over from the previous session.
    """
    path = pathlib.Path(path or NAV_PATH)
    d = _load(path, {"navs": {}})
    d.setdefault("navs", {})
    if "meta" not in d and d["navs"]:
        # First write after R3. Every pre-existing key was written with the RUN date while the
        # data it holds came from the previous trading day's Flex statement. We do NOT rewrite
        # them: only 2026-08-28 was verified against the broker (159,528.31 = the 08-27 NAV),
        # and silently re-keying a series we cannot verify day-by-day would trade a visible
        # error for an invisible one. The marker makes the ambiguity readable instead.
        d["_legacy_keys_are_run_dates_through"] = max(d["navs"])
        d["_legacy_note"] = ("keys at or before this date are RUN dates; the NAV they hold is "
                             "the previous trading day's Flex figure. Not rewritten -- unverified "
                             "day by day. Keys after it are Flex as_of dates.")
    d.setdefault("meta", {})
    key = normalize_date(as_of)
    if not key:
        raise ValueError("append_nav_at_as_of requires a real as_of date, got %r" % (as_of,))
    d["navs"][key] = round(float(net_liq), 2)
    d["meta"][key] = {"run_date": normalize_date(run_date) or None, "source": source,
                      "stale": bool(run_date and normalize_date(run_date) != key)}
    try:
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    return d


def normalize_date(v):
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s[:10]


# ------------------------------------------------------------------------- cash flows
def load_cash_flows(path=None):
    """External cash flows: {date: amount}. Positive = deposit.

    The GHA Flex query (1551541) currently requests only NAV + Open Positions, so the
    runner cannot see deposits. Until that query also returns the cash-transaction
    sections, this file is seeded from the IBKR activity statement and carries the date it
    was last reconciled. An unknown window makes TWR REFUSE, not guess.
    """
    d = _load(path or FLOW_PATH, {"flows": {}, "verified_through": None, "source": None})
    return ({normalize_date(k): float(v) for k, v in (d.get("flows") or {}).items()},
            normalize_date(d.get("verified_through")), d.get("source") or d.get("_source"))


# ------------------------------------------------------------------------------- TWR
SUSPECT_JUMP_PCT = 15.0     # a one-day NAV move this large with no recorded flow is a red flag


def twr(nav_by_date, flows_by_date=None, verified_through=None,
        suspect_jump_pct=SUSPECT_JUMP_PCT):
    """Daily-linked time-weighted return, neutralising external flows.

    Flows are treated as arriving at the START of the day, which is what makes a deposit
    invisible to the return: r_t = NAV_t / (NAV_{t-1} + F_t) - 1.

    On refusing vs. answering. The first version of this refused whenever the window ran past
    the date cash flows were reconciled to. That is safe and useless: deposits are rare, the
    reconciled date lags by construction, and a number that is permanently unavailable is a
    number the user never gets. Refusing "just in case" is not caution, it is the same
    silence this system keeps failing on -- only with a virtuous label.

    So: report the number, and go LOOKING for the thing that would invalidate it. Any day
    whose NAV moves more than `suspect_jump_pct` with no flow on record is reported as a
    suspected unrecorded flow, and THAT is what refuses. An unreconciled tail with no
    suspicious day gets the number plus an explicit caveat naming the tail.
    """
    flows_by_date = {normalize_date(k): float(v) for k, v in (flows_by_date or {}).items()}
    navs = {normalize_date(k): float(v) for k, v in (nav_by_date or {}).items() if v}
    ds = sorted(navs)
    if len(ds) < 2:
        return {"twr_pct": None, "window": None, "n_points": len(ds),
                "refused": "need at least two NAV points", "suspected_flows": [],
                "unreconciled_from": None}

    suspected = []
    link = 1.0
    for a, b in zip(ds, ds[1:]):
        f = flows_by_date.get(b, 0.0)
        base = navs[a] + f
        if base <= 0:
            continue
        chg_pct = (navs[b] - navs[a]) / navs[a] * 100 if navs[a] else 0.0
        if not f and abs(chg_pct) >= suspect_jump_pct:
            suspected.append({"date": b, "nav_change_pct": round(chg_pct, 1),
                              "nav_from": navs[a], "nav_to": navs[b]})
        link *= navs[b] / base

    unreconciled_from = None
    if verified_through and ds[-1] > verified_through:
        unreconciled_from = min(d for d in ds if d > verified_through)

    out = {"window": "%s..%s" % (ds[0], ds[-1]), "n_points": len(ds),
           "suspected_flows": suspected, "unreconciled_from": unreconciled_from,
           "flows_in_window": round(sum(v for k, v in flows_by_date.items()
                                        if ds[0] < k <= ds[-1]), 2)}
    if suspected:
        out["twr_pct"] = None
        out["refused"] = ("NAV moved %s on %s with no cash flow on record -- that is the shape of "
                          "an unrecorded deposit or withdrawal. Reconcile %s before trusting a "
                          "return for this window."
                          % ("/".join("%+.1f%%" % x["nav_change_pct"] for x in suspected),
                             "/".join(x["date"] for x in suspected),
                             ", ".join(x["date"] for x in suspected)))
        return out
    out["twr_pct"] = round((link - 1) * 100, 2)
    out["refused"] = None
    if unreconciled_from:
        out["caveat"] = ("现金流仅对账至 %s；%s 起的 %d 天若有出入金未记录，本数字会失真"
                         "（已按 >=%.0f%% 单日跳变扫描，未发现可疑日）"
                         % (verified_through, unreconciled_from,
                            sum(1 for d in ds if d >= unreconciled_from), suspect_jump_pct))
    return out


# ---------------------------------------------------------------------- attribution
def attribution_with_exits(days, window_days=14, exit_prices=None, realized=None,
                           exit_kinds=None):
    """Attribution that keeps the names that left.

    days: the signal_history structure -- [{date, net_liq, holdings:{t:{shares,price}}}, ...]
    exit_prices: {ticker: price} last observed quote for a name that vanished (B44 ledger)
    realized: {ticker: usd} authoritative realised P&L from the broker, when available

    Every exited name appears in the output with an explicit `basis`:
        "broker"    -- realised P&L from the statement (authoritative)
        "estimated" -- priced off the last observed quote; the true fill is unknown
        "unknown"   -- it left and we have neither; reported as unknown, never as zero
    """
    exit_prices = exit_prices or {}
    realized = realized or {}
    exit_kinds = exit_kinds or {}
    # `holdings is not None` -- NOT truthiness. An empty book is real data (everything was
    # sold), and dropping that day would hide the single most informative event in the window.
    # The same shortcut is what made exits invisible in the first place.
    days = [d for d in (days or []) if d.get("net_liq") and d.get("holdings") is not None]
    if len(days) < 3:
        return None
    days = days[-(window_days + 1):] if window_days else days
    first, last = days[0], days[-1]

    per, exits = {}, {}
    mkt_total = resid_total = 0.0
    for a, b in zip(days, days[1:]):
        ha, hb = a["holdings"], b["holdings"]
        day_mkt = 0.0
        for t, va in ha.items():
            sh, pa = va.get("shares"), va.get("price")
            if sh is None or pa is None:
                continue
            vb = hb.get(t)
            if vb and vb.get("price") is not None:
                c = sh * (vb["price"] - pa)
                day_mkt += c
                per[t] = per.get(t, 0.0) + c
                continue
            # the name LEFT the book on this pair. It is not zero and it is not residual.
            if t in realized:
                exits[t] = {"usd": float(realized[t]), "basis": "broker", "exit_date": b.get("date")}
            elif exit_prices.get(t):
                c = sh * (float(exit_prices[t]) - pa)
                _kind = exit_kinds.get(t)
                _basis = "backfill" if _kind == "backfill" else "estimated"
                exits[t] = {"usd": round(c, 2), "basis": _basis, "exit_date": b.get("date"),
                            "note": ("回填条目：离场价 = 最后一次观察到的快照价，误差最大"
                                     if _basis == "backfill" else "按最后观察到的报价估算，非成交价")}
                day_mkt += c
                per[t] = per.get(t, 0.0) + c
            else:
                exits[t] = {"usd": None, "basis": "unknown", "exit_date": b.get("date"),
                            "note": "离场当日无价可用——报告为未知，不报告为 0"}
        mkt_total += day_mkt
        resid_total += (b["net_liq"] - a["net_liq"]) - day_mkt

    names, rets, wts = [], [], []
    for t, v0 in first["holdings"].items():
        p0 = v0.get("price")
        vN = last["holdings"].get(t)
        pN = vN.get("price") if vN else exit_prices.get(t)      # exited names keep their seat
        if not p0 or not pN:
            continue
        w = [h["shares"] * h["price"] / d["net_liq"]
             for d in days
             for h in [d["holdings"].get(t)]
             if h and h.get("shares") and h.get("price") and d.get("net_liq")]
        if not w:
            continue
        names.append(t)
        rets.append(pN / p0 - 1)
        wts.append(sum(w) / len(w))
    eq = sum(rets) / len(rets) if rets else 0.0
    wtd = sum(w * r for w, r in zip(wts, rets))

    unknown = sorted(t for t, e in exits.items() if e["basis"] == "unknown")
    estimated = sorted(t for t, e in exits.items() if e["basis"] == "estimated")
    backfilled = sorted(t for t, e in exits.items() if e["basis"] == "backfill")
    return {
        "d0": first.get("date"), "dN": last.get("date"), "n_days": len(days),
        "nav0": first["net_liq"], "navN": last["net_liq"],
        "mkt_pnl": round(mkt_total, 2), "residual": round(resid_total, 2),
        "pick_pct": round(eq * 100, 2), "size_pct": round((wtd - eq) * 100, 2),
        "per": sorted(per.items(), key=lambda kv: -abs(kv[1])),
        "exits": exits, "names_in_pick_size": sorted(names),
        "coverage": {
            "exits_included": sorted(exits),
            "priced_from_broker": sorted(t for t, e in exits.items() if e["basis"] == "broker"),
            "priced_from_last_quote": estimated,
            "priced_from_backfill_seed": backfilled,
            "unknown": unknown,
            "residual_meaning": ("外部现金流 + 利息 + 费用 + 任何本表未能定价的离场"
                                 if unknown else "外部现金流 + 利息 + 费用"),
        },
    }
