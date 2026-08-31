"""Tier 2: VERIFIED structural veto only.

History: config carried an unverified annotation saying CCXI was "Churchill Capital XI,
a SPAC shell, outside the framework". The daily brief turned that string into a
"liquidate the whole position" instruction. The company is in fact the Agility Robotics
de-SPAC (B54). A structural veto now requires a verification stamp naming a date and a
first-party source; without one the rule can only ask for review.
"""
from __future__ import annotations
from ..domain.models import RuleProposal, HARD_EXIT, REVIEW, FLAG, TIER_THESIS_BROKEN
from . import facts

RULE_OUTSIDE = "thesis.outside_framework"
RULE_STALE = "thesis.fact_needs_reverification"
OUTSIDE_LAYER = "outside_framework"


def propose(positions, layers, cfg, today=None):
    """A structural veto requires a CURRENTLY-VERIFIED fact: a source, a verification date,
    and a date that has not expired. Anything less can only ask for review.

    Separately, every held name whose annotation is unverified or expired raises a FLAG, so
    a stale fact is visible before it has a chance to drive a decision rather than after."""
    roles = {h.get("ticker"): h.get("role") for h in (cfg.get("holdings") or [])}
    out = []
    for t in sorted(positions):
        a = facts.assess(roles.get(t), cfg, today)
        if layers.get(t) == OUTSIDE_LAYER:
            if facts.may_force_exit(a):
                out.append(RuleProposal(
                    rule_id=RULE_OUTSIDE, ticker=t, kind=HARD_EXIT, tier=TIER_THESIS_BROKEN,
                    reason="已核实处于框架之外（%s）：%s" % (a["reason"], str(roles.get(t))[:100]),
                    confidence="verified",
                    evidence={"annotation": roles.get(t), "verification": a,
                              "invalidation": "标的重新归入某一产业层，或核实结论被推翻"}))
            else:
                out.append(RuleProposal(
                    rule_id=RULE_OUTSIDE, ticker=t, kind=REVIEW, tier=TIER_THESIS_BROKEN,
                    reason="config 标为体系外，但该断言 **%s**（%s）——**不产生清仓指令**，先重新核实身份"
                           % (a["status"], a["reason"]),
                    confidence=a["status"], evidence={"annotation": roles.get(t),
                                                      "verification": a}))
            continue
        if a["status"] != facts.VERIFIED:
            out.append(RuleProposal(
                rule_id=RULE_STALE, ticker=t, kind=FLAG, tier=TIER_THESIS_BROKEN,
                reason="身份/赛道注释 **%s**：%s——只作提醒，不驱动任何决策"
                       % (a["status"], a["reason"]),
                confidence=a["status"], evidence={"verification": a}))
    return out
