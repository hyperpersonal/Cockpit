"""Tier 7: entries. Candidates and re-entries become RuleProposals, never text.

Why this module exists. Sells went through the unified Decision layer on 2026-08-28, but BUYS
did not: `_followups_md()` printed "可考虑买 X 股 ≈ $Y，止损=入场−8%", the re-entry prompt printed
its own share count and dollar value, and the radar table printed a "1% 风险示例股数" column.
Three more places producing executable numbers outside the adjudicator -- the exact shape that
gave one book three sell totals, only on the buy side and not yet caught by a live email.

Sizing. A binding BUY is the MINIMUM of every ceiling that applies:
    1% account risk budget implied by the entry-to-stop distance
    the fixed $30,000 single-name hard cap
    the theme's remaining headroom AFTER every higher-priority sell decision
    available VERIFIED cash
Never borrowed money: the cash term is the cash the broker statement actually shows.

Cash gate, conservative by decision (user, 2026-08-28): if verified cash is negative, this
report produces no BUY at all -- not even against proceeds the same report is proposing to
raise. A planned sale is not cash until it settles and appears on the next IBKR statement.

Verification gate: Serenity-14, VCP and dilution must each carry a source and a verification
date that has not expired. Anything less yields a WATCH, which carries no share count and no
dollar amount. The system does not compute those three for candidates today, so in production
every candidate currently resolves to WATCH -- that is the honest state, not a bug to paper
over by inventing precision.
"""
from __future__ import annotations
import math

from ..domain.models import RuleProposal, BUY, REVIEW, TIER_ENTRY
from ..domain.policy import hard_cap_usd
from . import facts
from .concentration import leverage_of, layer_of

RULE_BUY = "entry.candidate_buy"
RULE_WATCH = "entry.candidate_watch"
RULE_REENTRY = "entry.reentry_buy"
RULE_REENTRY_WATCH = "entry.reentry_watch"

VERIFY_KEYS = ("serenity14", "vcp", "dilution")
DEFAULT_STOP_PCT = 8.0          # B38/B44: entry stop is entry x (1 - 8%)


def _verification(cand, cfg, today):
    """Every required check must be present, sourced, dated and unexpired."""
    missing, detail = [], {}
    for k in VERIFY_KEYS:
        v = (cand.get("verification") or {}).get(k)
        if not isinstance(v, dict) or not v.get("ok"):
            missing.append(k)
            detail[k] = {"status": "unverified", "reason": "缺 %s 的核实结果" % k}
            continue
        a = facts.assess("核实 %s，源=%s" % (v.get("verified_on"), v.get("source")), cfg, today)
        detail[k] = a
        if a["status"] != facts.VERIFIED:
            missing.append(k)
    return missing, detail


def _theme_headroom(positions, cfg, net_liq):
    thr = float((cfg.get("risk") or {}).get("theme_exposure_alert_pct", 40))
    limit = net_liq * thr / 100.0
    expo = {}
    for t, p in (positions or {}).items():
        lay = layer_of(t, cfg)
        expo[lay] = expo.get(lay, 0.0) + float(p.get("market_value") or 0.0) * leverage_of(t, cfg)
    return {k: max(limit - v, 0.0) for k, v in expo.items()}, limit, expo


def _gates(cash, heat_pct, cfg):
    """Reasons this report may not open or add risk at all."""
    out = []
    if (cash or 0) <= 0:
        out.append("已核实现金为 $%s（非正）→ 本报告不产生买入；**不依赖本轮建议卖出的预计回款**，"
                   "成交出现在下一份 IBKR 账面后再重新评估" % format(round(cash or 0), ","))
    if heat_pct is not None and heat_pct >= 6.0:
        out.append("组合在险 %.1f%% ≥ 6%% → 暂停新增风险，本轮不产生买入" % heat_pct)
    return out


def _size(entry_price, stop, net_liq, cfg, headroom, cash_left):
    """Returns (value, shares, binding_name, all_caps). Never exceeds any single ceiling."""
    if not (entry_price and stop and entry_price > stop):
        return 0.0, 0, "invalid", {}
    risk_pct = float((cfg.get("risk") or {}).get("risk_pct_base", 1.0))
    per_share_risk = entry_price - stop
    caps = {
        "risk_budget_%.0f%%" % risk_pct: net_liq * risk_pct / 100.0 / (per_share_risk / entry_price),
        "single_name_hard_cap": hard_cap_usd(cfg),
        "theme_headroom": max(headroom, 0.0),
        "available_cash": max(cash_left, 0.0),
    }
    binding_name = min(caps, key=lambda k: caps[k])
    value = caps[binding_name]
    shares = int(math.floor(value / entry_price))
    return round(shares * entry_price, 2), shares, binding_name, {k: round(v, 2)
                                                                  for k, v in caps.items()}


def propose(positions, net_liq, cash, cfg, layers=None, candidates=None, reentries=None,
            heat_pct=None, today=None, as_of=None):
    """positions = the WORKING book, i.e. after every higher-priority sell decision."""
    out = []
    blocks = _gates(cash, heat_pct, cfg)
    headroom, limit, expo = _theme_headroom(positions, cfg, net_liq)
    cash_left = max(float(cash or 0.0), 0.0)

    items = []
    for c in (candidates or []):
        px = c.get("price") or c.get("entry_price")
        items.append({"ticker": c.get("ticker"), "src": "radar", "cand": c, "price": px,
                      "score": c.get("score"), "posture": c.get("posture"),
                      "rule": RULE_BUY, "watch_rule": RULE_WATCH})
    for r in (reentries or []):
        items.append({"ticker": r.get("ticker"), "src": "reentry_watch", "cand": r,
                      "price": r.get("price"), "score": None, "posture": "reentry",
                      "rule": RULE_REENTRY, "watch_rule": RULE_REENTRY_WATCH})

    # deterministic order: highest score first, then ticker. Never RS, never market noise.
    items.sort(key=lambda i: (-(i["score"] if i["score"] is not None else -1e9), i["ticker"]))

    for it in items:
        t, c, px = it["ticker"], it["cand"], it["price"]
        if not t:
            continue
        if t in (positions or {}) and (positions[t].get("market_value") or 0) > 0:
            continue                       # already held: this is not a new entry
        stop = c.get("stop") or (px * (1 - DEFAULT_STOP_PCT / 100.0) if px else None)
        lay = layer_of(t, cfg) if cfg else "unmapped"
        missing, detail = _verification(c, cfg, today or as_of)
        base_ev = {"source": it["src"], "as_of": as_of, "entry_price": px, "stop": stop,
                   "layer": lay, "score": it["score"], "posture": it["posture"],
                   "verification": detail,
                   "wait_prices": {k: c.get(k) for k in ("wait_20pct", "wait_25pct", "wait_ma50")
                                   if c.get(k) is not None}}
        if blocks or missing or not px:
            gaps = list(blocks)
            if missing:
                gaps.append("必要条件未核实：" + "、".join(missing)
                            + "（未核实即不给股数与金额，避免伪精确）")
            if not px:
                gaps.append("无可用价格，无法定价")
            out.append(RuleProposal(
                rule_id=it["watch_rule"], ticker=t, kind=REVIEW, tier=TIER_ENTRY,
                reason="观察（不可执行）：" + "；".join(gaps),
                confidence="unverified",
                evidence=dict(base_ev, data_gaps=gaps,
                              manual_confirmation=["Serenity 14 点", "VCP 形态", "增发/稀释核查"])))
            continue
        value, shares, binding_name, caps = _size(px, stop, net_liq, cfg,
                                                  headroom.get(lay, 0.0), cash_left)
        if shares <= 0 or value <= 0:
            out.append(RuleProposal(
                rule_id=it["watch_rule"], ticker=t, kind=REVIEW, tier=TIER_ENTRY,
                reason="观察（不可执行）：按最紧约束 **%s** 算出的金额不足一股" % binding_name,
                confidence="verified", evidence=dict(base_ev, caps=caps)))
            continue
        cash_left -= value
        headroom[lay] = max(headroom.get(lay, 0.0) - value * leverage_of(t, cfg), 0.0)
        out.append(RuleProposal(
            rule_id=it["rule"], ticker=t, kind=BUY, tier=TIER_ENTRY, target_value=value,
            reason="新开仓：%s 股 × $%.2f = $%s；最紧约束 = **%s**；止损 $%.2f（入场−%.0f%%）" % (
                shares, px, format(int(value), ","), binding_name, stop, DEFAULT_STOP_PCT),
            evidence=dict(base_ev, shares=shares, caps=caps, binding_cap=binding_name,
                          risk_usd=round(shares * (px - stop), 2),
                          invalidation="价格离开入场区、止损位变化、或现金/主题余量不足")))
    return out
