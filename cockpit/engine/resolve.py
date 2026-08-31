"""Decision assembly, plus a single-book flattener.

The ORDERED, portfolio-level adjudication lives in cockpit/engine/pipeline.py. This module
holds the piece both paths share -- turning a final {ticker: target} map into Decisions --
and `resolve_decisions()`, which flattens a hand-built set of proposals for one book. Use
`pipeline.adjudicate()` for anything real: a flattener cannot know that another ticker's exit
already removed the exposure a ceiling was sizing against.

One ticker in, exactly one Decision out.

Adjudication order (user decision, 2026-08-28):
    账户安全 -> 已核实的结构性否决 -> 主题及单票集中度 -> 风险预算和仓位上限
    -> 趋势与止损 -> 止盈 -> 新买入

Rules of adjudication:
  1. A binding HARD_EXIT anywhere sets the target position to 0.
  2. Otherwise the final target is the STRICTEST binding numeric ceiling.
  3. "同层最弱且 RS 为负" may only produce a REVIEW proposal. It is a ranking, not a
     verified verdict, and on 2026-08-28 it ordered full liquidation of KLAC, GLW and
     RAM on that basis alone.
  4. Advisory proposals (binding=False) never move the number. They ride along in
     `supporting_rules` so the appendix can explain, without producing a rival answer.
  5. Exactly one Decision per ticker. There is no other place in the system where a
     trade amount may be computed.

The portfolio risk budget (組合熱度 6-8%) is ADVISORY by explicit user decision
(2026-08-28): it is displayed as one number and one lamp, and never sets a sell amount.
"""
from __future__ import annotations
import datetime as dt

from ..domain.models import (Decision, RuleProposal, make_decision_id, TIER_NAMES,
                             HARD_EXIT, CAP_VALUE, TRIM_TO, BUY, REVIEW, FLAG)

SHARE_DP = 4          # IBKR trades fractional shares; 4dp matches the Flex statement
MONEY_EPS = 1.0       # ignore sub-dollar deltas


def _round_shares(x):
    return round(float(x), SHARE_DP)


def _strictest(caps):
    """caps: list[RuleProposal] with numeric target_value. Returns (value, proposal)."""
    best = min(caps, key=lambda p: (p.target_value, p.tier))
    return best.target_value, best


def resolve_decisions(proposals, positions, net_liq, as_of, cfg=None, context=None):
    """Flatten one book's proposals per ticker. Kept for single-tier and hand-built cases.

    It does NOT re-evaluate a rule after another ticker changed, so it cannot see that an exit
    elsewhere already satisfied a ceiling. Real adjudication goes through
    cockpit.engine.pipeline.adjudicate(); this is the last step of that pipeline's contract,
    exposed on its own for tests that construct proposals directly.
    """
    from .pipeline import Context
    cfg = cfg or {}
    context = context or {}
    ctx = Context(net_liq=net_liq, cash=None, as_of=as_of, cfg=cfg,
                  stops=(context or {}).get("stops", {}))

    original = {}
    for t, p in (positions or {}).items():
        d = dict(p)
        if d.get("market_value") is None and d.get("shares") and d.get("price"):
            d["market_value"] = d["shares"] * d["price"]
        original[t] = d
    for p in proposals:
        if p.ticker not in original and p.kind == BUY:
            original[p.ticker] = {"shares": None, "price": None, "market_value": 0.0}

    targets = {t: float(d.get("market_value") or 0.0) for t, d in original.items()}
    winners = {}
    by = {}
    for p in proposals:
        by.setdefault(p.ticker, []).append(p)
    for ticker, props in by.items():
        if ticker not in targets:
            continue
        binding = [p for p in props if p.binding]
        hard = [p for p in binding if p.kind == HARD_EXIT]
        caps = [p for p in binding if p.kind == CAP_VALUE and p.target_value is not None]
        trims = [p for p in binding if p.kind == TRIM_TO and p.target_value is not None]
        buys = [p for p in binding if p.kind == BUY]
        if hard:
            winners[ticker] = min(hard, key=lambda p: p.tier)
            targets[ticker] = 0.0
        elif caps:
            v, w = _strictest(caps)
            targets[ticker] = min(v, targets[ticker])
            winners[ticker] = w
        elif trims:
            v, w = _strictest(trims)
            if v < targets[ticker]:
                targets[ticker] = v
                winners[ticker] = w
        elif buys and not (original[ticker].get("market_value") or 0):
            w = max(buys, key=lambda p: p.target_value)
            targets[ticker] = w.target_value
            winners[ticker] = w

    return build_decisions(original, targets, winners, proposals, ctx)

def build_decisions(original, targets, winners, proposals, ctx):
    """Turn a final {ticker: target_value} map into one Decision per ticker.

    `original` is the book as it was BEFORE any rule ran, so current_shares and the traded
    delta are always measured against what the user actually holds.
    """
    cfg = getattr(ctx, "cfg", {}) or {}
    net_liq = getattr(ctx, "net_liq", None)
    as_of = getattr(ctx, "as_of", None)
    stops = getattr(ctx, "stops", {}) or {}
    rk = (cfg.get("risk") or {})
    min_pct_nav = float(rk.get("min_decision_pct_nav", 0.25))
    min_usd = float(rk.get("min_decision_usd", 200))

    by_ticker = {}
    for p in proposals or []:
        by_ticker.setdefault(p.ticker, []).append(p)

    valid_until = _valid_until(as_of)
    out = []
    for ticker in sorted(original):
        pos = original[ticker]
        shares, price = pos.get("shares"), pos.get("price")
        mv = pos.get("market_value")
        if mv is None and shares and price:
            mv = shares * price
        target_value = targets.get(ticker, mv)
        winner = winners.get(ticker)
        props = sorted(by_ticker.get(ticker, []), key=lambda p: (p.tier, p.rule_id))
        notes = ["%s：%s" % (TIER_NAMES.get(p.tier, "?"), p.reason)
                 for p in props if p.kind in (REVIEW, FLAG)]

        if price and target_value is not None:
            target_shares = _round_shares(target_value / price)
        else:
            target_shares = shares
        delta_shares = _round_shares((target_shares or 0) - (shares or 0)) \
            if shares is not None else target_shares
        delta_value = (delta_shares * price) if (delta_shares is not None and price) else None

        if delta_value is not None and delta_value < -MONEY_EPS:
            action = "EXIT" if (target_value is not None and target_value <= MONEY_EPS) else "SELL"
        elif delta_value is not None and delta_value > MONEY_EPS:
            action = "BUY"
        else:
            action = "HOLD"

        if action in ("SELL", "EXIT") and net_liq and mv is not None:
            if (mv / net_liq * 100) < min_pct_nav and abs(delta_value or 0) < min_usd:
                notes.append("残仓噪声闸门：仓位 $%.0f（%.3f%% 净值）低于 %.2f%% 且建议额 $%.0f "
                             "低于 $%.0f，不产生指令"
                             % (mv, mv / net_liq * 100, min_pct_nav, abs(delta_value or 0),
                                min_usd))
                action, target_value = "HOLD", mv
                target_shares, delta_shares, delta_value = shares, 0.0, 0.0
                winner = None

        out.append(Decision(
            decision_id=make_decision_id(as_of, ticker, action, target_value),
            as_of=as_of, ticker=ticker, action=action,
            current_shares=shares, target_shares=target_shares, delta_shares=delta_shares,
            target_value=round(target_value, 2) if target_value is not None else None,
            delta_value=round(delta_value, 2) if delta_value is not None else None,
            price=price,
            binding_rule=(winner.rule_id if winner else "none"),
            supporting_rules=[p.rule_id for p in props if p is not winner and p.kind != FLAG],
            order_hint=_order_hint(action, delta_shares, delta_value),
            valid_until=valid_until,
            invalidation_conditions=_invalidations(action, winner, ticker, price, net_liq, {}),
            expected_risk_usd=_risk_at_target(
                target_value, price,
                # a new entry has no position stop yet: its stop lives in the entry proposal
                stops.get(ticker) or ((winner.evidence or {}).get("stop") if winner else None)),
            source_confidence=(winner.confidence if winner else "verified"),
            tier=(winner.tier if winner else None),
            notes=notes,
        ))
    out.sort(key=lambda d: (-d.sell_value, d.ticker))
    _assert_one_per_ticker(out)
    return out


def _assert_one_per_ticker(decisions):
    seen = {}
    for d in decisions:
        if d.ticker in seen:
            raise AssertionError("two Decisions for %s -- the adjudicator is broken" % d.ticker)
        seen[d.ticker] = d


def total_sell_value(decisions) -> float:
    """THE sell total. Every section that shows a total calls this and nothing else."""
    return round(sum(d.sell_value for d in decisions), 2)


def _order_hint(action, delta_shares, delta_value):
    if action == "HOLD" or not delta_shares:
        return "无动作"
    verb = "卖出" if delta_shares < 0 else "买入"
    return "手动%s %s 股（≈$%s）；系统不下单" % (
        verb, abs(_round_shares(delta_shares)), format(int(abs(delta_value or 0)), ","))


def _valid_until(as_of):
    try:
        d = dt.date.fromisoformat(str(as_of)[:10].replace("/", "-")) if "-" in str(as_of) \
            else dt.datetime.strptime(str(as_of), "%Y%m%d").date()
    except Exception:
        return "下一封日报"
    return (d + dt.timedelta(days=1)).isoformat() + "（或下一封日报，以先到者为准）"


def _invalidations(action, winner, ticker, price, net_liq, context):
    if action == "HOLD" or winner is None:
        return []
    conds = []
    if price:
        conds.append("%s 价格偏离 $%.2f 超过 ±5%%（金额需重算）" % (ticker, price))
    if net_liq:
        conds.append("净值偏离 $%s 超过 ±3%%" % format(int(net_liq), ","))
    ev = winner.evidence or {}
    if ev.get("invalidation"):
        conds.insert(0, ev["invalidation"])
    return conds


def _risk_at_target(target_value, price, stop):
    if not (target_value and price and stop and price > stop):
        return None
    return round(target_value * (price - stop) / price, 0)
