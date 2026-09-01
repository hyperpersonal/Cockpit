"""P0 policy-consistency fixes (user, 2026-08-28): hard cap, leveraged stop, heat semantics."""
from __future__ import annotations
import copy, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import harness  # noqa: E402
from cockpit.domain import policy  # noqa: E402
from cockpit.domain.models import HARD_EXIT, FLAG  # noqa: E402
from cockpit.rules import account, concentration  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))


# ------------------------------------------------------------------ 1. hard cap
class TestHardCapIsOneFixedDollarValue(unittest.TestCase):
    def test_authoritative_key_is_thirty_thousand(self):
        self.assertEqual(CFG["risk"]["single_name_hard_cap_usd"], 30000)
        self.assertEqual(policy.hard_cap_usd(CFG), 30000.0)

    def test_total_assets_cannot_move_the_cap(self):
        """The whole point: the ceiling must not drift when the Schwab side is re-estimated."""
        for ta in (100000, 250000, 400000, 1000000, 0):
            c = copy.deepcopy(CFG)
            c["account"]["total_assets_usd"] = ta
            self.assertEqual(policy.hard_cap_usd(c), 30000.0,
                             "total_assets_usd=%s moved the hard cap" % ta)

    def test_legacy_percentage_cannot_move_the_cap_either(self):
        for pct in (5, 12, 30):
            c = copy.deepcopy(CFG)
            c["risk"]["single_name_hard_cap_pct_of_total"] = pct
            self.assertEqual(policy.hard_cap_usd(c), 30000.0)

    def test_the_rule_reads_the_same_resolver(self):
        c = copy.deepcopy(CFG)
        c["account"]["total_assets_usd"] = 1000000
        c["risk"]["single_name_hard_cap_pct_of_total"] = 30
        pos = {"AAA": {"market_value": 90000.0, "price": 10.0, "shares": 9000.0}}
        props = [p for p in concentration.propose(pos, 300000.0, c)
                 if p.rule_id == concentration.RULE_HARD_CAP]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].target_value, 30000.0)

    def test_legacy_fallback_survives_one_migration_cycle_and_says_so(self):
        c = copy.deepcopy(CFG)
        del c["risk"]["single_name_hard_cap_usd"]
        self.assertEqual(policy.hard_cap_usd(c), 30000.0)      # 250k x 12%
        notes = policy.deprecation_notices(c)
        self.assertTrue(notes)
        self.assertIn("回退", notes[0])

    def test_a_disagreeing_legacy_pair_is_reported_not_obeyed(self):
        c = copy.deepcopy(CFG)
        c["account"]["total_assets_usd"] = 500000          # legacy would say 60,000
        notes = policy.deprecation_notices(c)
        self.assertTrue(any("不**参与**" in n or "不参与" in n for n in notes))
        self.assertEqual(policy.hard_cap_usd(c), 30000.0)

    def test_no_module_recomputes_the_cap_on_its_own(self):
        pat = re.compile(r"total_assets\w*\s*\*|single_name_hard_cap_pct_of_total\W*\]?\s*/")
        for sub in ("", "domain", "engine", "rules", "render", "ledger"):
            d = os.path.join(ROOT, "cockpit", sub) if sub else os.path.join(ROOT, "cockpit")
            for f in sorted(os.listdir(d)):
                if not f.endswith(".py") or (sub == "domain" and f == "policy.py"):
                    continue
                src = open(os.path.join(d, f), encoding="utf-8").read()
                code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
                self.assertIsNone(pat.search(code),
                                  "%s/%s derives the hard cap itself" % (sub or ".", f))

    def test_the_frozen_book_still_resolves_to_thirty_thousand(self):
        r = harness.build()
        by = {d.ticker: d for d in r["decisions"]}
        self.assertEqual(by["SKHY"].target_value, 30000.0)
        self.assertEqual(by["MU"].target_value, 30000.0)


# ------------------------------------------------------------------ 2. leveraged stop
class TestLeveragedEtfHardStop(unittest.TestCase):
    NAV, CASH = 159528.31, 0.0

    def _ram(self, price, cost=12.10, low_stop=9.88):
        return {"RAM": {"market_value": 413.2232 * price, "price": price,
                        "cost_price": cost, "stop_level": low_stop}}

    def _lev(self, pos):
        return [p for p in account.propose(pos, self.NAV, self.CASH, CFG)
                if p.rule_id == account.RULE_LEV_STOP]

    def test_minus_fifteen_percent_triggers_when_it_is_hit_first(self):
        """cost 12.10 -> −15% = 10.285, above the 20-day stop 9.88, so it is reached first."""
        p = self._lev(self._ram(10.28))
        self.assertEqual([x.kind for x in p], [HARD_EXIT])
        self.assertIn("cost−15%", p[0].reason)
        self.assertAlmostEqual(p[0].evidence["level"], 10.285, places=3)

    def test_twenty_day_low_triggers_when_it_is_the_higher_level(self):
        """Cost far below the 20-day low (a position deep in profit): the low is hit first."""
        p = self._lev(self._ram(11.00, cost=5.00, low_stop=11.50))
        self.assertEqual([x.kind for x in p], [HARD_EXIT])
        self.assertIn("20日低", p[0].reason)
        self.assertAlmostEqual(p[0].evidence["level"], 11.50, places=2)

    def test_the_operative_level_is_always_the_higher_of_the_two(self):
        lvl, which, cs, ls = account._leveraged_stop_level(
            {"cost_price": 12.10, "stop_level": 9.88}, CFG)
        self.assertAlmostEqual(lvl, 10.285, places=3)
        self.assertAlmostEqual(cs, 10.285, places=3)
        self.assertEqual(ls, 9.88)

    def test_untriggered_leveraged_position_only_flags(self):
        p = self._lev(self._ram(12.63))
        self.assertEqual([x.kind for x in p], [FLAG])
        self.assertFalse(p[0].binding)

    def test_a_missing_cost_and_stop_is_a_data_gap_not_a_free_pass(self):
        p = self._lev({"RAM": {"market_value": 5000.0, "price": 12.0,
                               "cost_price": None, "stop_level": None}})
        self.assertEqual([x.kind for x in p], [FLAG])
        self.assertIn("数据缺口", p[0].reason)

    def test_a_plain_stock_never_gets_the_minus_fifteen_rule(self):
        """MU is 13% underwater on 2026-08-27 and must NOT be force-exited by this rule."""
        pos = {"MU": {"market_value": 39057.86, "price": 935.39,
                      "cost_price": 1077.706119859, "stop_level": 814.80}}
        self.assertEqual(self._lev(pos), [])
        self.assertEqual(concentration.leverage_of("MU", CFG), 1.0)

    def test_the_rule_id_is_actually_used(self):
        """RULE_LEV_STOP was a dead constant until now."""
        src = open(os.path.join(ROOT, "cockpit", "rules", "account.py"), encoding="utf-8").read()
        self.assertGreaterEqual(src.count("RULE_LEV_STOP"), 3)

    def test_a_triggered_leveraged_stop_beats_every_sizing_ceiling(self):
        from cockpit.engine.resolve import resolve_decisions
        pos = {"RAM": {"shares": 413.2232, "price": 10.28, "market_value": 4247.9,
                       "cost_price": 12.10, "stop_level": 9.88}}
        props = self._lev(pos) + list(concentration.propose(pos, self.NAV, CFG))
        d = resolve_decisions(props, pos, self.NAV, "2026-08-27", CFG)[0]
        self.assertEqual(d.action, "EXIT")
        self.assertEqual(d.target_value, 0.0)
        self.assertEqual(d.binding_rule, account.RULE_LEV_STOP)


# ------------------------------------------------------------------ 4. heat semantics
class TestHeatSemanticsAreNotSelfContradictory(unittest.TestCase):
    SRC = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()

    def test_the_gate_text_no_longer_denies_the_gate(self):
        """A ⛔ next to 'this is a warning, not a prohibition' is unreadable."""
        code = "\n".join(l for l in self.SRC.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("这是警示，不是禁令", code)

    def test_heat_blocks_new_risk_and_says_exactly_that(self):
        block = self._heat_reason()
        self.assertIn("暂停新增风险", block)
        self.assertIn("不开新仓", block)

    def test_heat_explicitly_does_not_demand_selling(self):
        self.assertIn("不要求你为了把在险打回", self._heat_reason())

    def _heat_reason(self):
        """The heat wording as the gate actually produces it.

        This used to slice daily_brief.py's source between two anchors. P0A moved the gate
        into `_buy_gate_reason()`, and a source-slicing test cannot tell "the wording moved"
        from "the wording is gone" -- so it now calls the gate. Buys are switched ON here on
        purpose: with the P0A boundary in force the heat branch is never reached, and a test
        that silently stopped exercising the heat rule would be worth nothing.
        """
        from cockpit import daily_brief
        import copy as _copy
        cfg = _copy.deepcopy(CFG)
        cfg.setdefault("risk", {})["entry_decisions_enabled"] = True
        return daily_brief._buy_gate_reason(cfg, 50000.0, 14.5, 31842.02)

    def test_heat_does_not_change_the_sell_total(self):
        """The number must be identical whether heat is 14.5% or 0.5%."""
        r = harness.build()
        base = r["total_sell"]
        self.assertAlmostEqual(base, 31842.02, places=2)
        heats = [sum(d["market_value"] * d["dist_to_stop_pct"] / 100.0
                     for d in r["positions"].values())]
        self.assertGreater(heats[0] / r["net_liq"] * 100, 6.0, "the book really is over budget")
        rule_ids = {d.binding_rule for d in r["decisions"] if d.is_sell}
        self.assertNotIn("sizing.vol_corr_cap", rule_ids)
        self.assertNotIn("sizing.risk_tier_target", rule_ids)

    def test_the_rendered_gate_and_its_reason_agree(self):
        from cockpit.render import action_list
        md = action_list.header("2026-08-27", "", 159528.31, -9186.07, 3.3, 23060.0, 14.5,
                                False, "组合在险 14.5% 高于 6-8% 预算 → **暂停新增风险**：今天不开新仓、"
                                       "不加仓。**它不要求你为了把在险打回 6-8% 而机械卖出**。",
                                31842.02)
        line = [l for l in md.splitlines() if "今天是否允许买入" in l][0]
        self.assertIn("⛔ 否", line)
        self.assertIn("暂停新增风险", line)
        for contradiction in ("不是禁令", "仅警示", "仍可买入"):
            self.assertNotIn(contradiction, line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
