"""B28+B41 all-market new-mainline scanner (off-map discovery).

Hunts emerging leadership OUTSIDE the hand-drawn config.subthemes map (the XLE blind spot):
  universe = US common stocks (mcap / price / dollar-volume floors from config.scanner) via the
             FMP screener, refreshed every universe_refresh_days, persisted to state;
  daily    = scan ONE rotating shard (shard_size quotes/day -- Starter-friendly: full universe
             rotates in ~universe/shard_size trading days) for names within near_high_pct of
             their 52w high AND with RS proxy (vs200 - SPY vs200, same convention as screener.py)
             >= rs_min_vs_spy; hits persist in state with first/last-seen dates and expire when
             requalification fails or they go stale;
  cluster  = >=cluster_min_names hits in the same industry = suspected new mainline; a cluster
             seen for >=cluster_suggest_days (calendar-day approximation) -> suggest promoting
             that industry into config.subthemes;
  B41      = fundamentals confirmation: latest annual revenue AND gross-profit growth positive
             for the strongest members ("price cluster + earnings verification" double confirm).
On-map names (subthemes/holdings/exclude) are skipped -- this module ONLY looks off-map.
Output is a CODE-RENDERED markdown section (B22 lesson: never handed to the LLM).
Fail-open everywhere: any error returns a labeled data-gap line, never breaks the brief."""
from __future__ import annotations
import json, datetime as dt, pathlib, yaml
from . import fmp

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
except Exception:
    CFG = {}
SC = CFG.get("scanner") or {}
STATE = ROOT / "state" / "scanner_state.json"

def _load() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}

def _save(st: dict):
    try:
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass

def _onmap() -> set:
    s = set()
    for v in (CFG.get("subthemes") or {}).values():
        s |= set(v.get("etfs") or []) | set(v.get("names") or [])
    s |= {h.get("ticker") for h in CFG.get("holdings", []) if isinstance(h, dict)}
    s |= set(CFG.get("exclude") or [])
    return s

def _refresh_universe(st: dict, today: str) -> dict:
    days = int(SC.get("universe_refresh_days", 7))
    last = st.get("universe_date")
    if last and st.get("universe") and (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days < days:
        return st
    rows = fmp.screener(marketCapMoreThan=int(SC.get("min_mcap_usd", 2000000000)),
                        priceMoreThan=float(SC.get("min_price", 10)),
                        isActivelyTrading="true", country="US", limit=3000)
    if not rows:                                    # fail-open: keep whatever universe we had
        return st
    onmap = _onmap()
    min_dv = float(SC.get("min_dollar_vol_usd", 50000000))
    uni = {}
    for r in rows:
        t = r.get("symbol")
        px, vol = (r.get("price") or 0), (r.get("volume") or 0)
        if not t or t in onmap or px * vol < min_dv:
            continue
        uni[t] = r.get("industry") or r.get("sector") or "unknown"
    if uni:
        st["universe"] = uni                        # {ticker: industry}
        st["universe_date"] = today
        st["cursor"] = 0
    return st

def _scan_shard(st: dict, today: str, bench_vs200: float):
    uni = st.get("universe") or {}
    names = sorted(uni)
    if not names:
        return st, 0
    n = min(int(SC.get("shard_size", 250)), len(names))
    cur = int(st.get("cursor", 0)) % len(names)
    shard = [names[(cur + i) % len(names)] for i in range(n)]
    st["cursor"] = (cur + n) % len(names)
    quotes = {q["symbol"]: q for q in fmp.batch_quote(shard) if isinstance(q, dict) and "symbol" in q}
    near = float(SC.get("near_high_pct", 5.0))
    rs_min = float(SC.get("rs_min_vs_spy", 20.0))
    hits = st.get("hits") or {}
    for t in shard:
        q = quotes.get(t) or {}
        p, hi, a200 = q.get("price"), q.get("yearHigh"), q.get("priceAvg200")
        if not (p and hi and a200):
            continue
        off_high = round((p / hi - 1) * 100, 1)
        rs = round((p / a200 - 1) * 100 - (bench_vs200 or 0.0), 1)
        if off_high >= -near and rs >= rs_min:
            h = hits.get(t) or {"first_seen": today}
            h.update({"last_seen": today, "off_high": off_high, "rs": rs,
                      "industry": uni.get(t, "unknown"), "price": p})
            hits[t] = h
        elif t in hits:
            del hits[t]                             # rescanned today and no longer qualifies
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=14)).isoformat()
    st["hits"] = {t: h for t, h in hits.items() if str(h.get("last_seen") or today) >= cutoff}
    return st, n

def _growth_ok(t: str):
    """B41: latest annual revenue growth AND gross-profit growth both positive.
    True/False, or None on data gap (labeled, never guessed)."""
    d = fmp._get("financial-statement-growth", symbol=t, period="annual", limit=1)
    if isinstance(d, list) and d:
        row = d[0]
        rev = row.get("revenueGrowth")
        gp = row.get("grossProfitGrowth", row.get("growthGrossProfit"))
        try:
            return float(rev) > 0 and float(gp) > 0
        except (TypeError, ValueError):
            return None
    return None

def _clusters(st: dict, today: str) -> list:
    hits = st.get("hits") or {}
    by_ind = {}
    for t, h in hits.items():
        by_ind.setdefault(h.get("industry") or "unknown", []).append((t, h))
    min_n = int(SC.get("cluster_min_names", 3))
    seen = st.get("cluster_first_seen") or {}
    out = []
    for ind, members in by_ind.items():
        if ind == "unknown" or len(members) < min_n:
            continue
        first = seen.get(ind) or today
        seen[ind] = first
        members.sort(key=lambda kv: -(kv[1].get("rs") or 0))
        checks = [_growth_ok(t) for t, _ in members[:5]]
        pos = sum(1 for ok in checks if ok is True)
        gap = sum(1 for ok in checks if ok is None)
        days_seen = (dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days + 1
        out.append({"industry": ind, "n": len(members),
                    "members": [(t, h.get("off_high"), h.get("rs")) for t, h in members[:5]],
                    "days": days_seen, "growth_pos": pos, "growth_gap": gap, "growth_n": len(checks),
                    "confirmed": pos >= min(3, len(checks)),
                    "suggest": days_seen >= int(SC.get("cluster_suggest_days", 5))})
    st["cluster_first_seen"] = {i: d for i, d in seen.items()
                               if i in by_ind and len(by_ind[i]) >= min_n}
    return sorted(out, key=lambda c: -c["n"])

def daily_scan(bench_vs200: float) -> str:
    """Entry point for daily_brief. Returns a code-rendered markdown section; NEVER raises."""
    try:
        if not SC or SC.get("enabled") is False:
            return ""
        today = dt.date.today().isoformat()
        st = _load()
        st = _refresh_universe(st, today)
        st, scanned = _scan_shard(st, today, bench_vs200)
        clusters = _clusters(st, today)
        _save(st)
        uni_n = len(st.get("universe") or {})
        hit_n = len(st.get("hits") or {})
        L = ["", "---",
             "## 🛰️ 地图外新主线扫描（B28+B41 · 系统直出 · 不经 LLM）", "",
             "宇宙 %d 票（mcap≥$%.0fB / 价≥$%.0f / 日成交额≥$%.0fM，每 %d 天刷新）｜本日轮扫 %d 票｜贴近新高+强RS 命中 %d 票" % (
                 uni_n, float(SC.get("min_mcap_usd", 2000000000)) / 1e9, float(SC.get("min_price", 10)),
                 float(SC.get("min_dollar_vol_usd", 50000000)) / 1e6,
                 int(SC.get("universe_refresh_days", 7)), scanned, hit_n)]
        if uni_n == 0:
            L.append("")
            L.append("> ⚠️ 宇宙为空（screener 数据缺口或首跑未成功）——fail-open，明日重试。")
        elif not clusters:
            L.append("")
            L.append("今日无「同业 ≥%d 只贴近新高」的地图外聚类。" % int(SC.get("cluster_min_names", 3)))
        else:
            L += ["", "| 行业簇 | 家数 | 最强代表（距高%/RS） | 持续天数 | 业绩同向(B41) | 建议 |",
                  "|---|---|---|---|---|---|"]
            for c in clusters[:5]:
                reps = "、".join("%s(%s/%s)" % m for m in c["members"][:3])
                gr = ("✅ %d/%d 正增长" % (c["growth_pos"], c["growth_n"])) if c["confirmed"] else (
                     "⚠️ %d 正 %d 缺口" % (c["growth_pos"], c["growth_gap"]))
                sug = "已持续≥%d天：建议将该行业加入 config.subthemes" % int(SC.get("cluster_suggest_days", 5)) \
                      if c["suggest"] else "继续观察"
                L.append("| %s | %d | %s | %d | %s | %s |" % (c["industry"], c["n"], reps, c["days"], gr, sug))
            L.append("")
            L.append("> 发现≠追高（B28 铁律）：簇内个股仍须过雷达 posture/等待价 + Serenity14/稀释核查，按 1% 风险入场；扫描器只负责「看见」。")
        return "\n".join(L) + "\n"
    except Exception as e:
        return "\n---\n> 🛰️ 地图外扫描数据缺口（%s）——fail-open，不影响简报其余部分。\n" % e
