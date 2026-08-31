"""Scrape the RENDERED text, not the objects behind it.

The 2026-08-28 defect was visible only in the email: every section computed correctly by
its own lights, and the contradiction appeared where the user reads. So the last guard
reads the finished markdown and counts dollar figures.
"""
from __future__ import annotations
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import harness  # noqa: E402
import invariants  # noqa: E402
from cockpit.render import action_list  # noqa: E402
from cockpit.rules.concentration import leverage_of  # noqa: E402

MONEY = re.compile(r"-?\$([\d,]+)")


def build_md():
    r = harness.build()
    cfg = harness.config()
    pos, nav, cash = r["positions"], r["net_liq"], r["cash"]
    reasons = {}
    for p in r["proposals"]:
        reasons.setdefault(p.rule_id, p.reason)
    lev = sum(pos[t]["market_value"] for t in pos if leverage_of(t, cfg) > 1)
    risk = sum(d["market_value"] * d["dist_to_stop_pct"] / 100.0 for d in pos.values())
    md = action_list.render(
        r["decisions"], reasons, as_of="2026-08-27", net_liq=nav, cash=cash,
        leverage_pct=lev / nav * 100, risk_usd=risk, risk_pct=risk / nav * 100,
        buying_allowed=False, buying_reason="保证金使用中（红线 v2 ③）")
    return r, md


class TestRenderedActionList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r, cls.md = build_md()

    def test_the_total_appears_once_and_matches_the_decisions(self):
        self.assertIn("**最终卖出总额** | **$31,842**", self.md)
        self.assertEqual(self.md.count("最终卖出总额"), 1)

    def test_each_traded_ticker_has_exactly_one_trade_amount(self):
        """Pull the 本次金额 row of every decision block out of the finished text."""
        blocks = re.split(r"\n\*\*\d+\. ", self.md)[1:]
        amounts = {}
        for b in blocks:
            ticker = b.split(" ·", 1)[0].strip()
            row = [ln for ln in b.splitlines() if ln.startswith("| 本次金额")]
            self.assertEqual(len(row), 1, "%s must show exactly one 本次金额 row" % ticker)
            amounts[ticker] = float(MONEY.search(row[0]).group(1).replace(",", ""))
        self.assertEqual(invariants.one_amount_per_ticker({"rendered": amounts}), [])
        self.assertEqual(sorted(amounts), ["MU", "RAM", "SKHY"])
        self.assertAlmostEqual(sum(amounts.values()), 31842, delta=1.5)

    def test_every_required_field_is_present_for_every_suggestion(self):
        required = ["当前股数 → 目标股数", "需要买卖的股数", "目标仓位金额", "绑定规则",
                    "核心理由", "有效期", "失效条件", "数据时间"]
        blocks = re.split(r"\n\*\*\d+\. ", self.md)[1:]
        self.assertEqual(len(blocks), 3)
        for b in blocks:
            for f in required:
                self.assertIn(f, b, "missing field %s" % f)

    def test_first_screen_carries_the_required_summary(self):
        head = self.md.split("### ", 1)[0]
        for f in ["账户数据时间", "净值 / 现金", "杠杆产品占净值", "组合在险",
                  "今天是否允许买入", "最终卖出总额"]:
            self.assertIn(f, head)
        self.assertIn("2026-08-27", head)

    def test_no_out_of_scope_advice_in_the_rendered_text(self):
        self.assertEqual(
            invariants.no_out_of_scope_advice(self.md, ["QQQ 定投", "回调子弹", "嘉信"]), [])

    def test_risk_budget_is_labelled_as_a_warning_not_an_instruction(self):
        self.assertIn("警示层，不产生卖出金额", self.md)

    def test_review_items_appear_but_carry_no_money(self):
        tail = self.md.split("### 观察项", 1)[1]
        for t in ("KLAC", "GLW"):
            self.assertIn(t, tail)
        self.assertNotIn("本次金额", tail)

    def test_renderer_does_no_arithmetic(self):
        """Structural: the module must not contain sizing arithmetic. A renderer that can
        compute an amount will eventually compute a different one."""
        src = open(os.path.join(os.path.dirname(HERE), "cockpit", "render",
                                "action_list.py"), encoding="utf-8").read()
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#") and '"""' not in ln)
        for token in ("market_value", "cap_usd", "* price", "/ price", "min(", "max("):
            self.assertNotIn(token, body, "renderer performs sizing: %r" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
