"""Tier 5 (trend & stop) plus the demoted ranking signals.

The 2026-08-28 email ordered FULL LIQUIDATION of KLAC, GLW and RAM on the reason
"本层最弱且 RS 为负" alone. A within-layer ranking is not a verified verdict on a
company; it is a sort order over whatever happens to be in the same bucket, and with
two names in a layer one of them is always "the weakest". That reason now produces a
REVIEW proposal, which carries no number and cannot move a position.

A breached EXECUTION stop (20-day closing low x0.99, B48) is a different thing: it is
mechanical, pre-committed, and stays a hard exit.
"""
from __future__ import annotations
from ..domain.models import (RuleProposal, HARD_EXIT, REVIEW, FLAG,
                             TIER_TREND_STOP, TIER_SIZING)

RULE_STOP     = "exit.stop_breached"
RULE_NEAR     = "exit.near_stop"
RULE_TREND    = "exit.below_200dma"
RULE_WEAKEST  = "exit.weakest_in_layer"


def propose(snapshot, layers=None, cfg=None):
    """snapshot: {ticker: {price, stop_level, already_broken_down, below_200dma,
    dist_to_stop_pct, rs}}. layers: {ticker: layer} for the weakest-in-layer ranking."""
    layers = layers or {}
    out = []

    ranked = {}
    buckets = {}
    for t, d in (snapshot or {}).items():
        buckets.setdefault(layers.get(t, "unmapped"), []).append((t, d.get("rs")))
    for layer, members in buckets.items():
        ordered = sorted(members, key=lambda kv: -(kv[1] if kv[1] is not None else -9999))
        for i, (t, _rs) in enumerate(ordered, 1):
            ranked[t] = (layer, i, len(ordered))

    for t, d in sorted((snapshot or {}).items()):
        if d.get("already_broken_down"):
            out.append(RuleProposal(
                rule_id=RULE_STOP, ticker=t, kind=HARD_EXIT, tier=TIER_TREND_STOP,
                reason="已跌破执行止损（20 日收盘低 ×0.99 = $%s）" % d.get("stop_level"),
                evidence={"stop_level": d.get("stop_level"), "price": d.get("price"),
                          "invalidation": "收盘重新站回 $%s 之上" % d.get("stop_level")}))
            continue
        dist = d.get("dist_to_stop_pct")
        if dist is not None and dist < 5:
            out.append(RuleProposal(
                rule_id=RULE_NEAR, ticker=t, kind=FLAG, tier=TIER_TREND_STOP,
                reason="距执行止损仅 %.1f%%（$%s）——即将触发" % (dist, d.get("stop_level")),
                evidence={"dist_pct": dist}))
        if d.get("below_200dma"):
            out.append(RuleProposal(
                rule_id=RULE_TREND, ticker=t, kind=FLAG, tier=TIER_TREND_STOP,
                reason="跌破 200 日线（趋势旗标，与执行止损分开）",
                evidence={"below_200dma": True}))
        layer, i, n = ranked.get(t, ("unmapped", 1, 1))
        if n >= 2 and i == n and (d.get("rs") or 0) < 0:
            out.append(RuleProposal(
                rule_id=RULE_WEAKEST, ticker=t, kind=REVIEW, tier=TIER_SIZING,
                reason="%s 层内 RS 最弱（%d/%d，RS=%s）——**复查候选，非清仓依据**；"
                       "要清仓需另有已核实的结构性理由" % (layer, i, n, d.get("rs")),
                confidence="unverified",
                evidence={"layer": layer, "rank": "%d/%d" % (i, n), "rs": d.get("rs")}))
    return out
