"""Safety boundaries and data-freshness labelling.

These encode agreements with the user rather than implementation choices, which is exactly
why a structural refactor is when they get lost. Each is asserted against real code/config.
"""
from __future__ import annotations
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import invariants  # noqa: E402
from cockpit import daily_brief as db  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
ENGINE_DIRS = ["domain", "engine", "rules", "render", "ledger"]


def engine_sources():
    out = {}
    for sub in ENGINE_DIRS:
        d = os.path.join(ROOT, "cockpit", sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".py"):
                out["%s/%s" % (sub, f)] = open(os.path.join(d, f), encoding="utf-8").read()
    return out


class TestSafetyBoundaries(unittest.TestCase):
    def test_no_order_or_transfer_calls_anywhere(self):
        for name, src in engine_sources().items():
            for bad in ("place_order", "submit_order", "cancel_order", "transfer_funds"):
                self.assertNotIn(bad, src, "%s: the system proposes, it never trades" % name)

    def test_ibkr_mascot_stays_excluded(self):
        self.assertIn("IBKR", CFG.get("exclude") or [])

    def test_leveraged_etf_ceiling_is_configured_and_enforced(self):
        rk = CFG["risk"]
        self.assertTrue(rk.get("leveraged_etf_max_pct_nav"))
        self.assertEqual(rk["leverage_factors"].get("RAM"), 2.0)
        from cockpit.rules import account
        pos = {"RAM": {"market_value": 20000.0}, "MU": {"market_value": 10000.0}}
        props = account.propose(pos, 100000.0, 0.0, CFG)
        caps = [p for p in props if p.rule_id == account.RULE_LEV_CAP]
        self.assertTrue(caps, "20% of NAV in a 2x ETF must hit the 5% ceiling")
        self.assertEqual(caps[0].ticker, "RAM")
        self.assertAlmostEqual(caps[0].target_value, 5000.0, places=2)

    def test_leverage_counts_at_its_factor_in_theme_exposure(self):
        from cockpit.rules.concentration import leverage_of
        self.assertEqual(leverage_of("RAM", CFG), 2.0)
        self.assertEqual(leverage_of("MU", CFG), 1.0)

    def test_schwab_and_tax_stay_out_of_the_engine(self):
        forbidden = ["QQQ 定投", "回调子弹", "嘉信", "税务策略", "资本利得税"]
        for name, src in engine_sources().items():
            self.assertEqual(invariants.no_out_of_scope_advice(src, forbidden), [],
                             "out-of-scope content in %s" % name)

    def test_intraday_path_is_not_imported_by_the_engine(self):
        for name, src in engine_sources().items():
            self.assertNotIn("intraday_alert", src, "%s must not reach the disabled path" % name)

    def test_the_pre_b48_stop_formula_never_enters_the_engine(self):
        """The intraday module still carries max(200DMA, cost x 0.8). One system, one stop."""
        import re
        for name, src in engine_sources().items():
            code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
            self.assertIsNone(re.search(r"avg\s*\*\s*0\.8", code), "%s" % name)
        intraday = open(os.path.join(ROOT, "cockpit", "intraday_alert.py"), encoding="utf-8").read()
        self.assertIn("avg * 0.8", intraday,
                      "sanity: the old formula does still live in the disabled module")


class TestDataFreshnessIsStated(unittest.TestCase):
    def test_a_lagged_snapshot_says_so(self):
        w = db._staleness("2026-08-27", "2026-08-28", {}, "postmarket")
        self.assertTrue(any("持仓数据滞后" in x for x in w))
        self.assertTrue(any("不是实时的" in x for x in w))

    def test_a_same_day_snapshot_does_not_cry_wolf(self):
        w = db._staleness("2026-08-28", "2026-08-28", {}, "postmarket")
        self.assertFalse(any("持仓数据滞后" in x for x in w))

    def test_after_hours_price_gap_is_always_stated(self):
        for as_of in ("2026-08-27", "2026-08-28"):
            w = db._staleness(as_of, "2026-08-28", {}, "postmarket")
            self.assertTrue(any("不含盘后" in x for x in w))

    def test_earnings_after_the_data_timestamp_downgrades_that_name(self):
        """The real 2026-08-27 case: MRVL reported after the close, and the brief's price was
        the regular-session close."""
        w = db._staleness("2026-08-27", "2026-08-28", {"MRVL": {"date": "2026-08-27"}}, "postmarket")
        line = [x for x in w if "财报" in x]
        self.assertTrue(line)
        self.assertIn("MRVL", line[0])
        self.assertIn("降级为参考", line[0])

    def test_earnings_before_the_data_timestamp_is_not_flagged(self):
        w = db._staleness("2026-08-27", "2026-08-28", {"MRVL": {"date": "2026-08-20"}}, "postmarket")
        self.assertFalse([x for x in w if "财报" in x])

    def test_the_renderer_prints_the_staleness_rows(self):
        from cockpit.render import action_list
        md = action_list.header("2026-08-27", "", 159528.31, -9186.07, 3.3, 23060.0, 14.5,
                                False, "保证金使用中", 31842.02,
                                staleness=db._staleness("2026-08-27", "2026-08-28",
                                                        {"MRVL": {"date": "2026-08-27"}},
                                                        "postmarket"))
        self.assertIn("数据新鲜度", md)
        self.assertIn("持仓数据滞后", md)
        self.assertIn("MRVL", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
