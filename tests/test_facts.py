"""External facts: source + verification date + expiry, and what each status may do."""
from __future__ import annotations
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
from cockpit.rules import facts, thesis  # noqa: E402
from cockpit.domain.models import HARD_EXIT, REVIEW, FLAG  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))


class TestStampParsing(unittest.TestCase):
    def test_canonical_form(self):
        a = facts.assess("某某（核实 2026-08-25，源=FMP profile）", CFG, "2026-08-28")
        self.assertEqual(a["status"], facts.VERIFIED)
        self.assertEqual(a["verified_on"], "2026-08-25")
        self.assertEqual(a["source"], "FMP profile")
        self.assertEqual(a["age_days"], 3)

    def test_legacy_hexhi_yu_form(self):
        """AAOI in the live config writes 核实于 rather than 核实."""
        a = facts.assess("...核实于 2026-08-25，源=SEC 424B5 原文）", CFG, "2026-08-28")
        self.assertEqual(a["status"], facts.VERIFIED)
        self.assertEqual(a["source"], "SEC 424B5 原文")

    def test_legacy_colon_form(self):
        """SKHY writes 核实 YYYY-MM-DD：<source text> with no 源= marker."""
        a = facts.assess("核实 2026-08-25：FMP profile isAdr=true / country=KR", CFG, "2026-08-28")
        self.assertEqual(a["status"], facts.VERIFIED)
        self.assertIn("FMP profile", a["source"])

    def test_date_without_a_source_is_not_verified(self):
        a = facts.assess("核实 2026-08-25", CFG, "2026-08-28")
        self.assertEqual(a["status"], facts.UNVERIFIED)
        self.assertIn("没写来源", a["reason"])

    def test_no_stamp_at_all(self):
        self.assertEqual(facts.assess("SPAC空壳，体系外", CFG)["status"], facts.UNVERIFIED)

    def test_explicit_unverified_is_reported_as_such(self):
        a = facts.assess("某某（未核实）", CFG)
        self.assertEqual(a["status"], facts.UNVERIFIED)
        self.assertIn("未核实", a["reason"])

    def test_expiry(self):
        ann = "某某（核实 2026-08-25，源=FMP profile）"
        self.assertEqual(facts.assess(ann, CFG, "2027-02-20")["status"], facts.VERIFIED)
        a = facts.assess(ann, CFG, "2027-03-16")
        self.assertEqual(a["status"], facts.EXPIRED)
        self.assertIn("需重新核实", a["reason"])

    def test_every_live_annotation_is_currently_verified(self):
        """Regression guard on the real config: if someone edits a role and drops the stamp,
        this goes red before the annotation can drive anything."""
        bad = {t: v for t, v in facts.audit(CFG, "2026-08-28").items()
               if v["status"] != facts.VERIFIED}
        self.assertEqual(bad, {}, "unverified/expired annotations in config.holdings")


class TestOnlyVerifiedFactsMayForceAnExit(unittest.TestCase):
    POS = {"XX": {"market_value": 10000.0, "price": 10.0, "shares": 1000.0}}
    LAYERS = {"XX": thesis.OUTSIDE_LAYER}

    def _cfg(self, role):
        c = dict(CFG)
        c["holdings"] = [{"ticker": "XX", "role": role}]
        return c

    def test_verified_can_hard_exit(self):
        p = thesis.propose(self.POS, self.LAYERS,
                           self._cfg("已退市（核实 2026-08-25，源=交易所公告）"), today="2026-08-28")
        self.assertEqual([x.kind for x in p], [HARD_EXIT])

    def test_unverified_can_only_review(self):
        p = thesis.propose(self.POS, self.LAYERS, self._cfg("体系外空壳"), today="2026-08-28")
        self.assertEqual([x.kind for x in p], [REVIEW])
        self.assertIn("不产生清仓指令", p[0].reason)

    def test_expired_can_only_review(self):
        """The CCXI failure mode with a clock attached: a fact that was true once is not a
        licence to liquidate forever."""
        p = thesis.propose(self.POS, self.LAYERS,
                           self._cfg("已退市（核实 2026-08-25，源=交易所公告）"), today="2027-03-16")
        self.assertEqual([x.kind for x in p], [REVIEW])
        self.assertEqual(p[0].confidence, facts.EXPIRED)

    def test_stale_annotation_on_a_normal_holding_only_flags(self):
        p = thesis.propose(self.POS, {"XX": "memory_hbm"}, self._cfg("随便写的"), today="2026-08-28")
        self.assertEqual([x.kind for x in p], [FLAG])
        self.assertFalse(p[0].binding, "a FLAG can never move a position")

    def test_a_verified_normal_holding_produces_nothing(self):
        p = thesis.propose(self.POS, {"XX": "memory_hbm"},
                           self._cfg("正常（核实 2026-08-25，源=FMP profile）"), today="2026-08-28")
        self.assertEqual(p, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
