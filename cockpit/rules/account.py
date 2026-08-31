"""Tier 1: account safety. Red line v2 (leveraged-ETF narrow door) + margin state.

These are the only rules allowed to override everything else, so they are deliberately
few and mechanical.
"""
from __future__ import annotations
from ..domain.models import RuleProposal, CAP_VALUE, FLAG, HARD_EXIT, TIER_ACCOUNT_SAFETY

RULE_LEV_CAP  = "account.leveraged_etf_cap"
RULE_LEV_STOP = "account.leveraged_etf_hard_stop"
RULE_MARGIN   = "account.margin_in_use"


def _leveraged_stop_level(pos, cfg):
    """Red line v2: a 2x ETF is hard-stopped at −15% from cost OR the 20-day low, WHICHEVER
    IS HIT FIRST on the way down. On a falling price the first level reached is the HIGHER of
    the two, so the operative stop is max(cost x 0.85, 20d_low x 0.99).

    Returns (level, which, cost_stop, low_stop) or (None, ...) when inputs are missing.
    Fail-open on data gaps -- but the gap is reported, never silently treated as "no stop"."""
    pct = float(((cfg or {}).get("risk") or {}).get("leveraged_etf_hard_stop_pct", 15))
    cost = pos.get("cost_price")
    cost_stop = (float(cost) * (1 - pct / 100.0)) if cost else None
    low_stop = pos.get("stop_level")
    cands = [(v, k) for v, k in ((cost_stop, "cost−%.0f%%" % pct), (low_stop, "20日低×0.99"))
             if v is not None]
    if not cands:
        return None, None, cost_stop, low_stop
    level, which = max(cands)
    return level, which, cost_stop, low_stop


def propose(positions, net_liq, cash, cfg, stops=None):
    rk = (cfg.get("risk") or {})
    lev_map = (rk.get("leverage_factors") or {})
    max_pct = float(rk.get("leveraged_etf_max_pct_nav", 5))
    out = []
    if not net_liq:
        return out

    lev_names = [t for t in positions if float(lev_map.get(t, 1.0)) > 1.0]
    lev_usd = sum(float(positions[t].get("market_value") or 0) for t in lev_names)
    if lev_usd > net_liq * max_pct / 100.0 + 1 and lev_names:
        allowed = net_liq * max_pct / 100.0
        scale = allowed / lev_usd
        for t in sorted(lev_names):
            mv = float(positions[t].get("market_value") or 0)
            out.append(RuleProposal(
                rule_id=RULE_LEV_CAP, ticker=t, kind=CAP_VALUE, tier=TIER_ACCOUNT_SAFETY,
                target_value=round(mv * scale, 2),
                reason="杠杆 ETF 合计 %.1f%% 净值 > 红线 v2 上限 %.0f%%" % (
                    lev_usd / net_liq * 100, max_pct),
                evidence={"leveraged_total_usd": round(lev_usd, 2), "max_pct_nav": max_pct,
                          "invalidation": "杠杆 ETF 合计回落至 %.0f%% 净值以下" % max_pct}))

    # RULE_LEV_STOP -- previously an unused constant. Red line v2 clause 4 says the hard stop
    # on a leveraged ETF is unconditional, so it belongs in tier 1 (account safety), above the
    # concentration ceilings: a breached leveraged stop exits the whole position regardless of
    # what any sizing rule would otherwise allow.
    for t in sorted(lev_names):
        p = positions[t] or {}
        px = p.get("price")
        level, which, cost_stop, low_stop = _leveraged_stop_level(p, cfg)
        if level is None:
            out.append(RuleProposal(
                rule_id=RULE_LEV_STOP, ticker=t, kind=FLAG, tier=TIER_ACCOUNT_SAFETY,
                reason="杠杆 ETF **无法计算硬止损**（缺成本价与 20 日低）——数据缺口，不是「没有止损」",
                confidence="unverified", evidence={"cost_price": p.get("cost_price"),
                                                   "stop_level": p.get("stop_level")}))
            continue
        if px is not None and px <= level:
            out.append(RuleProposal(
                rule_id=RULE_LEV_STOP, ticker=t, kind=HARD_EXIT, tier=TIER_ACCOUNT_SAFETY,
                reason="杠杆 ETF 硬止损触发：现价 $%.2f ≤ $%.2f（%s，两者先到者）——红线 v2 ④ 无条件执行"
                       % (px, level, which),
                evidence={"level": round(level, 4), "trigger": which,
                          "cost_stop": (round(cost_stop, 4) if cost_stop else None),
                          "low_stop": (round(low_stop, 4) if low_stop else None),
                          "price": px,
                          "invalidation": "无——杠杆 ETF 的硬止损不可撤销，触发即离场"}))
        else:
            out.append(RuleProposal(
                rule_id=RULE_LEV_STOP, ticker=t, kind=FLAG, tier=TIER_ACCOUNT_SAFETY,
                reason="杠杆 ETF 硬止损位 $%.2f（%s，两者先到者）；现价 $%s，距 %.1f%%"
                       % (level, which, px, ((px - level) / px * 100) if px else 0),
                evidence={"level": round(level, 4), "trigger": which,
                          "cost_stop": (round(cost_stop, 4) if cost_stop else None),
                          "low_stop": (round(low_stop, 4) if low_stop else None)}))

    if (cash or 0) < -100:
        out.append(RuleProposal(
            rule_id=RULE_MARGIN, ticker="__portfolio__", kind=FLAG, tier=TIER_ACCOUNT_SAFETY,
            reason="保证金使用中（现金 $%s，计息）→ 清零前不开新仓、不补杠杆产品（红线 v2 ③）"
                   % format(int(cash), ","),
            evidence={"cash": cash}))
    return out
