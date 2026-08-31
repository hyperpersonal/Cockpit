"""Portfolio-level sequential adjudication.

The defect these lock down (found with the real functions, 2026-08-28): every rule generated
its proposals from the ORIGINAL book, so a ceiling could size itself against exposure another
ticker was already giving up. "One Decision per ticker" did not prevent it -- flattening
conflicts per ticker says nothing about two tickers double-counting the same excess.

Every test here drives cockpit.engine.pipeline.adjudicate() with real rule modules.
"""
from __future__ import annotations
import copy, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import harness  # noqa: E402
import invariants  # noqa: E402
from cockpit.engine import pipeline  # noqa: E402
from cockpit.engine.resolve import total_sell_value  # noqa: E402
from cockpit.rules import account, concentration, exit as exit_rules, thesis  # noqa: E402

BASE = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
NAV = 100000.0
VERIFIED = "（核实 2026-08-25，源=FMP profile）"


def cfg(theme_pct=60, lev_max=100, cap=200000, layers=None, leverage=None, tickers=()):
    c = copy.deepcopy(BASE)
    c["subthemes"] = {}
    c["risk"].update(theme_overrides=dict(layers or {}), leverage_factors=dict(leverage or {}),
                     theme_exposure_alert_pct=theme_pct, leveraged_etf_max_pct_nav=lev_max,
                     single_name_hard_cap_usd=cap)
    c["holdings"] = [{"ticker": t, "role": "x" + VERIFIED} for t in tickers]
    return c


def run(pos, c, layers, cash=0.0, net_liq=NAV):
    ctx = pipeline.Context(net_liq=net_liq, cash=cash, as_of="2026-08-28", cfg=c,
                           layers=layers, stops={t: d.get("stop_level") for t, d in pos.items()})
    ds, props, trace = pipeline.adjudicate(pos, ctx)
    by = {d.ticker: d for d in ds}
    expo = pipeline.theme_exposures({d.ticker: d.target_value for d in ds}, layers, c)
    bound = {p.evidence.get("theme") for p in props
             if p.rule_id == concentration.RULE_THEME and p.evidence.get("theme")}
    return {"by": by, "props": props, "trace": trace, "expo": expo, "bound": bound,
            "total": total_sell_value(ds), "cfg": c, "layers": layers, "net_liq": net_liq}


def stock(mv, price=100.0, cost=100.0, stop=80.0, broken=False, rs=1.0):
    return {"shares": mv / price, "price": price, "market_value": mv, "cost_price": cost,
            "stop_level": stop, "already_broken_down": broken, "rs": rs}


class TestOrdinaryExitSatisfiesTheCeiling(unittest.TestCase):
    """The reported case. ORD 60,000 + LEV 10,000@2x = 80,000 exposure vs a 60,000 ceiling.
    ORD breaches its execution stop. Once it goes, the theme holds 20,000 and LEV must not be
    touched. Old engine: sell 70,000, exposure 0."""

    LAYERS = {"ORD": "T", "LEV": "T"}
    CFG = cfg(layers=LAYERS, leverage={"LEV": 2.0}, tickers=("ORD", "LEV"))
    POS = {"ORD": stock(60000.0, stop=105.0, broken=True), "LEV": stock(10000.0, price=10.0,
                                                                       cost=10.0, stop=8.0)}

    def setUp(self):
        self.r = run(copy.deepcopy(self.POS), self.CFG, self.LAYERS)

    def test_the_leveraged_member_is_not_cut(self):
        self.assertEqual(self.r["by"]["LEV"].action, "HOLD")
        self.assertEqual(self.r["by"]["LEV"].sell_value, 0.0)

    def test_the_theme_rule_never_fires_at_all(self):
        self.assertEqual([p for p in self.r["props"]
                          if p.rule_id == concentration.RULE_THEME and p.binding], [])

    def test_total_is_the_exit_and_nothing_more(self):
        self.assertAlmostEqual(self.r["total"], 60000.0, places=2)

    def test_the_exit_is_applied_before_any_sizing(self):
        a = [x for x in self.r["trace"] if x["pass"] == "A"]
        self.assertEqual([x["ticker"] for x in a], ["ORD"])
        self.assertIn("before sizing", a[0]["why"])

    def test_ceiling_respected(self):
        self.assertEqual(invariants.theme_ceiling_respected(self.r["expo"], NAV, 60), [])


class TestAccountCapAbsorbsPartOfTheExcess(unittest.TestCase):
    """Tier 1's 5%-of-NAV leveraged ceiling already removes some theme exposure. The theme rule
    may only size against what is LEFT."""

    LAYERS = {"A": "T", "LEV": "T"}
    CFG = cfg(theme_pct=40, lev_max=5, layers=LAYERS, leverage={"LEV": 2.0}, tickers=("A", "LEV"))
    POS = {"A": stock(30000.0), "LEV": stock(20000.0, price=10.0, cost=10.0, stop=1.0)}

    def setUp(self):
        self.r = run(copy.deepcopy(self.POS), self.CFG, self.LAYERS)

    def test_tier1_caps_the_leveraged_product_first(self):
        self.assertAlmostEqual(self.r["by"]["LEV"].target_value, 5000.0, delta=1.0)
        self.assertEqual(self.r["by"]["LEV"].binding_rule, account.RULE_LEV_CAP)

    def test_theme_only_handles_the_remainder(self):
        """After tier 1: 30,000 + 2x5,000 = 40,000, exactly the ceiling. Nothing left to do."""
        self.assertEqual(self.r["by"]["A"].action, "HOLD")
        self.assertEqual([p for p in self.r["props"]
                          if p.rule_id == concentration.RULE_THEME and p.binding], [])
        self.assertAlmostEqual(self.r["expo"]["T"], 40000.0, delta=1.0)

    def test_total_is_only_the_tier1_cut(self):
        self.assertAlmostEqual(self.r["total"], 15000.0, delta=1.0)


class TestLeveragedHardStopSatisfiesTheCeiling(unittest.TestCase):
    """A breached leveraged hard stop removes the whole leveraged position. If that alone
    brings the theme under its ceiling, no other member may be touched."""

    LAYERS = {"A": "T", "LEV": "T"}
    CFG = cfg(theme_pct=60, layers=LAYERS, leverage={"LEV": 2.0}, tickers=("A", "LEV"))
    # LEV: cost 10 -> -15% = 8.50; price 8.40 -> triggered. 20,000 mv = 40,000 exposure.
    POS = {"A": stock(50000.0), "LEV": stock(20000.0, price=8.40, cost=10.0, stop=5.0)}

    def setUp(self):
        self.r = run(copy.deepcopy(self.POS), self.CFG, self.LAYERS)

    def test_the_leveraged_stop_fired(self):
        self.assertEqual(self.r["by"]["LEV"].action, "EXIT")
        self.assertEqual(self.r["by"]["LEV"].binding_rule, account.RULE_LEV_STOP)

    def test_the_ordinary_member_is_untouched(self):
        self.assertEqual(self.r["by"]["A"].action, "HOLD")
        self.assertEqual(self.r["by"]["A"].sell_value, 0.0)

    def test_ceiling_respected_and_nothing_extra_sold(self):
        self.assertEqual(invariants.theme_ceiling_respected(self.r["expo"], NAV, 60), [])
        self.assertAlmostEqual(self.r["total"], 20000.0, places=2)


class TestNoDoubleCountingAcrossTickers(unittest.TestCase):
    """One ticker exits on a high-priority rule while another is the theme rule's target. The
    excess must be counted once."""

    LAYERS = {"A": "T", "B": "T", "LEV": "T"}
    CFG = cfg(theme_pct=40, layers=LAYERS, leverage={"LEV": 2.0}, tickers=("A", "B", "LEV"))
    # A 25,000 (stop-breached) + B 25,000 + LEV 5,000@2x=10,000 -> 60,000 vs 40,000 ceiling.
    # A exits (25,000), leaving 35,000 -- under the ceiling. Nothing else should move.
    POS = {"A": stock(25000.0, stop=105.0, broken=True), "B": stock(25000.0),
           "LEV": stock(5000.0, price=10.0, cost=10.0, stop=1.0)}

    def setUp(self):
        self.r = run(copy.deepcopy(self.POS), self.CFG, self.LAYERS)

    def test_only_the_exiting_ticker_sells(self):
        self.assertEqual(self.r["by"]["A"].action, "EXIT")
        for t in ("B", "LEV"):
            self.assertEqual(self.r["by"][t].action, "HOLD", "%s must not move" % t)
        self.assertAlmostEqual(self.r["total"], 25000.0, places=2)

    def test_the_excess_is_counted_once(self):
        self.assertAlmostEqual(self.r["expo"]["T"], 35000.0, delta=1.0)
        self.assertEqual(invariants.theme_ceiling_respected(self.r["expo"], NAV, 40), [])

    def test_theme_still_binds_when_the_exit_is_not_enough(self):
        """Same book, but A is small: its exit leaves the theme still over, and only the
        REMAINING excess may be taken -- from the leveraged member first."""
        pos = {"A": stock(5000.0, stop=105.0, broken=True), "B": stock(35000.0),
               "LEV": stock(10000.0, price=10.0, cost=10.0, stop=1.0)}
        r = run(pos, self.CFG, self.LAYERS)          # 5,000+35,000+20,000 = 60,000 vs 40,000
        self.assertEqual(r["by"]["A"].action, "EXIT")          # -5,000 -> 55,000
        self.assertAlmostEqual(r["by"]["LEV"].target_value, 2500.0, delta=1.0)   # -15,000
        self.assertEqual(r["by"]["B"].action, "HOLD")
        self.assertAlmostEqual(r["expo"]["T"], 40000.0, delta=1.0)
        self.assertAlmostEqual(r["total"], 5000.0 + 7500.0, delta=1.0)


class TestPostConditions(unittest.TestCase):
    """Applied to every scenario in this file plus the frozen live book."""

    def _check(self, r, thr):
        self.assertEqual(invariants.theme_ceiling_respected(r["expo"], r["net_liq"], thr), [])
        self.assertEqual(invariants.no_unnecessary_theme_cut(
            r["expo"], r["net_liq"], thr, r["bound"]), [])
        tickers = [d.ticker for d in r["by"].values()]
        self.assertEqual(len(tickers), len(set(tickers)))

    def test_synthetic_scenarios(self):
        for case, thr in ((TestOrdinaryExitSatisfiesTheCeiling, 60),
                          (TestAccountCapAbsorbsPartOfTheExcess, 40),
                          (TestLeveragedHardStopSatisfiesTheCeiling, 60),
                          (TestNoDoubleCountingAcrossTickers, 40)):
            r = run(copy.deepcopy(case.POS), case.CFG, case.LAYERS)
            self._check(r, thr)

    def test_the_live_2026_08_27_book(self):
        r = harness.build()
        c = harness.config()
        expo = pipeline.theme_exposures({d.ticker: d.target_value for d in r["decisions"]},
                                        r["layers"], c)
        bound = {p.evidence.get("theme") for p in r["proposals"]
                 if p.rule_id == concentration.RULE_THEME and p.evidence.get("theme")}
        thr = float(c["risk"]["theme_exposure_alert_pct"])
        self.assertEqual(invariants.theme_ceiling_respected(expo, r["net_liq"], thr), [])
        self.assertEqual(invariants.no_unnecessary_theme_cut(expo, r["net_liq"], thr, bound), [])

    def test_frozen_live_numbers_unchanged(self):
        r = harness.build()
        by = {d.ticker: d for d in r["decisions"]}
        self.assertAlmostEqual(by["SKHY"].target_value, 30000.00, places=2)
        self.assertAlmostEqual(by["MU"].target_value, 30000.00, places=2)
        self.assertAlmostEqual(by["RAM"].target_value, 1905.66, delta=1.0)
        self.assertAlmostEqual(r["total_sell"], 31842.02, delta=1.0)

    def test_rs_still_absent_from_the_allocation_path(self):
        src = open(os.path.join(ROOT, "cockpit", "rules", "concentration.py"),
                   encoding="utf-8").read()
        body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        body = body.split('"""', 2)[-1]
        for tok in ("rs_by_ticker", '"rs"', "rs="):
            self.assertNotIn(tok, body)


class TestProfitTrimDoesNotCompound(unittest.TestCase):
    """A name a higher tier already reduced is not trimmed again -- taking a third of what is
    left after a concentration cut is a second bite at the same position."""

    LAYERS = {"A": "T", "B": "T"}
    CFG = cfg(theme_pct=40, cap=30000, layers=LAYERS, tickers=("A", "B"))

    def test_no_second_bite(self):
        pos = {"A": stock(30000.0, price=100.0, cost=50.0),      # +100% -> profit rule fires
               "B": stock(25000.0)}
        r = run(pos, self.CFG, self.LAYERS)                      # 55,000 vs 40,000 ceiling
        a = r["by"]["A"]
        self.assertNotEqual(a.binding_rule, "profit.take_ladder")
        skipped = [x for x in r["trace"] if x["pass"] == "C" and "skipped" in x["why"]]
        self.assertTrue(skipped, "the trim must be recorded as skipped, not silently dropped")
        self.assertAlmostEqual(r["expo"]["T"], 40000.0, delta=1.0)

class TestProfitTakeSamePeriodRule(unittest.TestCase):
    """User decision 2026-08-28, formalised.

    Same report period, same ticker: if a tier 1-5 rule already produced a reduction or exit,
    a profit-take may NOT be stacked on top of it this round. It stays a supporting reason and
    is re-evaluated in the next report, against the book the execution actually leaves. This is
    "no double sell in one round", not a permanent exemption.
    """

    LAYERS = {"A": "T", "B": "T"}

    def _cfg(self, cap=30000, theme_pct=40):
        return cfg(theme_pct=theme_pct, cap=cap, layers=self.LAYERS, tickers=("A", "B"))

    # (1) nothing higher fired -> the profit take binds normally
    def test_profit_binds_when_no_higher_tier_reduced(self):
        pos = {"A": stock(20000.0, price=100.0, cost=50.0),      # +100%
               "B": stock(10000.0)}                              # theme 30,000 < 40,000
        r = run(pos, self._cfg(), self.LAYERS)
        d = r["by"]["A"]
        self.assertEqual(d.binding_rule, "profit.take_ladder")
        self.assertAlmostEqual(d.target_value, 20000.0 * (1 - 0.33), delta=1.0)
        self.assertEqual(r["by"]["B"].action, "HOLD")

    # (2) a higher tier reduced the same ticker -> skipped THIS round, kept as supporting
    def test_profit_is_skipped_but_retained_as_supporting_when_a_higher_tier_cut(self):
        pos = {"A": stock(40000.0, price=100.0, cost=50.0),      # over the 30,000 hard cap
               "B": stock(10000.0)}
        r = run(pos, self._cfg(), self.LAYERS)
        d = r["by"]["A"]
        self.assertEqual(d.binding_rule, concentration.RULE_HARD_CAP)
        self.assertAlmostEqual(d.target_value, 30000.0, delta=1.0)
        self.assertIn("profit.take_ladder", d.supporting_rules,
                      "the profit rule must survive as a supporting reason, not vanish")
        skipped = [x for x in r["trace"] if x["pass"] == "C" and "skipped" in x["why"]]
        self.assertTrue(skipped)
        self.assertIn("next report", skipped[0]["why"])

    # (3) next report, on the book the execution left -> the profit take may bind again
    def test_next_report_on_the_executed_book_lets_profit_bind_again(self):
        after = {"A": stock(30000.0, price=100.0, cost=50.0),    # now exactly at the cap
                 "B": stock(10000.0)}
        r = run(after, self._cfg(), self.LAYERS)
        d = r["by"]["A"]
        self.assertEqual(d.binding_rule, "profit.take_ladder")
        self.assertAlmostEqual(d.target_value, 30000.0 * (1 - 0.33), delta=1.0)

    # HARD_EXIT always wins, in any round
    def test_a_hard_exit_always_beats_the_profit_take(self):
        pos = {"A": stock(20000.0, price=100.0, cost=50.0, stop=105.0, broken=True),
               "B": stock(10000.0)}
        r = run(pos, self._cfg(), self.LAYERS)
        d = r["by"]["A"]
        self.assertEqual(d.action, "EXIT")
        self.assertEqual(d.target_value, 0.0)
        self.assertEqual(d.binding_rule, exit_rules.RULE_STOP)
        self.assertIn("profit.take_ladder", d.supporting_rules)

if __name__ == "__main__":
    unittest.main(verbosity=2)
