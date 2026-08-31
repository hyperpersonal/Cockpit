"""Frozen end-to-end resolution of the 2026-08-27 book.

This is the regression anchor for the refactor: same book in, same decisions out.
If a later change moves any number here, that change is a RULE change and must be
argued for on its own, not slipped in as a "refactor".

What the same book produced BEFORE the unified Decision layer (2026-08-28 email):
    three sell totals -- 72,858 / 86,442 / 109,543
    SKHY carried 35,911 in two sections and 40,154 in two others
    KLAC / GLW / RAM ordered for full liquidation on a within-layer RS ranking alone
    a $59 profit-take on a 0.741-share stub
"""
from __future__ import annotations
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import harness  # noqa: E402


class TestResolution20260827(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = harness.build()
        cls.by = {d.ticker: d for d in cls.r["decisions"]}

    def test_only_three_sells(self):
        sells = {t: round(d.sell_value, 2) for t, d in self.by.items() if d.is_sell}
        self.assertEqual(sells, {"SKHY": 19470.82, "MU": 9057.85, "RAM": 3313.35})

    def test_one_total(self):
        self.assertAlmostEqual(self.r["total_sell"], 31842.02, places=2)

    def test_binding_rules_are_named_and_specific(self):
        self.assertEqual(self.by["SKHY"].binding_rule, "concentration.single_name_hard_cap")
        self.assertEqual(self.by["MU"].binding_rule, "concentration.single_name_hard_cap")
        self.assertEqual(self.by["RAM"].binding_rule, "concentration.theme_cap")

    def test_hard_cap_is_thirty_thousand_absolute(self):
        """User decision 2026-08-28: fixed $30,000, an absolute ceiling from total-wealth
        risk tolerance. Schwab is not managed here and does not enter the number."""
        self.assertAlmostEqual(self.by["SKHY"].target_value, 30000.00, places=2)
        self.assertAlmostEqual(self.by["MU"].target_value, 30000.00, places=2)

    def test_theme_lands_exactly_on_the_limit(self):
        cfg = harness.config()
        from cockpit.rules.concentration import leverage_of
        after = {}
        for t, d in self.by.items():
            lay = self.r["layers"][t]
            after[lay] = after.get(lay, 0.0) + (d.target_value or 0) * leverage_of(t, cfg)
        self.assertAlmostEqual(after["memory_hbm"] / self.r["net_liq"] * 100, 40.0, places=1)

    def test_weakest_in_layer_never_liquidates(self):
        """The 2026-08-28 behaviour this replaces: KLAC/GLW/RAM ordered fully liquidated
        because each was the weakest name in its layer with negative RS."""
        for t in ("KLAC", "GLW"):
            self.assertEqual(self.by[t].action, "HOLD")
            self.assertEqual(self.by[t].sell_value, 0.0)
            self.assertTrue(any("复查候选" in n for n in self.by[t].notes),
                            "%s must still be surfaced for review, just not liquidated" % t)
        self.assertNotEqual(self.by["RAM"].binding_rule, "exit.weakest_in_layer")

    def test_residual_stub_produces_no_instruction(self):
        d = self.by["MRVL"]
        self.assertEqual(d.action, "HOLD")
        self.assertTrue(any("噪声闸门" in n for n in d.notes))

    def test_every_sell_is_executable(self):
        for d in self.r["decisions"]:
            if not d.is_sell:
                continue
            self.assertLess(d.delta_shares, 0)
            self.assertIsNotNone(d.price)
            self.assertAlmostEqual(abs(d.delta_shares) * d.price, d.sell_value, delta=0.02)
            self.assertIn("卖出", d.order_hint)
            self.assertTrue(d.invalidation_conditions)
            self.assertEqual(d.as_of, "2026-08-27")

    def test_cash_projection_uses_the_same_number(self):
        self.assertAlmostEqual(self.r["cash"] + self.r["total_sell"], 22655.95, places=2)

    def test_sell_total_is_a_third_of_the_old_ladder(self):
        """Executability check, stated as a fact rather than a feeling: the old ladder
        asked for 109,543 on a 159,528 book (69% of NAV)."""
        self.assertLess(self.r["total_sell"] / self.r["net_liq"], 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
