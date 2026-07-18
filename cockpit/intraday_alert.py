"""B14 intraday event alerts. Fires ONLY when a holding (a) breaches its stop/technical level,
(b) makes a large intraday move (|chg| >= alerts.intraday_move_pct), or (c) has breaking SAME-DAY
news. Runs every 30 min during US market hours via GHA (NON-realtime; cron can lag 5-15 min).
SILENT when nothing fires (no email). Code-rendered (no LLM) for speed/determinism/cost.
Dedupes per (ticker, condition, day) in state/alert_state.json so a standing breach doesn't re-spam.
Positions/avg-cost come from IBKR Flex (EOD -- fine for stop levels); live price from FMP.
Fail-open: any data error -> that check is skipped, never blocks the others."""
from __future__ import annotations
import os, json, datetime as dt, pathlib, yaml
from . import fmp, ibkr, screener, notify, calendars

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}

STATE = ROOT / "state" / "alert_state.json"

def _stop_level(price, a200, avg):
    """Real stop BELOW price = max of (200DMA, cost-20%) that sits under price; broken = all above."""
    cand = [x for x in [a200, (avg * 0.8 if avg else None)] if x]
    below = [L for L in cand if price and price > L]
    return (max(below) if below else None), bool(cand and price and not below)

def _load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"date": "", "fired": {}}

def _save_state(st):
    try:
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

def build_alerts():
    """Return (today, [(ticker, cond_key, text), ...]) for everything currently triggered."""
    today = dt.date.today().isoformat()
    exclude = set(CFG.get("exclude", []))
    cfg_holdings = [h["ticker"] for h in CFG.get("holdings", []) if h["ticker"] not in exclude]
    port = ibkr.get_portfolio() or {"positions": {}}
    pos = port.get("positions", {})
    # B33: IBKR-driven active book (config fallback when Flex empty/offline)
    holdings = sorted({t for t in pos if t not in exclude}) or cfg_holdings
    move_thr = CFG.get("alerts", {}).get("intraday_move_pct", 6.0)
    news_on = CFG.get("alerts", {}).get("news_alerts", True)
    quotes = screener.quote_map(holdings)
    trig = []
    for t in holdings:
        q = quotes.get(t, {})
        price = q.get("price"); a200 = q.get("priceAvg200"); chg = q.get("changePercentage")
        avg = (pos.get(t) or {}).get("avg_price")
        if price:
            stop, broken = _stop_level(price, a200, avg)
            if (stop and price <= stop) or broken:
                txt = ("⚠️ %s 破位：现价 $%.2f" % (t, price)
                       + (" ≤ 止损/技术位 $%.2f" % stop if stop
                          else "（已跌破全部参考位）"))
                trig.append((t, "breach", txt))
        if chg is not None and abs(chg) >= move_thr:
            arrow = "\U0001f4c9" if chg < 0 else "\U0001f4c8"
            trig.append((t, "move", "%s %s 日内异动：%+.1f%%（现价 $%.2f）"
                         % (arrow, t, chg, price or 0)))
    if news_on:
        for n in fmp.stock_news(holdings, limit=25):
            d = str(n.get("publishedDate") or n.get("date") or "")[:10]
            sym = n.get("symbol") or ""
            if d == today and sym in holdings:
                title = (n.get("title") or "")[:80]
                trig.append((sym, "news:" + title[:40],
                             "\U0001f4f0 %s 新闻：%s" % (sym, title)))
    return today, trig

def main():
    if os.getenv("FORCE_RUN", "false").lower() != "true":
        if not calendars.is_us_trading_day():
            print("not a US trading day, skip."); return
        if calendars.market_phase() not in ("intraday", "closing_auction"):
            print("not US market hours, skip."); return
    today, trig = build_alerts()
    st = _load_state()
    if st.get("date") != today:
        st = {"date": today, "fired": {}}
    fresh = [(t, k, txt) for (t, k, txt) in trig if not st["fired"].get("%s:%s" % (t, k))]
    if not fresh:
        print("no new alerts."); _save_state(st); return
    for t, k, _ in fresh:
        st["fired"]["%s:%s" % (t, k)] = today
    _save_state(st)
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%MZ")
    body = ("盘中事件警报 " + stamp + "\n\n"
            + "\n".join("- " + txt for _, _, txt in fresh)
            + "\n\n（仅信息提示，非下单建议；持仓股数/成本截至上一交易日，价格为 FMP 实时）")
    notify.send("⚠️ 盘中警报 %s" % today, body)
    print(body)

if __name__ == "__main__":
    main()
