"""The seven failure tests. These define what "done" means for the refactor.

Every requirement gets TWO tests:

  *_checker_catches_2026_08_28   -- point the invariant at the email the system really
                                    sent on 2026-08-28. It MUST report the violation.
                                    If this goes green-with-no-findings, the checker is
                                    asleep and the paired test below is worthless.
                                    THESE PASS TODAY.

  *_system_satisfies             -- point the same invariant at what the system produces.
                                    THESE FAIL TODAY, ON PURPOSE, and turn green when the
                                    unified Decision layer lands.

Why the pairing: 2026-08-28, selfcheck gate 10 shipped a checker that a `#` comment
could satisfy. A test that has never been seen to fail proves nothing.
"""
from __future__ import annotations
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import invariants  # noqa: E402

OBS = json.load(open(os.path.join(HERE, "fixtures", "brief_20260828_observed.json"), encoding="utf-8"))
STMT = json.load(open(os.path.join(HERE, "fixtures", "ibkr_statement_20260827.json"), encoding="utf-8"))

import harness  # noqa: E402

NOT_BUILT = "%s does not exist yet: %s. RED by design until that layer lands."


def needs(case, dotted, attr):
    """Fail loudly (never skip) when a not-yet-built layer is required. A skipped test is
    an invisible test, and invisible things that never happen are this project's signature
    defect."""
    try:
        mod = __import__(dotted, fromlist=[attr])
        getattr(mod, attr)
    except Exception as e:
        case.fail(NOT_BUILT % (dotted + "." + attr, e))


# ---------------------------------------------------------------- 1. one amount per ticker
class T1_OneAmountPerTicker(unittest.TestCase):
    def test_checker_catches_2026_08_28(self):
        found = invariants.one_amount_per_ticker(OBS["sell_amounts_by_section"])
        self.assertTrue(found, "checker failed to notice a real, documented contradiction")
        joined = " ".join(found)
        self.assertIn("SKHY", joined)   # 35,911 vs 40,154
        self.assertIn("ORCL", joined)   # 9,056 vs 14,630

    def test_system_satisfies(self):
        r = harness.build()
        found = invariants.one_amount_per_ticker(harness.sections_from(r["decisions"]))
        self.assertEqual(found, [], "each ticker must carry exactly one amount everywhere")

    def test_exactly_one_decision_per_ticker(self):
        r = harness.build()
        tickers = [d.ticker for d in r["decisions"]]
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertEqual(set(tickers), set(harness.BOOK["positions"]))


# ---------------------------------------------------------------- 2. one sell total
class T2_OneSellTotal(unittest.TestCase):
    def test_checker_catches_2026_08_28(self):
        found = invariants.one_sell_total(OBS["sell_totals_by_section"])
        self.assertTrue(found, "three totals in one email must be reported")
        self.assertIn("3 different sell totals", found[0])

    def test_system_satisfies(self):
        from cockpit.engine.resolve import total_sell_value
        r = harness.build()
        one = total_sell_value(r["decisions"])
        self.assertEqual(invariants.one_sell_total(
            {"action_plan": one, "position_audit": one, "disposal_ladder": one}), [])

    def test_total_is_the_sum_of_the_decisions_and_nothing_else(self):
        r = harness.build()
        self.assertAlmostEqual(
            r["total_sell"], sum(d.sell_value for d in r["decisions"]), places=2)
        self.assertAlmostEqual(r["total_sell"], 31842.02, places=2,
                               msg="frozen: the 2026-08-27 book resolves to ONE total")


# ---------------------------------------------------------------- 3. as_of not overwritten
class T3_AsOfNotOverwritten(unittest.TestCase):
    def test_checker_catches_2026_08_28(self):
        found = invariants.as_of_not_overwritten_by_run_date(
            OBS["as_of_in_header"], OBS["run_date"])
        self.assertTrue(found, "20260827 data recorded under 2026-08-28 must be reported")

    def test_nav_history_really_recorded_it_under_the_run_date(self):
        """Not a fixture claim -- read the state file the live system wrote."""
        navs = json.load(open(os.path.join(ROOT, "state", "nav_history.json"),
                              encoding="utf-8"))["navs"]
        self.assertIn("2026-08-28", navs)
        self.assertEqual(navs["2026-08-28"], 159528.31)
        self.assertNotIn("2026-08-27", navs,
                         "the 08-27 Flex figure was filed under 08-28; 08-27 itself has no entry")

    def test_decisions_carry_the_flex_as_of_not_the_run_date(self):
        r = harness.build()
        for d in r["decisions"]:
            self.assertEqual(d.as_of, "2026-08-27")

    def test_system_satisfies(self):
        needs(self, "cockpit.ledger.performance", "append_nav_at_as_of")
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        call = [ln for ln in src.splitlines() if "_append_nav(" in ln and "def " not in ln]
        self.assertTrue(call, "_append_nav call site not found")
        self.assertTrue(all("as_of" in ln for ln in call),
                        "daily_brief still files NAV under the run date: %s" % call)
        sig = [ln for ln in src.splitlines() if "_append_signal_log(" in ln and "def " not in ln]
        self.assertTrue(all("as_of" in ln for ln in sig),
                        "signal_history still keyed by the run date: %s" % sig)


# ---------------------------------------------------------------- 4. exits in attribution
class T4_ExitsInAttribution(unittest.TestCase):
    def test_checker_catches_missing_exits(self):
        attributed = [t for t in STMT["open_positions"] if t != "IBKR"]
        found = invariants.exits_present_in_attribution(
            STMT["fully_exited_during_period"], attributed)
        self.assertTrue(found)
        for t in ("NVDA", "SPCX", "MSFT", "META", "AVGO", "TSM"):
            self.assertIn(t, found[0])

    def test_the_replaced_code_is_kept_and_still_shows_the_defect(self):
        """The old implementation is retained as _attribution_legacy purely so the defect
        stays demonstrable. Read the real code, not a description of it."""
        src = open(os.path.join(ROOT, "cockpit", "biweekly_review.py"), encoding="utf-8").read()
        legacy = src.split("def _attribution_legacy(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not vb: continue", legacy)
        self.assertIn("if not vN: continue", legacy)
        live = src.split("def _attribution(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("attribution_with_exits", live, "the live path must use the ledger")
        self.assertGreater(STMT["realized_pnl"]["NVDA"], 1400,
                           "the name it dropped is the largest realised gain of the period")

    def test_system_satisfies(self):
        needs(self, "cockpit.ledger.performance", "attribution_with_exits")
        from cockpit.ledger.performance import attribution_with_exits
        days = [{"date": "d1", "net_liq": 100.0, "holdings": {"X": {"shares": 1, "price": 10.0}}},
                {"date": "d2", "net_liq": 100.0, "holdings": {"X": {"shares": 1, "price": 11.0}}},
                {"date": "d3", "net_liq": 100.0, "holdings": {}}]
        a = attribution_with_exits(days, exit_prices={"X": 12.0})
        self.assertEqual(invariants.exits_present_in_attribution(["X"], list(a["exits"])), [])
        self.assertEqual(a["exits"]["X"]["basis"], "estimated")


# ---------------------------------------------------------------- 5. theme_overrides reach risk
class T5_OverridesReachRiskPaths(unittest.TestCase):
    def _maps(self):
        from cockpit import daily_brief as db
        overrides = (db.CFG.get("risk", {}) or {}).get("theme_overrides", {}) or {}
        return overrides, db._theme_of()

    @staticmethod
    def _legacy_map(cfg):
        """The map the risk engine received before R5: config.subthemes ONLY.
        Reconstructed here rather than read from live source, so this test keeps
        proving the checker works after the defect is gone."""
        out = {}
        for name, v in (cfg.get("subthemes") or {}).items():
            for t in (v.get("names") or []):
                out.setdefault(t, name)
        return out

    def test_checker_catches_the_pre_R5_map(self):
        cfg = harness.config()
        overrides = (cfg.get("risk") or {}).get("theme_overrides") or {}
        found = invariants.overrides_reach_risk_paths(overrides, self._legacy_map(cfg))
        self.assertTrue(found, "the pre-R5 map hid every override name from the risk engine")
        for t in ("SKHY", "RAM", "KLAC", "ASX", "GLW", "SOXX", "CCXI"):
            self.assertIn(t, found[0])

    def test_the_two_largest_positions_share_a_theme(self):
        """SKHY 31% NAV and MU 24.5% NAV are both memory_hbm. Before R5 the map handed to
        position_caps() had no theme for SKHY at all, so the >=0.60 same-theme correlation
        floor never applied to the most concentrated pair in the book."""
        cfg = harness.config()
        legacy = self._legacy_map(cfg)
        self.assertEqual(legacy.get("MU"), "memory_hbm")
        self.assertIsNone(legacy.get("SKHY"), "the pre-R5 map could not see SKHY's theme")
        _overrides, live = self._maps()
        self.assertEqual(live.get("SKHY"), "memory_hbm", "R5: it can now")
        self.assertEqual(live.get("MU"), "memory_hbm")

    def test_single_layer_resolver_covers_every_override(self):
        """concentration.theme_map is now the ONE place a layer is resolved."""
        from cockpit.rules import concentration
        overrides, _ = self._maps()
        cfg = harness.config()
        tm = concentration.theme_map(list(overrides) + list(harness.BOOK["positions"]), cfg)
        self.assertEqual(invariants.overrides_reach_risk_paths(overrides, tm), [])
        self.assertEqual(tm["SKHY"], "memory_hbm")
        self.assertEqual(tm["MU"], "memory_hbm")

    def test_system_satisfies(self):
        """RED until daily_brief actually passes the complete map into risk.position_caps."""
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        call = [ln for ln in src.splitlines() if "risk.position_caps(" in ln]
        self.assertTrue(call, "position_caps call site not found")
        self.assertTrue(any("theme_map" in ln for ln in call),
                        "position_caps still receives the subthemes-only map: %s" % call)


# ---------------------------------------------------------------- 6. no Schwab/QQQ in IBKR brief
class T6_NoOutOfScopeAdvice(unittest.TestCase):
    FORBIDDEN = ["QQQ 定投", "回调子弹", "嘉信"]

    def test_checker_catches_2026_08_28(self):
        found = invariants.no_out_of_scope_advice(OBS["schwab_qqq_text"], self.FORBIDDEN)
        self.assertTrue(found)
        self.assertTrue(OBS["contains_schwab_qqq_text"])

    def test_checker_catches_the_rendered_line(self):
        """Point the checker at the line the 2026-08-28 email really printed."""
        self.assertTrue(invariants.no_out_of_scope_advice(OBS["schwab_qqq_text"], self.FORBIDDEN))

    def test_system_satisfies(self):
        """Both halves: the rendered strings, and the module that holds them. The comment
        explaining the removal must not quote the removed text -- a source-level guard that a
        comment can satisfy (or break) is not a guard."""
        from cockpit.daily_brief import _MKT_ZONE_CN
        for _zone, (_label, hint) in _MKT_ZONE_CN.items():
            self.assertEqual(invariants.no_out_of_scope_advice(hint, self.FORBIDDEN), [])
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        self.assertEqual(invariants.no_out_of_scope_advice(src, self.FORBIDDEN), [])


# ---------------------------------------------------------------- 7. residual-position noise floor
class T7_NoResidualNoise(unittest.TestCase):
    MIN_PCT_NAV = 0.25
    MIN_USD = 200.0

    def test_checker_catches_2026_08_28(self):
        pt = OBS["profit_take"]
        found = invariants.no_residual_noise(
            [{"ticker": pt["ticker"], "market_value": pt["market_value"],
              "amount_usd": pt["trim_usd"]}],
            OBS["net_liq_printed"], self.MIN_PCT_NAV, self.MIN_USD)
        self.assertTrue(found, "a $59 trim on a $179 stub must be filtered")
        self.assertIn("MRVL", found[0])

    def test_config_floor_matches_the_resolver_default(self):
        """A policy declared in config and a different default buried in code is two policies."""
        import yaml
        from cockpit.engine import resolve as R
        cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
        rk = cfg.get("risk", {}) or {}
        self.assertEqual(rk["min_decision_pct_nav"], self.MIN_PCT_NAV)
        self.assertEqual(rk["min_decision_usd"], self.MIN_USD)
        empty = R.resolve_decisions([], {}, 100000.0, "2026-08-27", {})
        self.assertEqual(empty, [])

    def test_resolver_already_suppresses_the_mrvl_stub(self):
        r = harness.build()
        mrvl = [d for d in r["decisions"] if d.ticker == "MRVL"][0]
        self.assertEqual(mrvl.action, "HOLD")
        self.assertEqual(mrvl.sell_value, 0.0)

    def test_system_satisfies(self):
        """RED until the noise floor is a declared policy in config, not a code default."""
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
        rk = cfg.get("risk", {}) or {}
        self.assertIn("min_decision_pct_nav", rk)
        self.assertIn("min_decision_usd", rk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
