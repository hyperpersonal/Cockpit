"""Portfolio-level SEQUENTIAL adjudication.

The defect this replaces (found 2026-08-28 with the real functions):

    NAV 100,000 · theme ceiling 60% · ORD 60,000 (ordinary) · LEV 10,000 at 2x = 20,000
    exposure · theme exposure 80,000. ORD breaches its execution stop -> HARD_EXIT.

    Old behaviour: every rule generated its proposals from the ORIGINAL book, and the engine
    then took the strictest value PER TICKER. The theme rule therefore sized its cut against
    80,000 of exposure and took LEV to zero -- while ORD, 60,000 of that same exposure, was
    already leaving. Total sell 70,000; final theme exposure 0.
    Correct: once ORD exits, the theme holds 20,000, under its 60,000 ceiling, and LEV should
    not be touched at all. Total sell 60,000.

"One Decision per ticker" was never the whole requirement. Flattening conflicts per ticker
says nothing about a rule sizing itself against exposure another rule is already removing.
What was missing is that each tier must see the book the tiers above it have already left
behind.

So:

  PASS A -- unconditional exits, gathered from EVERY stage against the original book.
      A hard exit depends on price, cost or a verified fact; never on position size. It is
      therefore knowable before any sizing runs, and it MUST be known first, or the sizing
      rules budget against exposure that is already going away. This is what fixes the case
      above, and it fixes it for the leveraged hard stop (tier 1) and the execution stop
      (tier 5) equally -- not just for the one construction that exposed it.

  PASS B -- ceilings, tier by tier, in priority order. Each stage is re-evaluated against the
      CURRENT working book, so the theme rule sees the positions that survived tier 1, tier 2
      and the single-name hard cap.

  PASS C -- profit trims and entries, on the final working book. A trim is skipped for any
      name a higher tier has already reduced: taking a third of what is left after a
      concentration cut is a second bite at the same position.

Post-condition, asserted here and tested in tests/test_pipeline_sequential.py: after every
Decision is applied, no binding ceiling is still breached, and no cut is larger than the
binding constraint required.
"""
from __future__ import annotations

from ..domain.models import (RuleProposal, HARD_EXIT, CAP_VALUE, TRIM_TO, BUY,
                             TIER_ACCOUNT_SAFETY, TIER_THESIS_BROKEN, TIER_CONCENTRATION,
                             TIER_SIZING, TIER_TREND_STOP, TIER_PROFIT_TAKE, TIER_ENTRY)
from .resolve import build_decisions

MONEY_EPS = 1.0


class Context:
    """Everything the stages need that is not the working book itself."""

    def __init__(self, net_liq, cash, as_of, cfg, layers=None, caps=None, low10=None,
                 stops=None, candidates=None, reentries=None, heat_pct=None):
        self.net_liq = net_liq
        self.cash = cash
        self.as_of = as_of
        self.cfg = cfg or {}
        self.layers = layers or {}
        self.caps = caps or {}
        self.low10 = low10 or {}
        self.stops = stops or {}
        self.candidates = candidates or []
        self.reentries = reentries or []
        self.heat_pct = heat_pct


# Stages carry the phases they participate in. Entry runs ONLY in pass C: a buy must be sized
# against the book every sell decision has already left behind, and against the cash and theme
# headroom that actually remain.
PHASE_EXITS = "A"
PHASE_CAPS = "B"
PHASE_ENTRIES = "C"


def default_stages():
    """(tier, name, fn, phases) in adjudication order. Imported lazily so a rule module can
    import the engine without a cycle."""
    from ..rules import account, thesis, concentration, sizing, profit, entry
    from ..rules import exit as exit_rules
    AB = (PHASE_EXITS, PHASE_CAPS)
    return [
        (TIER_ACCOUNT_SAFETY, "account",
         lambda wb, c: account.propose(wb, c.net_liq, c.cash, c.cfg), AB),
        (TIER_THESIS_BROKEN, "thesis",
         lambda wb, c: thesis.propose(wb, c.layers, c.cfg, today=c.as_of), AB),
        (TIER_CONCENTRATION, "concentration",
         lambda wb, c: concentration.propose(wb, c.net_liq, c.cfg), AB),
        (TIER_SIZING, "sizing",
         lambda wb, c: sizing.propose(wb, c.caps, c.net_liq, c.cfg), AB),
        (TIER_TREND_STOP, "exit",
         lambda wb, c: exit_rules.propose(wb, c.layers, c.cfg), AB),
        (TIER_PROFIT_TAKE, "profit",
         lambda wb, c: profit.propose(wb, c.cfg, low10=c.low10), (PHASE_ENTRIES,)),
        (TIER_ENTRY, "entry",
         lambda wb, c: entry.propose(wb, c.net_liq, c.cash, c.cfg, layers=c.layers,
                                     candidates=c.candidates, reentries=c.reentries,
                                     heat_pct=c.heat_pct, today=c.as_of, as_of=c.as_of),
         (PHASE_ENTRIES,)),
    ]


def _fresh(positions):
    out = {}
    for t, p in (positions or {}).items():
        d = dict(p)
        if d.get("market_value") is None and d.get("shares") and d.get("price"):
            d["market_value"] = d["shares"] * d["price"]
        out[t] = d
    return out


def _apply(working, targets):
    """Rewrite the working book so the next tier sees what the previous ones left."""
    for t, v in targets.items():
        if t not in working:
            continue
        px = working[t].get("price")
        working[t]["market_value"] = round(float(v), 2)
        if px:
            working[t]["shares"] = round(float(v) / px, 6)


def adjudicate(positions, ctx, stages=None):
    """Returns (decisions, proposals, trace). One Decision per ticker, by construction."""
    stages = stages or default_stages()
    original = _fresh(positions)
    working = _fresh(positions)
    targets = {t: float(p.get("market_value") or 0.0) for t, p in original.items()}
    winners, seen, proposals, trace = {}, set(), [], []

    def keep(p):
        key = (p.rule_id, p.ticker, p.kind, p.target_value)
        if key not in seen:
            seen.add(key)
            proposals.append(p)

    # ---- PASS A: unconditional exits, known before any sizing -----------------------
    exits = []
    for tier, name, fn, phases in stages:
        if PHASE_EXITS not in phases:
            continue
        for p in fn(original, ctx) or []:
            if p.binding and p.kind == HARD_EXIT:
                exits.append((tier, p))
    for tier, p in sorted(exits, key=lambda tp: (tp[0], tp[1].rule_id)):
        if targets.get(p.ticker, 0.0) > MONEY_EPS:
            targets[p.ticker] = 0.0
            winners[p.ticker] = p
            keep(p)
            trace.append({"pass": "A", "tier": tier, "rule": p.rule_id, "ticker": p.ticker,
                          "target": 0.0, "why": "unconditional exit, applied before sizing"})
    _apply(working, targets)

    # ---- PASS B: ceilings, tier by tier, each on the updated book --------------------
    for tier, name, fn, phases in stages:
        if PHASE_CAPS not in phases:
            continue
        props = fn(working, ctx) or []
        for p in props:
            if p.ticker == "__portfolio__" or p.kind in (HARD_EXIT,):
                keep(p)
                continue
            if not (p.binding and p.kind == CAP_VALUE and p.target_value is not None):
                keep(p)
                continue
            keep(p)
            cur = targets.get(p.ticker)
            if cur is None:
                continue
            if p.target_value < cur - 0.005:
                targets[p.ticker] = float(p.target_value)
                winners[p.ticker] = p
                trace.append({"pass": "B", "tier": tier, "rule": p.rule_id,
                              "ticker": p.ticker, "target": float(p.target_value),
                              "why": "strictest ceiling on the book left by higher tiers"})
        _apply(working, targets)

    # ---- PASS C: trims and entries, on the final book --------------------------------
    for tier, name, fn, phases in stages:
        if PHASE_ENTRIES not in phases:
            continue
        for p in fn(working, ctx) or []:
            keep(p)
            if p.kind not in (TRIM_TO, BUY):
                continue
            if p.kind == BUY:
                # A BUY may name a ticker the book has never held. It must still become a
                # Decision -- silently dropping it (the `cur is None -> continue` this
                # replaces) is exactly how buys stayed outside the Decision layer while
                # _followups_md printed share counts of its own.
                if p.ticker not in original:
                    px = (p.evidence or {}).get("entry_price")
                    original[p.ticker] = {"shares": 0.0, "price": px, "market_value": 0.0}
                    targets[p.ticker] = 0.0
                cur_mv = float(original[p.ticker].get("market_value") or 0.0)
                if cur_mv > MONEY_EPS:
                    trace.append({"pass": "C", "tier": tier, "rule": p.rule_id,
                                  "ticker": p.ticker, "target": targets[p.ticker],
                                  "why": "skipped: already held, not a new entry"})
                    continue
                if p.target_value and p.target_value > targets.get(p.ticker, 0.0):
                    targets[p.ticker] = float(p.target_value)
                    winners[p.ticker] = p
                    trace.append({"pass": "C", "tier": tier, "rule": p.rule_id,
                                  "ticker": p.ticker, "target": float(p.target_value),
                                  "why": "entry sized against the post-sell book"})
                continue
            cur = targets.get(p.ticker)
            if cur is None:
                continue
            # a name a higher tier already reduced is not trimmed again IN THIS REPORT.
            # User decision 2026-08-28: same period, same ticker -- a profit trim may not be
            # stacked on top of a tier 1-5 reduction. It stays as a supporting reason and is
            # re-evaluated next report against the book the execution actually leaves.
            if cur < float(original[p.ticker].get("market_value") or 0.0) - MONEY_EPS:
                trace.append({"pass": "C", "tier": tier, "rule": p.rule_id,
                              "ticker": p.ticker, "target": cur,
                              "why": "skipped: a higher tier already reduced this position "
                                     "this period; profit-take is re-evaluated next report"})
                continue
            if p.kind == TRIM_TO and p.target_value is not None and p.target_value < cur:
                targets[p.ticker] = float(p.target_value)
                winners[p.ticker] = p
                trace.append({"pass": "C", "tier": tier, "rule": p.rule_id,
                              "ticker": p.ticker, "target": float(p.target_value),
                              "why": "profit trim, nothing stricter applied"})
    _apply(working, targets)

    decisions = build_decisions(original, targets, winners, proposals, ctx)
    return decisions, proposals, trace


def theme_exposures(targets, layers, cfg):
    from ..rules.concentration import leverage_of
    out = {}
    for t, v in targets.items():
        lay = layers.get(t, "unmapped")
        out[lay] = out.get(lay, 0.0) + float(v) * leverage_of(t, cfg)
    return out
