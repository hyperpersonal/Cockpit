"""Builds the unified decision set from the frozen 2026-08-27 book.

No network, no FMP, no LLM: position facts come from the IBKR statement and the
technical signals from the system's own 2026-08-28 output. This is the fixed input
every consistency test runs against, so a change in behaviour shows up as a changed
number rather than as a changed market.
"""
from __future__ import annotations
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml  # noqa: E402
from cockpit.rules import concentration  # noqa: E402
from cockpit.engine import pipeline  # noqa: E402
from cockpit.engine.resolve import total_sell_value  # noqa: E402

BOOK = json.load(open(os.path.join(HERE, "fixtures", "book_20260827.json"), encoding="utf-8"))


def config():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    cfg.setdefault("risk", {})
    return cfg


def build(cfg=None, caps=None):
    cfg = cfg or config()
    pos = BOOK["positions"]
    # market_value must never be an independent third number: it IS shares x price.
    # A fixture where they can drift is a fixture that can hide a defect.
    for _t, _d in pos.items():
        _calc = _d["shares"] * _d["price"]
        if abs(_calc - _d["market_value"]) > 0.02:
            raise AssertionError("book fixture inconsistent for %s: shares x price = %.2f "
                                 "but market_value = %.2f" % (_t, _calc, _d["market_value"]))
    nav, cash = BOOK["net_liq"], BOOK["cash"]
    rs = {t: d["rs"] for t, d in pos.items()}
    layers = concentration.theme_map(list(pos), cfg)
    snap = {t: dict(d, unreal_pnl_pct=(
        (d["price"] / d["cost_price"] - 1) * 100 if d.get("cost_price") else None))
        for t, d in pos.items()}

    ctx = pipeline.Context(net_liq=nav, cash=cash, as_of=BOOK["as_of"], cfg=cfg,
                           layers=layers, caps=caps or {},
                           stops={t: d["stop_level"] for t, d in pos.items()})
    decisions, props, trace = pipeline.adjudicate(snap, ctx)

    return {"decisions": decisions, "proposals": props, "layers": layers,
            "net_liq": nav, "cash": cash, "positions": pos, "trace": trace,
            "total_sell": total_sell_value(decisions)}


def sections_from(decisions):
    """Every money-bearing section reads the SAME Decision. Simulating three sections
    here is the point: they must be identical by construction, not by luck."""
    amounts = {d.ticker: d.sell_value for d in decisions if d.is_sell}
    return {"action_plan": dict(amounts), "position_audit": dict(amounts),
            "disposal_ladder": dict(amounts), "exception_list": dict(amounts)}
