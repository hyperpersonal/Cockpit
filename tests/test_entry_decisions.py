"""Entries go through the Decision layer, or they do not exist.

Before this, sells were adjudicated but buys were not: _followups_md() printed "可考虑买 X 股
≈ $Y，止损=入场−8%", the re-entry prompt printed its own share count and dollar value, and the
radar table printed a "1% 风险示例股数" column. Three more producers of executable numbers
outside the adjudicator -- the same defect class as the three sell totals, on the buy side.
"""
from __future__ import annotations
import copy, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import harness  # noqa: E402
from cockpit.domain.models import TIER_ENTRY  # noqa: E402
from cockpit.engine import pipeline  # noqa: E402
from cockpit.rules import entry  # noqa: E402

BASE = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
TODAY = "2026-08-28"


def verified(when="2026-08-25"):
    return {k: {"ok": True, "source": "人工核对 %s" % k, "verified_on": when}
            for k in entry.VERIFY_KEYS}


def cfg(theme_pct=40, layers=None, leverage=None, cap=30000):
    c = copy.deepcopy(BASE)
    c["subthemes"] = {}
    c["risk"].update(theme_overrides=dict(layers or {}), leverage_factors=dict(leverage or {}),
                     theme_exposure_alert_pct=theme_pct, single_name_hard_cap_usd=cap,
                     leveraged_etf_max_pct_nav=100)
    c["holdings"] = [{"ticker": t, "role": "x（核实 2026-08-25，源=FMP profile）"}
                     for t in (layers or {})]
    return c


def cand(ticker, price=50.0, score=30.0, verify=True, **kw):
    d = {"ticker": ticker, "price": price, "score": score, "subtheme": "T",
         "posture": "buyable-on-support"}
    if verify:
        d["verification"] = verified()
    d.update(kw)
    return d


def run(pos, c, layers, cash, net_liq=100000.0, candidates=(), reentries=(), heat=None):
    ctx = pipeline.Context(net_liq=net_liq, cash=cash, as_of=TODAY, cfg=c, layers=layers,
                           candidates=list(candidates), reentries=list(reentries),
                           heat_pct=heat,
                           stops={t: d.get("stop_level") for t, d in pos.items()})
    ds, props, trace = pipeline.adjudicate(pos, ctx)
    return {"by": {d.ticker: d for d in ds}, "ds": ds, "props": props, "trace": trace}


HOLD = {"HELD": {"shares": 100.0, "price": 100.0, "market_value": 10000.0,
                 "cost_price": 100.0, "stop_level": 80.0, "rs": 1.0}}
LAYERS = {"HELD": "T", "CAND": "T", "CAND2": "T", "BACK": "T"}
CFG = cfg(layers=LAYERS)


class T1_StageWiring(unittest.TestCase):
    def test_default_stages_includes_tier_entry(self):
        tiers = [t for t, _n, _f, _p in pipeline.default_stages()]
        self.assertIn(TIER_ENTRY, tiers)

    def test_entry_runs_only_in_the_entry_phase(self):
        for tier, name, _fn, phases in pipeline.default_stages():
            if tier == TIER_ENTRY:
                self.assertEqual(tuple(phases), (pipeline.PHASE_ENTRIES,),
                                 "a buy must be sized against the post-sell book")


class T2_NewTickerBecomesADecision(unittest.TestCase):
    def setUp(self):
        self.r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0,
                     candidates=[cand("CAND")])

    def test_a_ticker_never_held_gets_a_buy_decision(self):
        self.assertIn("CAND", self.r["by"])
        d = self.r["by"]["CAND"]
        self.assertEqual(d.action, "BUY")
        self.assertEqual(d.current_shares, 0.0)
        self.assertGreater(d.target_shares, 0)
        self.assertEqual(d.binding_rule, entry.RULE_BUY)

    def test_it_is_not_silently_dropped(self):
        self.assertTrue([x for x in self.r["trace"]
                         if x["ticker"] == "CAND" and x["pass"] == "C"])

    def test_the_buy_enters_the_final_book(self):
        self.assertGreater(self.r["by"]["CAND"].target_value, 0)

    def test_exactly_one_decision_per_ticker(self):
        names = [d.ticker for d in self.r["ds"]]
        self.assertEqual(len(names), len(set(names)))


class T3_CashGate(unittest.TestCase):
    def test_negative_cash_produces_no_buy(self):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=-5000.0, candidates=[cand("CAND")])
        self.assertNotIn("BUY", [d.action for d in r["ds"]])
        w = [p for p in r["props"] if p.rule_id == entry.RULE_WATCH]
        self.assertTrue(w)
        self.assertIn("不依赖本轮建议卖出的预计回款", w[0].reason)

    def test_planned_sale_proceeds_do_not_unlock_a_buy(self):
        """The book is over its hard cap, so this report proposes a large sale. Cash is still
        negative on the statement, so no BUY may be produced against those proceeds."""
        pos = copy.deepcopy(HOLD)
        pos["BIG"] = {"shares": 500.0, "price": 100.0, "market_value": 50000.0,
                      "cost_price": 100.0, "stop_level": 80.0, "rs": 1.0}
        layers = dict(LAYERS, BIG="U")
        r = run(pos, cfg(layers=layers), layers, cash=-9186.07, candidates=[cand("CAND")])
        self.assertEqual(r["by"]["BIG"].action, "SELL")
        self.assertGreater(r["by"]["BIG"].sell_value, 19000)
        self.assertNotIn("CAND", r["by"])

    def test_zero_cash_is_also_blocked(self):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=0.0, candidates=[cand("CAND")])
        self.assertNotIn("CAND", r["by"])


class T4_HeatGate(unittest.TestCase):
    def test_heat_at_or_above_six_blocks_the_buy(self):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0,
                candidates=[cand("CAND")], heat=14.5)
        self.assertNotIn("CAND", r["by"])
        w = [p for p in r["props"] if p.rule_id == entry.RULE_WATCH]
        self.assertIn("暂停新增风险", w[0].reason)

    def test_heat_below_six_does_not_block(self):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0,
                candidates=[cand("CAND")], heat=3.0)
        self.assertEqual(r["by"]["CAND"].action, "BUY")


class T5_VerificationGate(unittest.TestCase):
    def _watch(self, c):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0, candidates=[c])
        self.assertNotIn("CAND", r["by"])
        return [p for p in r["props"] if p.rule_id == entry.RULE_WATCH][0]

    def test_missing_verification_yields_watch_only(self):
        p = self._watch(cand("CAND", verify=False))
        self.assertEqual(p.kind, "review")
        self.assertIsNone(p.target_value)
        self.assertIn("必要条件未核实", p.reason)
        for k in entry.VERIFY_KEYS:
            self.assertIn(k, p.reason)

    def test_a_partially_verified_candidate_is_still_watch(self):
        v = verified()
        del v["vcp"]
        p = self._watch(cand("CAND", verification=v))
        self.assertIn("vcp", p.reason)

    def test_an_expired_verification_is_not_verification(self):
        p = self._watch(cand("CAND", verification=verified("2025-01-01")))
        self.assertIn("必要条件未核实", p.reason)

    def test_a_watch_carries_no_shares_and_no_amount(self):
        p = self._watch(cand("CAND", verify=False))
        self.assertNotIn("shares", p.evidence)
        self.assertIsNone(p.target_value)
        self.assertFalse(re.search(r"\d+\s*股", p.reason))


class T6_SizingIsTheMinimumOfEveryCeiling(unittest.TestCase):
    """price 50, stop 46 -> per-share risk 4 -> 1% of 100,000 buys 12,500 of stock."""

    def _buy(self, cash=999999.0, theme_pct=40, cap=30000, held_mv=10000.0, price=50.0):
        pos = {"HELD": {"shares": held_mv / 100.0, "price": 100.0, "market_value": held_mv,
                        "cost_price": 100.0, "stop_level": 80.0, "rs": 1.0}}
        c = cfg(theme_pct=theme_pct, layers=LAYERS, cap=cap)
        r = run(pos, c, LAYERS, cash=cash, candidates=[cand("CAND", price=price)])
        return r["by"].get("CAND"), r

    def test_risk_budget_binds(self):
        d, _ = self._buy()
        self.assertAlmostEqual(d.target_value, 12500.0, delta=50.0)
        self.assertEqual(d.binding_rule, entry.RULE_BUY)

    def test_hard_cap_binds(self):
        d, _ = self._buy(cap=5000)
        self.assertLessEqual(d.target_value, 5000.0)

    def test_theme_headroom_binds(self):
        """Ceiling 40% of 100,000 = 40,000; HELD occupies 28,000 (under the 30,000 hard cap,
        so nothing sells) -> 12,000 of headroom, tighter than the 12,500 risk budget."""
        d, _ = self._buy(held_mv=28000.0)
        self.assertAlmostEqual(d.target_value, 12000.0, delta=50.0)

    def test_headroom_is_measured_on_the_post_sell_book(self):
        """HELD at 38,000 is cut to the 30,000 hard cap first. The entry then sees 10,000 of
        headroom, not 2,000 -- sizing a buy against exposure that is being sold would either
        starve the buy or, worse, size it against a book that no longer exists."""
        d, r = self._buy(held_mv=38000.0)
        self.assertEqual(r["by"]["HELD"].action, "SELL")
        self.assertAlmostEqual(r["by"]["HELD"].target_value, 30000.0, delta=1.0)
        self.assertAlmostEqual(d.target_value, 10000.0, delta=50.0)

    def test_cash_binds(self):
        d, _ = self._buy(cash=3000.0)
        self.assertLessEqual(d.target_value, 3000.0)

    def test_the_binding_cap_is_named_in_the_evidence(self):
        _d, r = self._buy(cash=3000.0)
        p = [x for x in r["props"] if x.rule_id == entry.RULE_BUY][0]
        self.assertEqual(p.evidence["binding_cap"], "available_cash")
        self.assertEqual(set(p.evidence["caps"]),
                         {"risk_budget_1%", "single_name_hard_cap", "theme_headroom",
                          "available_cash"})

    def test_never_borrows(self):
        d, _ = self._buy(cash=3000.0)
        self.assertLessEqual(d.target_value, 3000.0)

    def test_evidence_carries_everything_required(self):
        _d, r = self._buy()
        p = [x for x in r["props"] if x.rule_id == entry.RULE_BUY][0]
        for k in ("source", "as_of", "entry_price", "stop", "layer", "verification",
                  "shares", "caps", "binding_cap", "risk_usd", "invalidation"):
            self.assertIn(k, p.evidence, "entry proposal missing %s" % k)


class T7_MultipleCandidates(unittest.TestCase):
    def test_cumulative_buys_never_exceed_cash_and_order_is_deterministic(self):
        cands = [cand("CAND2", score=20.0), cand("CAND", score=30.0)]
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=10000.0, candidates=cands)
        buys = [d for d in r["ds"] if d.action == "BUY"]
        self.assertLessEqual(sum(d.target_value for d in buys), 10000.0 + 0.01)
        self.assertEqual(buys[0].ticker, "CAND", "higher score is funded first")
        again = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=10000.0,
                    candidates=list(reversed(cands)))
        self.assertEqual([d.ticker for d in again["ds"] if d.action == "BUY"],
                         [d.ticker for d in buys], "input order must not change the outcome")

    def test_the_unfunded_candidate_becomes_a_watch_not_a_tiny_order(self):
        cands = [cand("CAND", score=30.0), cand("CAND2", score=20.0)]
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=10000.0, candidates=cands)
        w = [p for p in r["props"] if p.ticker == "CAND2" and p.kind == "review"]
        self.assertTrue(w)


class T8_ReentryUsesTheSamePath(unittest.TestCase):
    def test_a_verified_reentry_produces_a_buy_decision(self):
        re_ = {"ticker": "BACK", "price": 50.0, "ma50": 45.0, "exit_date": "2026-08-01",
               "verification": verified()}
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0, reentries=[re_])
        d = r["by"]["BACK"]
        self.assertEqual(d.action, "BUY")
        self.assertEqual(d.binding_rule, entry.RULE_REENTRY)
        p = [x for x in r["props"] if x.rule_id == entry.RULE_REENTRY][0]
        self.assertEqual(p.evidence["source"], "reentry_watch")

    def test_an_unverified_reentry_is_a_watch(self):
        re_ = {"ticker": "BACK", "price": 50.0, "ma50": 45.0, "exit_date": "2026-08-01"}
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0, reentries=[re_])
        self.assertNotIn("BACK", r["by"])
        self.assertTrue([p for p in r["props"] if p.rule_id == entry.RULE_REENTRY_WATCH])


class T9_AlreadyHeldIsNotANewEntry(unittest.TestCase):
    def test_a_held_ticker_never_becomes_a_buy(self):
        r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0,
                candidates=[cand("HELD", price=100.0)])
        self.assertEqual(r["by"]["HELD"].action, "HOLD")
        self.assertEqual([p for p in r["props"]
                          if p.ticker == "HELD" and p.rule_id == entry.RULE_BUY], [])


class T10_NoExecutableNumbersOutsideTheDecisionLayer(unittest.TestCase):
    FORBIDDEN = [r"可考虑买.*股", r"示例\s*%s?\s*股", r"1%风险示例股数",
                 r"若再入场按.*股", r"size_1pct_stop8"]

    def _src(self, *parts):
        return open(os.path.join(ROOT, *parts), encoding="utf-8").read()

    def test_daily_brief_renderers_carry_no_order_numbers(self):
        src = self._src("cockpit", "daily_brief.py")
        for fn in ("_followups_md", "_candidates_md"):
            body = src.split("def %s(" % fn, 1)[1].split("\ndef ", 1)[0]
            for pat in self.FORBIDDEN:
                self.assertIsNone(re.search(pat, body), "%s still prints %r" % (fn, pat))
            self.assertNotIn("止损=入场", body)

    def test_the_example_size_field_is_gone_everywhere(self):
        for parts in (("cockpit", "daily_brief.py"), ("cockpit", "scanner.py"),
                      ("cockpit", "screener.py")):
            self.assertNotIn("size_1pct_stop8", self._src(*parts), "%s" % parts[-1])

    def test_scanner_output_has_no_share_counts(self):
        src = self._src("cockpit", "scanner.py")
        self.assertIsNone(re.search(r"股数", src))

    def test_the_llm_prompt_forbids_orders(self):
        src = self._src("cockpit", "daily_brief.py")
        self.assertIn("Never output buy/sell orders", src)

    def test_action_list_is_the_only_renderer_with_order_hints(self):
        al = self._src("cockpit", "render", "action_list.py")
        self.assertIn("order_hint", al)
        self.assertNotIn("order_hint", self._src("cockpit", "daily_brief.py"))


class T11_FrozenLiveBookHasNoBuy(unittest.TestCase):
    def test_negative_cash_book_sells_only(self):
        r = harness.build()
        self.assertAlmostEqual(r["total_sell"], 31842.02, delta=1.0)
        self.assertEqual([d.ticker for d in r["decisions"] if d.action == "BUY"], [])
        self.assertLess(r["cash"], 0)


class T12_PositiveCashSyntheticBook(unittest.TestCase):
    """The end-to-end proof that a complete BUY Decision can be produced."""

    def setUp(self):
        self.r = run(copy.deepcopy(HOLD), CFG, LAYERS, cash=20000.0, heat=3.0,
                     candidates=[cand("CAND")])
        self.d = self.r["by"]["CAND"]

    def test_one_buy_decision_with_every_field(self):
        self.assertEqual(self.d.action, "BUY")
        self.assertEqual(self.d.current_shares, 0.0)
        self.assertEqual(self.d.target_shares, 250.0)
        self.assertEqual(self.d.delta_shares, 250.0)
        self.assertAlmostEqual(self.d.target_value, 12500.0, delta=1.0)
        self.assertEqual(self.d.binding_rule, entry.RULE_BUY)
        self.assertTrue(self.d.invalidation_conditions)
        self.assertIn("买入", self.d.order_hint)
        self.assertEqual(self.d.as_of, TODAY)
        self.assertAlmostEqual(self.d.expected_risk_usd, 1000.0, delta=1.0,
                               msg="a new entry must carry its at-risk dollars, not a dash")

    def test_it_renders_through_the_action_list(self):
        from cockpit.render import action_list
        reasons = {p.rule_id: p.reason for p in self.r["props"]}
        md = action_list.render(self.r["ds"], reasons, as_of=TODAY, net_liq=100000.0,
                                cash=20000.0, leverage_pct=0.0, risk_usd=3000.0, risk_pct=3.0,
                                buying_allowed=True, buying_reason="无约束阻止新增风险")
        self.assertIn("CAND · 买入", md)
        self.assertIn("+250 股", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
