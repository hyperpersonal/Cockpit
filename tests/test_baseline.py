"""Golden baseline: the broker's own numbers, frozen.

These MUST PASS at every point of the refactor. If one of them goes red, the
refactor broke the account arithmetic, not a rendering detail.

Source of truth: tests/fixtures/ibkr_statement_20260827.json, transcribed from
IBKR Activity Statement U22209151 (2025-11-03 .. 2026-08-27).
"""
from __future__ import annotations
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import invariants  # noqa: E402

STMT = json.load(open(os.path.join(HERE, "fixtures", "ibkr_statement_20260827.json"), encoding="utf-8"))


class TestStatementBaseline(unittest.TestCase):
    """Frozen account facts. Numbers a Cockpit computation must reproduce."""

    def test_nav_cash_stock(self):
        n = STMT["nav"]
        self.assertEqual(n["total"], 159528.31)
        self.assertEqual(n["cash"], -9186.07)
        self.assertEqual(n["stock"], 168877.45)
        self.assertEqual(n["settled_cash"], -23644.30)

    def test_nav_identity_includes_accruals(self):
        """The defect this pins: NAV != stock + cash. The gap is exactly the accruals.
        168,877.45 - 9,186.07 = 159,691.38, which is 163.07 ABOVE the real NAV."""
        n = STMT["nav"]
        self.assertEqual(invariants.nav_identity(
            n["stock"], n["cash"], n["interest_accruals"], n["dividend_accruals"], n["total"]), [])
        naive = n["stock"] + n["cash"]
        self.assertAlmostEqual(naive - n["total"], 163.07, places=2,
                               msg="the stock+cash shortcut is off by exactly the accruals")

    def test_position_shares_and_values(self):
        pos = STMT["open_positions"]
        self.assertEqual(len(pos), 12, "12 positions including the unvested IBKR grant")
        for t, p in pos.items():
            self.assertAlmostEqual(p["qty"] * p["close"], p["value"], delta=0.02,
                                   msg="%s: qty x close must equal value" % t)
            self.assertAlmostEqual(p["value"] - p["cost_basis"], p["unrealized"], delta=0.02,
                                   msg="%s: value - cost must equal unrealized" % t)
        self.assertAlmostEqual(sum(p["value"] for p in pos.values()),
                               STMT["open_positions_total"]["value"], delta=0.05)
        self.assertAlmostEqual(sum(p["cost_basis"] for p in pos.values()),
                               STMT["open_positions_total"]["cost_basis"], delta=0.05)

    def test_active_book_is_eleven_names(self):
        """IBKR-the-stock is an unvested grant, excluded from the active book."""
        active = {t: p for t, p in STMT["open_positions"].items() if t != "IBKR"}
        self.assertEqual(len(active), 11)
        self.assertAlmostEqual(sum(p["value"] for p in active.values()), 168283.34, delta=1.0)

    def test_memory_hbm_exposure_leverage_adjusted(self):
        """RAM is a 2x DRAM ETF: it enters theme exposure at 2x its market value (red-line v2)."""
        p = STMT["open_positions"]
        expo = p["SKHY"]["value"] + p["MU"]["value"] + p["RAM"]["value"] * 2
        self.assertAlmostEqual(expo, 98966.71, delta=1.0)
        self.assertAlmostEqual(expo / STMT["nav"]["total"] * 100, 62.0, delta=0.1)

    def test_external_cash_flows(self):
        flows = STMT["external_cash_flows"]
        self.assertAlmostEqual(sum(f["amount"] for f in flows), 160000.00, delta=0.01)
        self.assertEqual(sum(f["amount"] for f in flows),
                         STMT["change_in_nav"]["deposits_withdrawals"])

    def test_twr_is_negative_while_raw_nav_looks_positive(self):
        """The whole reason TWR is required: NAV went 9,968 -> 159,528 while the account LOST money."""
        c = STMT["change_in_nav"]
        self.assertEqual(STMT["twr_pct"], -1.62)
        self.assertLess(c["mark_to_market"], 0)
        raw_nav_change_pct = (c["ending_value"] / c["starting_value"] - 1) * 100
        self.assertGreater(raw_nav_change_pct, 1400, "raw NAV change is +1500%, and it is meaningless")

    def test_change_in_nav_reconciles(self):
        c = STMT["change_in_nav"]
        parts = sum(v for k, v in c.items() if k not in ("starting_value", "ending_value"))
        self.assertAlmostEqual(c["starting_value"] + parts, c["ending_value"], delta=0.02)

    def test_realized_pnl_of_exited_names(self):
        r = STMT["realized_pnl"]
        self.assertAlmostEqual(sum(r.values()), STMT["realized_total"], delta=0.05)
        self.assertEqual(r["NVDA"], 1444.67)
        self.assertEqual(r["SPCX"], 1120.45)
        for t in STMT["fully_exited_during_period"]:
            self.assertIn(t, r, "%s exited during the period and must carry realized P&L" % t)

    def test_total_pnl_reconciles(self):
        self.assertAlmostEqual(STMT["realized_total"] + STMT["unrealized_total"],
                               STMT["realized_plus_unrealized"], delta=0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
