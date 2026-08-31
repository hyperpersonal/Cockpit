"""Tier 6: profit-taking ladder (B45).

Emits a TRIM_TO proposal when unrealised gain clears risk.profit_take_trigger_pct.
It is binding only in the sense that it can produce a number when nothing stricter
applies -- any concentration or safety ceiling overrides it, and the noise floor in the
adjudicator kills it outright on a residual position (the 2026-08-28 email proposed
taking $59 of profit on a 0.741-share, $179 MRVL stub).
"""
from __future__ import annotations
from ..domain.models import RuleProposal, TRIM_TO, TIER_PROFIT_TAKE

RULE_PROFIT = "profit.take_ladder"


def propose(snapshot, cfg, low10=None):
    rk = (cfg.get("risk") or {})
    trig = float(rk.get("profit_take_trigger_pct", 25))
    frac = float(rk.get("profit_take_fraction", 0.33))
    low10 = low10 or {}
    out = []
    for t, d in sorted((snapshot or {}).items()):
        pnl = d.get("unreal_pnl_pct")
        if pnl is None and d.get("cost_price") and d.get("price"):
            pnl = (d["price"] / d["cost_price"] - 1) * 100
        mv = d.get("market_value")
        if pnl is None or mv is None or pnl < trig:
            continue
        out.append(RuleProposal(
            rule_id=RULE_PROFIT, ticker=t, kind=TRIM_TO, tier=TIER_PROFIT_TAKE,
            target_value=round(mv * (1 - frac), 2),
            reason="浮盈 %.1f%% ≥ +%.0f%% → 止盈减 %d%%（或将止损上移至 10 日低%s）" % (
                pnl, trig, round(frac * 100),
                (" $%s" % low10[t]) if low10.get(t) else ""),
            evidence={"pnl_pct": round(pnl, 1), "trigger_pct": trig, "fraction": frac,
                      "low10": low10.get(t),
                      "invalidation": "浮盈回落至 +%.0f%% 以下" % trig}))
    return out
