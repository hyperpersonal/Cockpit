"""Tier 4: risk budget and position ceilings -- ADVISORY.

User decision 2026-08-28: the portfolio heat budget (6-8%) and the per-name risk-tier
reverse-sizing are a WARNING LAYER. They are shown, they are explained in the appendix,
and they never set a trade amount. Making them binding would have produced a standing
instruction to sell $86k-$110k of a $158k book, which is not an executable answer.

So every proposal here is binding=False. The appendix may say "按风险预算口径还需再减
$X"; that is one clearly labelled reference number, not a rival answer.
"""
from __future__ import annotations
from ..domain.models import RuleProposal, CAP_VALUE, TIER_SIZING

RULE_VOLCORR = "sizing.vol_corr_cap"
RULE_RISKTIER = "sizing.risk_tier_target"


def propose(snapshot, caps, net_liq, cfg):
    rk = (cfg.get("risk") or {})
    hi_rs = float(rk.get("high_conviction_rs", 20))
    pct_hi = float(rk.get("risk_pct_high", 2.0))
    pct_base = float(rk.get("risk_pct_base", 1.0))
    out = []
    for t, d in sorted((snapshot or {}).items()):
        cap_usd = (caps.get(t) or {}).get("cap_usd")
        mv = d.get("market_value")
        if cap_usd and mv and mv > cap_usd + 1:
            out.append(RuleProposal(
                rule_id=RULE_VOLCORR, ticker=t, kind=CAP_VALUE, tier=TIER_SIZING,
                target_value=round(cap_usd, 2), binding=False,
                reason="vol×corr 动态上限 $%s（参考口径，不产生指令）" % format(int(cap_usd), ","),
                evidence={"cap_usd": cap_usd, "market_value": mv}))
        px, stop, rs = d.get("price"), d.get("stop_level"), d.get("rs")
        if px and stop and px > stop and net_liq:
            tier = pct_hi if (rs is not None and rs >= hi_rs) else pct_base
            target = net_liq * tier / 100.0 / ((px - stop) / px)
            if mv and target < mv:
                out.append(RuleProposal(
                    rule_id=RULE_RISKTIER, ticker=t, kind=CAP_VALUE, tier=TIER_SIZING,
                    target_value=round(target, 2), binding=False,
                    reason="按 %.0f%% 单笔风险反推的仓位 $%s（参考口径，不产生指令）"
                           % (tier, format(int(target), ",")),
                    evidence={"risk_tier_pct": tier, "stop": stop, "price": px}))
    return out
