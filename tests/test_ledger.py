"""Performance ledger: as_of filing, TWR, attribution that keeps the exits."""
from __future__ import annotations
import json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from cockpit.ledger import performance as P  # noqa: E402

STMT = json.load(open(os.path.join(HERE, "fixtures", "ibkr_statement_20260827.json"), encoding="utf-8"))


class TestAsOfFiling(unittest.TestCase):
    def test_nav_is_filed_under_as_of_not_run_date(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "nav.json")
            d = P.append_nav_at_as_of("20260827", 159528.31, run_date="2026-08-28", path=p)
            self.assertIn("2026-08-27", d["navs"])
            self.assertNotIn("2026-08-28", d["navs"])
            self.assertEqual(d["navs"]["2026-08-27"], 159528.31)
            self.assertTrue(d["meta"]["2026-08-27"]["stale"],
                            "a figure carried over from the prior session must say so")

    def test_legacy_shape_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "nav.json")
            json.dump({"navs": {"2026-08-26": 156139.43}}, open(p, "w"))
            d = P.append_nav_at_as_of("2026-08-27", 159528.31, run_date="2026-08-28", path=p)
            self.assertEqual(d["navs"]["2026-08-26"], 156139.43)

    def test_refuses_a_missing_as_of(self):
        with self.assertRaises(ValueError):
            P.append_nav_at_as_of("", 1.0)


class TestTWR(unittest.TestCase):
    def test_a_deposit_is_not_a_gain(self):
        """The whole point. NAV doubles because money arrived; the return is zero."""
        navs = {"2026-01-01": 100000.0, "2026-01-02": 200000.0}
        r = P.twr(navs, {"2026-01-02": 100000.0}, verified_through="2026-01-02")
        self.assertEqual(r["twr_pct"], 0.0)

    def test_an_undeclared_doubling_is_refused_not_reported_as_performance(self):
        """Same series, flow file empty. A +100% one-day move with nothing on record has the
        shape of an unrecorded deposit, so the ledger refuses and names the day."""
        r = P.twr({"2026-01-01": 100000.0, "2026-01-02": 200000.0}, {},
                  verified_through="2026-01-02")
        self.assertIsNone(r["twr_pct"])
        self.assertEqual([x["date"] for x in r["suspected_flows"]], ["2026-01-02"])
        self.assertIn("2026-01-02", r["refused"])

    def test_a_move_below_the_suspicion_threshold_is_reported(self):
        r = P.twr({"2026-01-01": 100000.0, "2026-01-02": 105000.0}, {},
                  verified_through="2026-01-02")
        self.assertAlmostEqual(r["twr_pct"], 5.0, places=6)
        self.assertEqual(r["suspected_flows"], [])

    def test_daily_linking(self):
        navs = {"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-03": 99.0}
        self.assertAlmostEqual(P.twr(navs, {}, "2026-01-03")["twr_pct"], -1.0, places=6)

    def test_an_unreconciled_tail_gets_the_number_plus_a_named_caveat(self):
        """Refusing whenever the tail is unreconciled would refuse forever -- the reconciled
        date always lags. Answer, and say exactly what is unverified."""
        navs = {"2026-08-26": 156139.43, "2026-08-28": 159528.31}
        r = P.twr(navs, {}, verified_through="2026-08-27")
        self.assertIsNotNone(r["twr_pct"])
        self.assertEqual(r["unreconciled_from"], "2026-08-28")
        self.assertIn("2026-08-27", r["caveat"])
        self.assertEqual(r["suspected_flows"], [])

    def test_the_real_deposit_days_would_have_been_caught_if_unrecorded(self):
        """Reverse test on live-shaped data: drop the 160,000 from the flow file and the
        detector must point at the two real deposit dates."""
        navs = {"2026-05-27": 50000.0, "2026-05-28": 150000.0,
                "2026-06-28": 152000.0, "2026-06-29": 212000.0}
        r = P.twr(navs, {}, verified_through="2026-06-29")
        self.assertIsNone(r["twr_pct"])
        self.assertEqual([x["date"] for x in r["suspected_flows"]], ["2026-05-28", "2026-06-29"])
        ok = P.twr(navs, {"2026-05-28": 100000.0, "2026-06-29": 60000.0},
                   verified_through="2026-06-29")
        self.assertIsNotNone(ok["twr_pct"])
        self.assertEqual(ok["suspected_flows"], [])

    def test_real_flows_file_matches_the_statement(self):
        flows, through, src = P.load_cash_flows()
        self.assertAlmostEqual(sum(flows.values()), 160000.00, places=2)
        self.assertEqual(sum(flows.values()), STMT["change_in_nav"]["deposits_withdrawals"])
        self.assertEqual(through, "2026-08-27")
        self.assertTrue(src)
        for f in STMT["external_cash_flows"]:
            self.assertAlmostEqual(flows[f["date"]], f["amount"], places=2)

    def test_nav_history_alone_would_report_a_huge_fake_gain(self):
        """Documents why this module exists: the real nav_history, read naively, says +57%
        over a window in which the account was down and 160,000 was deposited."""
        navs = json.load(open(os.path.join(ROOT, "state", "nav_history.json"),
                              encoding="utf-8"))["navs"]
        ds = sorted(navs)
        naive = (navs[ds[-1]] / navs[ds[0]] - 1) * 100
        self.assertGreater(naive, 40)
        self.assertEqual(STMT["twr_pct"], -1.62)


class TestAttributionKeepsExits(unittest.TestCase):
    DAYS = [
        {"date": "2026-08-24", "net_liq": 100000.0,
         "holdings": {"AAA": {"shares": 10, "price": 100.0}, "NVDA": {"shares": 5, "price": 200.0}}},
        {"date": "2026-08-25", "net_liq": 101500.0,
         "holdings": {"AAA": {"shares": 10, "price": 110.0}, "NVDA": {"shares": 5, "price": 210.0}}},
        {"date": "2026-08-26", "net_liq": 102000.0,
         "holdings": {"AAA": {"shares": 10, "price": 115.0}}},
    ]

    def test_exited_name_is_reported_not_dropped(self):
        a = P.attribution_with_exits(self.DAYS, exit_prices={"NVDA": 228.0})
        self.assertIn("NVDA", a["exits"])
        self.assertEqual(a["exits"]["NVDA"]["basis"], "estimated")
        self.assertAlmostEqual(a["exits"]["NVDA"]["usd"], 5 * (228.0 - 210.0), places=2)
        self.assertIn("NVDA", a["names_in_pick_size"])

    def test_broker_realised_pnl_wins_over_an_estimate(self):
        a = P.attribution_with_exits(self.DAYS, exit_prices={"NVDA": 228.0},
                                     realized={"NVDA": 1444.67})
        self.assertEqual(a["exits"]["NVDA"]["basis"], "broker")
        self.assertEqual(a["exits"]["NVDA"]["usd"], 1444.67)

    def test_an_unpriceable_exit_is_unknown_never_zero(self):
        a = P.attribution_with_exits(self.DAYS)
        self.assertEqual(a["exits"]["NVDA"]["basis"], "unknown")
        self.assertIsNone(a["exits"]["NVDA"]["usd"])
        self.assertIn("NVDA", a["coverage"]["unknown"])
        self.assertIn("未能定价的离场", a["coverage"]["residual_meaning"])

    def _legacy(self):
        import cockpit.biweekly_review as B
        import pathlib, shutil, tempfile
        td = tempfile.mkdtemp()
        root = pathlib.Path(td)
        (root / "state").mkdir()
        json.dump({"days": self.DAYS}, open(root / "state" / "signal_history.json", "w"))
        old = B.ROOT
        B.ROOT = root
        try:
            return B._attribution()
        finally:
            B.ROOT = old
            shutil.rmtree(td, ignore_errors=True)

    def test_legacy_keeps_the_days_before_the_exit_but_loses_the_final_leg(self):
        """The precise defect, stated precisely. NVDA moved 200 -> 210 while held (worth 50)
        and 210 -> 228 on the way out (worth 90). The legacy attribution books the 50 and
        loses the 90 -- it is not 'zero', it is 'truncated at the exit'."""
        legacy = self._legacy()
        self.assertIsNotNone(legacy)
        self.assertAlmostEqual(dict(legacy["per"])["NVDA"], 50.0, places=2)
        new = P.attribution_with_exits(self.DAYS, exit_prices={"NVDA": 228.0})
        self.assertAlmostEqual(dict(new["per"])["NVDA"], 140.0, places=2)

    def test_the_lost_leg_is_laundered_into_residual(self):
        """And the 90 does not vanish: it lands in `residual`, which the legacy output
        labels as deposits / interest / fees. Trading P&L reported as external cash flow."""
        legacy = self._legacy()
        new = P.attribution_with_exits(self.DAYS, exit_prices={"NVDA": 228.0})
        self.assertAlmostEqual(legacy["residual"] - new["residual"], 90.0, places=2)

    def test_legacy_drops_the_exited_name_from_pick_and_size(self):
        """The pick / size buckets use `last["holdings"]`, so an exited name is gone outright."""
        src = open(os.path.join(ROOT, "cockpit", "biweekly_review.py"), encoding="utf-8").read()
        body = src.split("def _attribution(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not vN: continue", body)
        new = P.attribution_with_exits(self.DAYS, exit_prices={"NVDA": 228.0})
        self.assertIn("NVDA", new["names_in_pick_size"])


class TestEmptyBookDayIsNotDropped(unittest.TestCase):
    def test_a_fully_liquidated_day_is_real_data(self):
        days = [
            {"date": "d1", "net_liq": 100.0, "holdings": {"X": {"shares": 1, "price": 10.0}}},
            {"date": "d2", "net_liq": 101.0, "holdings": {"X": {"shares": 1, "price": 11.0}}},
            {"date": "d3", "net_liq": 102.0, "holdings": {}},
        ]
        a = P.attribution_with_exits(days, exit_prices={"X": 12.0})
        self.assertIsNotNone(a, "the day the book went flat must not be discarded")
        self.assertEqual(a["exits"]["X"]["basis"], "estimated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
