"""Theme-concentration allocation, scheme (a) -- user decision 2026-08-28.

    1. fixed $30,000 single-name hard cap first
    2. recompute the theme's leverage-adjusted exposure
    3. still over 40%? cut the LEVERAGED members first ($1 sold removes $leverage of exposure)
    4. leveraged members at zero and still over? cut the ordinary members pro-rata by their
       share of the remaining theme exposure

RS is observation only. It may not decide a binding amount, an ordering, or an exit.
"""
from __future__ import annotations
import copy, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import harness  # noqa: E402
from cockpit.rules import concentration, account  # noqa: E402
from cockpit.engine.resolve import resolve_decisions, total_sell_value  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
NAV = 100000.0


def cfg_with(layers, leverage=None, thr=40):
    c = copy.deepcopy(CFG)
    c["risk"]["theme_overrides"] = dict(layers)
    c["risk"]["leverage_factors"] = dict(leverage or {})
    c["risk"]["theme_exposure_alert_pct"] = thr
    c["subthemes"] = {}
    return c


def exposure(targets, cfg, layers, theme):
    return sum(v * concentration.leverage_of(t, cfg)
               for t, v in targets.items() if layers[t] == theme)


class TestFrozen20260827(unittest.TestCase):
    """The agreed numbers, frozen. A change here is a rule change, not a refactor."""

    @classmethod
    def setUpClass(cls):
        cls.r = harness.build()
        cls.by = {d.ticker: d for d in cls.r["decisions"]}

    def test_targets(self):
        self.assertAlmostEqual(self.by["SKHY"].target_value, 30000.00, places=2)
        self.assertAlmostEqual(self.by["MU"].target_value, 30000.00, places=2)
        self.assertAlmostEqual(self.by["RAM"].target_value, 1905.66, delta=1.0)

    def test_total_sell(self):
        self.assertAlmostEqual(self.r["total_sell"], 31842.02, delta=1.0)

    def test_memory_hbm_lands_on_forty_percent(self):
        cfg = harness.config()
        after = {}
        for d in self.r["decisions"]:
            lay = self.r["layers"][d.ticker]
            after[lay] = after.get(lay, 0.0) + (d.target_value or 0) * concentration.leverage_of(
                d.ticker, cfg)
        self.assertAlmostEqual(after["memory_hbm"] / self.r["net_liq"] * 100, 40.0, places=1)

    def test_the_leveraged_member_is_the_one_the_theme_rule_touches(self):
        self.assertEqual(self.by["RAM"].binding_rule, concentration.RULE_THEME)
        for t in ("SKHY", "MU"):
            self.assertEqual(self.by[t].binding_rule, concentration.RULE_HARD_CAP)


class TestRsCannotChangeTheBindingAmount(unittest.TestCase):
    """The divergence case that decided this rule. Under weakest-RS-first, flipping RAM to the
    strongest name in its layer left the 2x ETF untouched and sold $26,097 of SKHY instead."""

    def _build_with_ram_rs(self, rs):
        book = copy.deepcopy(harness.BOOK)
        book["positions"]["RAM"]["rs"] = rs
        old = harness.BOOK
        harness.BOOK = book
        try:
            return harness.build()
        finally:
            harness.BOOK = old

    def test_flipping_ram_to_the_strongest_changes_nothing(self):
        weak = {d.ticker: round(d.sell_value, 2) for d in self._build_with_ram_rs(-22.5)["decisions"]
                if d.is_sell}
        strong = {d.ticker: round(d.sell_value, 2) for d in self._build_with_ram_rs(99.0)["decisions"]
                  if d.is_sell}
        self.assertEqual(weak, strong)
        self.assertAlmostEqual(strong["RAM"], 3313.35, delta=1.0)
        self.assertNotIn("SKHY", [t for t in strong if strong[t] > 26000])

    def test_totals_are_identical_across_the_rs_flip(self):
        a = total_sell_value(self._build_with_ram_rs(-22.5)["decisions"])
        b = total_sell_value(self._build_with_ram_rs(99.0)["decisions"])
        self.assertAlmostEqual(a, b, places=2)

    def test_the_allocation_rule_takes_no_rs_argument(self):
        import inspect
        params = list(inspect.signature(concentration.propose).parameters)
        self.assertEqual(params, ["positions", "net_liq", "cfg"])

    def test_rs_is_absent_from_the_allocation_module(self):
        src = open(os.path.join(ROOT, "cockpit", "rules", "concentration.py"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        code = code.split('"""', 2)[-1]          # drop the module docstring, which explains why
        for token in ("rs_by_ticker", '"rs"', "rs=", ".get('rs')"):
            self.assertNotIn(token, code, "RS leaked back into the allocation path: %r" % token)


class TestLeveragedFirstThenProRata(unittest.TestCase):
    LAYERS = {"LEV": "T", "A": "T", "B": "T"}
    # 20% ceiling on a 100k book so the members stay well under the $30,000 single-name cap.
    # (Fixture note: with a 40% ceiling and a $40,000 member, the HARD CAP fires first and
    # removes the theme excess by itself -- correct behaviour, but it makes the allocation
    # step untestable, which is how the first draft of these fixtures went wrong.)
    CFG = cfg_with(LAYERS, {"LEV": 2.0}, thr=20)

    def _targets(self, pos):
        props = concentration.propose(pos, NAV, self.CFG)
        t = {k: v["market_value"] for k, v in pos.items()}
        for p in sorted(props, key=lambda x: x.rule_id):
            t[p.ticker] = min(t[p.ticker], p.target_value)
        return t, props

    def test_leveraged_alone_absorbs_the_excess(self):
        """Exposure 2x5,000 + 9,000 + 8,000 = 27,000 vs a 20,000 ceiling: 7,000 to remove, and
        LEV carries 10,000 of exposure, so it absorbs all of it. Nothing else moves."""
        pos = {"LEV": {"market_value": 5000.0}, "A": {"market_value": 9000.0},
               "B": {"market_value": 8000.0}}
        t, props = self._targets(pos)
        self.assertAlmostEqual(t["A"], 9000.0, places=2)
        self.assertAlmostEqual(t["B"], 8000.0, places=2)
        self.assertAlmostEqual(t["LEV"], 1500.0, delta=1.0)     # (10,000-7,000)/2
        self.assertAlmostEqual(exposure(t, self.CFG, self.LAYERS, "T"), 20000.0, delta=1.0)
        self.assertEqual({p.ticker for p in props if p.rule_id == concentration.RULE_THEME},
                         {"LEV"})

    def test_leveraged_exhausted_then_ordinary_pro_rata(self):
        """LEV can only give $4,000 of exposure; the remaining $10,000 splits evenly because
        A and B hold equal shares of what is left."""
        pos = {"LEV": {"market_value": 1000.0}, "A": {"market_value": 12500.0},
               "B": {"market_value": 12500.0}}          # exposure 2,000+25,000 = 27,000
        t, props = self._targets(pos)
        self.assertAlmostEqual(t["LEV"], 0.0, delta=0.01, msg="leveraged member goes to zero first")
        self.assertAlmostEqual(t["A"], 10000.0, delta=1.0)      # (25,000-5,000)/2 each
        self.assertAlmostEqual(t["B"], 10000.0, delta=1.0)
        self.assertAlmostEqual(exposure(t, self.CFG, self.LAYERS, "T"), 20000.0, delta=1.0)
        self.assertEqual({p.ticker for p in props if p.rule_id == concentration.RULE_THEME},
                         {"LEV", "A", "B"})

    def test_pro_rata_is_proportional_not_equal(self):
        """Unequal ordinary members must be cut in proportion, not by the same dollar amount."""
        pos = {"LEV": {"market_value": 1000.0}, "A": {"market_value": 24000.0},
               "B": {"market_value": 6000.0}}           # exposure 2,000+30,000 = 32,000
        t, _ = self._targets(pos)
        self.assertAlmostEqual(t["LEV"], 0.0, delta=0.01)
        cut_a, cut_b = 24000.0 - t["A"], 6000.0 - t["B"]
        self.assertGreater(cut_b, 0, "both ordinary members must be cut")
        self.assertAlmostEqual(cut_a / cut_b, 4.0, places=2)    # 24,000 : 6,000
        self.assertAlmostEqual(exposure(t, self.CFG, self.LAYERS, "T"), 20000.0, delta=1.0)

    def test_a_theme_inside_its_ceiling_produces_nothing(self):
        pos = {"LEV": {"market_value": 1000.0}, "A": {"market_value": 8000.0},
               "B": {"market_value": 8000.0}}           # exposure 18,000 < 20,000
        props = [p for p in concentration.propose(pos, NAV, self.CFG)
                 if p.rule_id == concentration.RULE_THEME]
        self.assertEqual(props, [])

    def test_exactly_at_the_ceiling_produces_nothing(self):
        pos = {"LEV": {"market_value": 5000.0}, "A": {"market_value": 10000.0}}  # 10k+10k = 20k
        props = [p for p in concentration.propose(pos, NAV, self.CFG)
                 if p.rule_id == concentration.RULE_THEME]
        self.assertEqual(props, [])


class TestHigherPriorityRulesWin(unittest.TestCase):
    """Rule 6: the theme allocation may not override account safety, a verified structural
    veto, the leveraged hard stop, or the single-name hard cap."""

    LAYERS = {"RAM": "memory_hbm", "SKHY": "memory_hbm"}
    CFG = cfg_with(LAYERS, {"RAM": 2.0})
    SMALL_NAV = 80000.0      # so the theme is genuinely over its ceiling AFTER the hard cap

    def test_leveraged_hard_stop_beats_the_theme_allocation_and_leaves_one_decision(self):
        pos = {"RAM": {"shares": 413.2232, "price": 10.28, "market_value": 4247.90,
                       "cost_price": 12.10, "stop_level": 9.88},
               "SKHY": {"shares": 306.1124, "price": 161.61, "market_value": 49470.82,
                        "cost_price": 163.18, "stop_level": 133.94}}
        NAV = self.SMALL_NAV
        props = (account.propose(pos, NAV, 0.0, self.CFG)
                 + concentration.propose(pos, NAV, self.CFG))
        lev = [p for p in props if p.rule_id == account.RULE_LEV_STOP]
        theme = [p for p in props if p.rule_id == concentration.RULE_THEME]
        self.assertTrue(lev and lev[0].kind == "hard_exit", "the hard stop must have fired")
        self.assertTrue(theme, "the theme rule must also have fired, so this is a real contest")

        ds = resolve_decisions([p for p in props if p.ticker != "__portfolio__"],
                               pos, NAV, "2026-08-27", self.CFG)
        by = {d.ticker: d for d in ds}
        self.assertEqual(len([d for d in ds if d.ticker == "RAM"]), 1,
                         "exactly one Decision per ticker")
        self.assertEqual(by["RAM"].action, "EXIT")
        self.assertEqual(by["RAM"].target_value, 0.0)
        self.assertEqual(by["RAM"].binding_rule, account.RULE_LEV_STOP)
        self.assertIn(concentration.RULE_THEME, by["RAM"].supporting_rules)

    def test_the_hard_cap_still_binds_when_it_is_stricter(self):
        pos = {"SKHY": {"shares": 306.1124, "price": 161.61, "market_value": 49470.82},
               "RAM": {"market_value": 100.0}}
        ds = resolve_decisions(concentration.propose(pos, NAV, self.CFG), pos, NAV,
                               "2026-08-27", self.CFG)
        by = {d.ticker: d for d in ds}
        self.assertEqual(by["SKHY"].target_value, 30000.0)
        self.assertEqual(by["SKHY"].binding_rule, concentration.RULE_HARD_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
