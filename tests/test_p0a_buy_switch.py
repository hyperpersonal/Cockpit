"""P0A: the production BUY kill switch.

Why this exists. The unified Decision layer landed on 2026-08-28 and it adjudicates SELLS the
way the user accepted: portfolio-level, sequential, one Decision per ticker. The BUY half was
built in the same push but has NOT been accepted -- the three entry conditions (Serenity-14,
VCP, dilution) are not computed by this system at all, so today every production candidate
resolves to WATCH only because the verification gate happens to catch it on the way past.

That is a coincidence, not a control. One enriched candidate record, or one config edit, and
an unvalidated buy becomes an executable order on the first screen. So P0A adds one explicit
switch, `risk.entry_decisions_enabled`, and it FAILS CLOSED: a missing key means disabled.

The bearing assumption this file states out loud: these tests prove the switch stops the
BINDING BUY path inside the engine and that the first screen says so. They do NOT prove that
a real email was sent with the switch off -- no live run happens here (see the "what I did
not check" note in the handoff).
"""
from __future__ import annotations
import copy, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import harness  # noqa: E402
import test_entry_decisions as ted  # noqa: E402   (reuse the exact BUY scenario, flag flipped)
from cockpit import daily_brief  # noqa: E402
from cockpit.domain import policy  # noqa: E402
from cockpit.render import action_list  # noqa: E402

PROD = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))


def _flag(cfg, value):
    c = copy.deepcopy(cfg)
    c.setdefault("risk", {})
    if value is None:
        c["risk"].pop(policy.ENTRY_FLAG_KEY, None)
    else:
        c["risk"][policy.ENTRY_FLAG_KEY] = value
    return c


class P1_ProductionConfigIsClosed(unittest.TestCase):
    def test_the_key_is_present_and_false(self):
        self.assertIn(policy.ENTRY_FLAG_KEY, PROD["risk"],
                      "production config must state the switch, not inherit a default")
        self.assertIs(PROD["risk"][policy.ENTRY_FLAG_KEY], False)

    def test_the_resolver_agrees(self):
        self.assertFalse(policy.entry_decisions_enabled(PROD))
        self.assertEqual(policy.entry_block_reason(PROD), policy.P0A_BUY_BLOCK_REASON)


class P2_FailsClosed(unittest.TestCase):
    """A missing key may never open the buy path. Neither may a value that is not true."""

    def test_missing_key_is_disabled(self):
        self.assertFalse(policy.entry_decisions_enabled(_flag(PROD, None)))

    def test_empty_and_absent_config_are_disabled(self):
        for cfg in (None, {}, {"risk": {}}, {"risk": None}):
            self.assertFalse(policy.entry_decisions_enabled(cfg), repr(cfg))

    def test_only_true_opens_it(self):
        for v in (False, 0, 1, "", "no", "off", "False", None, [], "maybe"):
            self.assertFalse(policy.entry_decisions_enabled(_flag(PROD, v)), repr(v))
        for v in (True, "true", "TRUE", "yes", "on", "1"):
            self.assertTrue(policy.entry_decisions_enabled(_flag(PROD, v)), repr(v))

    def test_a_missing_key_still_blocks_a_fundable_candidate(self):
        """No key at all, positive cash, low heat, everything verified -> still no BUY."""
        c = _flag(ted.cfg(layers=ted.LAYERS), None)
        r = ted.run(copy.deepcopy(ted.HOLD), c, ted.LAYERS, cash=20000.0, heat=3.0,
                    candidates=[ted.cand("CAND")])
        self.assertNotIn("CAND", r["by"])


class P3_FlagOffProducesNoBuy(unittest.TestCase):
    """The scenario T12 uses to prove a BUY can be produced -- positive cash, heat 3%, all
    three verifications present and unexpired -- with the switch off and nothing else changed."""

    def setUp(self):
        self.c = _flag(ted.cfg(layers=ted.LAYERS), False)
        self.r = ted.run(copy.deepcopy(ted.HOLD), self.c, ted.LAYERS, cash=20000.0, heat=3.0,
                         candidates=[ted.cand("CAND")], reentries=[
                             {"ticker": "BACK", "price": 50.0, "ma50": 45.0,
                              "exit_date": "2026-08-01", "verification": ted.verified()}])

    def test_no_buy_decision_at_all(self):
        self.assertEqual([d.ticker for d in self.r["ds"] if d.action == "BUY"], [])

    def test_the_candidate_never_enters_the_book(self):
        self.assertNotIn("CAND", self.r["by"])
        self.assertNotIn("BACK", self.r["by"])

    def test_no_binding_buy_proposal_survives(self):
        self.assertEqual([p.rule_id for p in self.r["props"]
                          if p.kind == "buy" and p.binding], [])

    def test_the_candidate_still_produces_a_watch(self):
        """Off does not mean silent: the observation still travels the same module."""
        w = [p for p in self.r["props"] if p.ticker == "CAND" and p.kind == "review"]
        self.assertTrue(w, "a blocked candidate must still surface as WATCH/REVIEW")
        self.assertIn(policy.P0A_BUY_BLOCK_REASON, w[0].reason)

    def test_the_reentry_still_produces_a_watch(self):
        w = [p for p in self.r["props"] if p.ticker == "BACK" and p.kind == "review"]
        self.assertTrue(w)
        self.assertIn(policy.P0A_BUY_BLOCK_REASON, w[0].reason)

    def test_a_watch_carries_no_shares_no_amount_no_stop_order(self):
        for t in ("CAND", "BACK"):
            w = [p for p in self.r["props"] if p.ticker == t and p.kind == "review"][0]
            self.assertIsNone(w.target_value)
            self.assertNotIn("shares", w.evidence)
            self.assertFalse(w.binding)

    def test_the_sell_side_is_untouched(self):
        """P0A disables buying only. The position that is held must still be adjudicated."""
        self.assertIn("HELD", self.r["by"])


class P4_FirstScreenSaysNo(unittest.TestCase):
    def test_the_gate_renders_no_with_the_exact_reason(self):
        reason = policy.entry_block_reason(PROD)
        md = action_list.header("2026-08-27", "", 159528.31, -9186.07, 3.3, 23060.0, 14.5,
                                buying_allowed=(reason is None), buying_reason=reason,
                                sell_total=31842.02)
        line = [l for l in md.splitlines() if "今天是否允许买入" in l][0]
        self.assertIn("否", line)
        self.assertNotIn("✅ 是", line)
        self.assertIn(policy.P0A_BUY_BLOCK_REASON, line)

    def test_the_gate_blocks_on_a_day_no_market_condition_would_block(self):
        """Positive cash, heat 3% -- neither the margin rule nor the heat rule says anything.
        Only P0A does, and it must be what the first screen shows."""
        self.assertEqual(daily_brief._buy_gate_reason(PROD, 50000.0, 3.0, 0.0),
                         policy.P0A_BUY_BLOCK_REASON)

    def test_p0a_outranks_the_market_conditions(self):
        """On the frozen book cash is negative AND heat is 14.5%. Both would produce their own
        wording; the acceptance boundary is the one that must be stated."""
        self.assertEqual(daily_brief._buy_gate_reason(PROD, -9186.07, 14.5, 31842.02),
                         policy.P0A_BUY_BLOCK_REASON)

    def test_with_buys_enabled_the_market_conditions_are_reached_again(self):
        """P0A must not swallow the pre-existing gates -- turning it on restores them."""
        on = _flag(PROD, True)
        self.assertIn("保证金使用中", daily_brief._buy_gate_reason(on, -9186.07, 3.0, 0.0))
        self.assertIn("组合在险", daily_brief._buy_gate_reason(on, 50000.0, 14.5, 31842.02))
        self.assertIsNone(daily_brief._buy_gate_reason(on, 50000.0, 3.0, 0.0))

    def test_build_uses_the_same_gate_and_derives_the_flag_from_it(self):
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        self.assertIn("buy_block = _buy_gate_reason(CFG, cash, portfolio_heat_pct, sell_total)",
                      src, "build() must not re-implement the gate")
        self.assertIn("buying_allowed=(buy_block is None)", src)


class P5_FlagOnStillWorks(unittest.TestCase):
    """P0A must not amputate the buy engine -- it must switch it off. With the flag explicitly
    true the original synthetic BUY Decision comes back, field for field (T12's numbers)."""

    def setUp(self):
        self.r = ted.run(copy.deepcopy(ted.HOLD), ted.cfg(layers=ted.LAYERS), ted.LAYERS,
                         cash=20000.0, heat=3.0, candidates=[ted.cand("CAND")])
        self.d = self.r["by"]["CAND"]

    def test_the_fixture_sets_the_flag_explicitly(self):
        self.assertIs(ted.cfg(layers=ted.LAYERS)["risk"][policy.ENTRY_FLAG_KEY], True,
                      "the BUY fixtures must opt in, never rely on a default")

    def test_the_original_buy_decision_is_unchanged(self):
        self.assertEqual(self.d.action, "BUY")
        self.assertEqual(self.d.current_shares, 0.0)
        self.assertEqual(self.d.target_shares, 250.0)
        self.assertEqual(self.d.delta_shares, 250.0)
        self.assertAlmostEqual(self.d.target_value, 12500.0, delta=1.0)
        self.assertAlmostEqual(self.d.expected_risk_usd, 1000.0, delta=1.0)


class P6_FrozenBookUnchanged(unittest.TestCase):
    """The 2026-08-27 regression number must not move. It runs on the PRODUCTION config, so
    it now runs with the switch off -- and it was already BUY-free (cash was negative), which
    is exactly why it is the right sentinel for "P0A changed nothing on the sell side"."""

    def setUp(self):
        self.r = harness.build()

    def test_total_sell_is_still_31842_02(self):
        self.assertAlmostEqual(self.r["total_sell"], 31842.02, delta=1.0)

    def test_no_buy_decisions(self):
        self.assertEqual([d.ticker for d in self.r["decisions"] if d.action == "BUY"], [])

    def test_it_ran_with_buys_disabled(self):
        self.assertFalse(policy.entry_decisions_enabled(harness.config()))


class P7_ToDeleteIsNotPartOfAnything(unittest.TestCase):
    """`_to_delete/` holds superseded copies of live modules. If it ever enters discovery or a
    commit, tests would pass against dead code -- the same shape as the deleted-function
    defect this project already hit twice."""

    MARK = os.sep + "_to_delete" + os.sep

    def test_no_loaded_module_comes_from_to_delete(self):
        bad = [n for n, m in list(sys.modules.items())
               if getattr(m, "__file__", None) and self.MARK in os.path.abspath(m.__file__)]
        self.assertEqual(bad, [])

    def test_discovery_root_contains_no_to_delete(self):
        for dirpath, dirnames, _files in os.walk(HERE):
            self.assertNotIn("_to_delete", dirnames, "under %s" % dirpath)

    def test_git_tracks_nothing_under_to_delete(self):
        try:
            out = subprocess.run(["git", "--no-optional-locks", "ls-files", "--", "_to_delete"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:      # no git here -> not a verdict
            self.skipTest("git unavailable: %s" % e)
        if out.returncode != 0:
            self.skipTest("not a git work tree: %s" % (out.stderr or "").strip())
        self.assertEqual(out.stdout.strip(), "",
                         "_to_delete/ is tracked -- it must never enter a commit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
