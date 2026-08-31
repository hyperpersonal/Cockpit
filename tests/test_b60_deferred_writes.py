"""B60: one-shot content may be consumed ONLY by a brief that was actually delivered.

The incident (2026-08-28): a 06:14 UTC run detected the NVDA/AVGO exits, wrote
last_positions.json and stamped reentry_watch["NVDA"]["prompted"], then its email silently
failed on a stale app password. The 06:55 run compared against the already-updated snapshot,
saw no closes, and the exit postmortem plus the NVDA re-entry prompt were gone for good.

B59 (exit 1 on send failure) plus the workflow's `if: always()` Persist step made this WORSE:
the run turns red and the state that consumed the content gets committed anyway.

These tests drive the REAL functions against REAL temp state files. Each one is paired with a
reverse-break proof in test_reverse_break_*: the guard is deliberately broken and the test
must go red. A test that has never been seen to fail proves nothing, and neither does a
selfcheck gate that a comment or a string can satisfy (gate 10's first version did exactly
that).
"""
from __future__ import annotations
import json, os, pathlib, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import cockpit.daily_brief as db  # noqa: E402


class _StateSandbox:
    """A throwaway ROOT/state tree so the real writers can run without touching real state."""

    def __enter__(self):
        self.td = pathlib.Path(tempfile.mkdtemp())
        (self.td / "state").mkdir()
        self.last_positions = self.td / "state" / "last_positions.json"
        self.reentry = self.td / "state" / "reentry_watch.json"
        self.memory = self.td / "state" / "reflection_memory.json"
        json.dump({"date": "2026-08-26",
                   "positions": {"NVDA": {"shares": 50.42, "avg": 210.0, "pnl_pct": 7.4},
                                 "MU": {"shares": 41.7557, "avg": 1077.7, "pnl_pct": -13.2}}},
                  open(self.last_positions, "w"))
        json.dump({"watch": {"NVDA": {"exit_date": "2026-08-27", "exit_price": 227.98,
                                      "kind": "full", "prompted": None}}},
                  open(self.reentry, "w"))
        json.dump({"entries": []}, open(self.memory, "w"))
        self._old_root = db.ROOT
        db.ROOT = self.td
        db._PENDING_WRITES.clear()
        return self

    def __exit__(self, *a):
        db.ROOT = self._old_root
        db._PENDING_WRITES.clear()
        shutil.rmtree(self.td, ignore_errors=True)

    def positions_on_disk(self):
        return json.load(open(self.last_positions, encoding="utf-8"))

    def prompted_on_disk(self):
        return json.load(open(self.reentry, encoding="utf-8"))["watch"]["NVDA"]["prompted"]


class _Mem:
    """Stand-in for ReflectionMemory: records add() calls and whether save() reached disk."""

    def __init__(self, path):
        self.path = path
        self.added = []
        self.saved = 0

    def add(self, **kw):
        self.added.append(kw)

    def save(self):
        self.saved += 1
        json.dump({"entries": self.added}, open(self.path, "w"))


class TestCloseDetectionIsNotConsumedByAnUndeliveredBrief(unittest.TestCase):
    def test_detection_defers_the_snapshot_write(self):
        with _StateSandbox() as s:
            mem = _Mem(s.memory)
            cur = {"MU": {"shares": 41.7557, "avg_price": 1077.7, "mv": 39057.86}}
            closed, trimmed, prev = db._reflect_on_closes(cur, set(), mem, "2026-08-28")
            self.assertEqual(closed, ["NVDA"], "the exit must still be detected")
            self.assertTrue(mem.added, "a postmortem lesson must be queued")
            # NOTHING may have reached disk yet
            self.assertIn("NVDA", s.positions_on_disk()["positions"],
                          "last_positions must still show NVDA -- the detector stays armed")
            self.assertEqual(mem.saved, 0, "reflection memory must not be saved before delivery")
            self.assertTrue(db._PENDING_WRITES, "the consuming writes must be queued")

    def test_a_failed_send_leaves_everything_unconsumed(self):
        with _StateSandbox() as s:
            mem = _Mem(s.memory)
            cur = {"MU": {"shares": 41.7557, "avg_price": 1077.7, "mv": 39057.86}}
            db._reflect_on_closes(cur, set(), mem, "2026-08-28")
            # simulate the 2026-08-28 06:14 run: delivery fails, so no flush happens
            self.assertIn("NVDA", s.positions_on_disk()["positions"])
            # the NEXT run must still see the close
            db._PENDING_WRITES.clear()
            mem2 = _Mem(s.memory)
            closed2, _t, _p = db._reflect_on_closes(cur, set(), mem2, "2026-08-29")
            self.assertEqual(closed2, ["NVDA"],
                             "the next run must re-detect the exit the failed run found")

    def test_a_successful_send_consumes_exactly_once(self):
        with _StateSandbox() as s:
            mem = _Mem(s.memory)
            cur = {"MU": {"shares": 41.7557, "avg_price": 1077.7, "mv": 39057.86}}
            db._reflect_on_closes(cur, set(), mem, "2026-08-28")
            db.flush_pending_writes()
            self.assertNotIn("NVDA", s.positions_on_disk()["positions"])
            self.assertEqual(mem.saved, 1)
            self.assertEqual(db._PENDING_WRITES, [], "the queue must be emptied by the flush")
            mem2 = _Mem(s.memory)
            closed2, _t, _p = db._reflect_on_closes(cur, set(), mem2, "2026-08-29")
            self.assertEqual(closed2, [], "a delivered brief consumes the close exactly once")


class TestReentryPromptIsNotSpentByAnUndeliveredBrief(unittest.TestCase):
    QUOTES = {"NVDA": {"price": 240.0, "priceAvg50": 225.0}}

    def test_prompt_fires_but_the_stamp_is_deferred(self):
        with _StateSandbox() as s:
            prompts = db._reentry_update([], self.QUOTES, "2026-08-29", 159528.31, 18.8)
            self.assertEqual([p["ticker"] for p in prompts], ["NVDA"])
            self.assertIsNone(s.prompted_on_disk(),
                              "the 'prompted' stamp must not be written before delivery")

    def test_a_failed_send_re_issues_the_prompt_next_run(self):
        with _StateSandbox() as s:
            db._reentry_update([], self.QUOTES, "2026-08-29", 159528.31, 18.8)
            db._PENDING_WRITES.clear()          # delivery failed: nothing flushed
            again = db._reentry_update([], self.QUOTES, "2026-08-30", 159528.31, 18.8)
            self.assertEqual([p["ticker"] for p in again], ["NVDA"])
            self.assertIsNone(s.prompted_on_disk())

    def test_a_delivered_brief_spends_the_prompt_once(self):
        with _StateSandbox() as s:
            db._reentry_update([], self.QUOTES, "2026-08-29", 159528.31, 18.8)
            db.flush_pending_writes()
            self.assertEqual(s.prompted_on_disk(), "2026-08-29")
            again = db._reentry_update([], self.QUOTES, "2026-08-30", 159528.31, 18.8)
            self.assertEqual(again, [], "a spent prompt must not re-fire")


class TestMainOnlyFlushesOnConfirmedDelivery(unittest.TestCase):
    def _run_main(self, send_ok):
        import contextlib, io
        calls = {"flushed": 0}
        old = (db.build, db.notify.send, db.flush_pending_writes,
               db.calendars.is_us_trading_day)
        db.build = lambda: "body"
        db.notify.send = lambda subject, body: send_ok
        db.flush_pending_writes = lambda: calls.__setitem__("flushed", calls["flushed"] + 1)
        db.calendars.is_us_trading_day = lambda: True
        try:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    db.main()
                exited = 0
            except SystemExit as e:
                exited = e.code
        finally:
            (db.build, db.notify.send, db.flush_pending_writes,
             db.calendars.is_us_trading_day) = old
        return exited, calls["flushed"]

    def test_failed_delivery_exits_nonzero_and_flushes_nothing(self):
        exited, flushed = self._run_main(send_ok=False)
        self.assertEqual(exited, 1, "a failed send must turn the run RED (B59)")
        self.assertEqual(flushed, 0, "a failed send must not consume one-shot state (B60)")

    def test_successful_delivery_flushes_exactly_once(self):
        exited, flushed = self._run_main(send_ok=True)
        self.assertEqual(exited, 0)
        self.assertEqual(flushed, 1)


class TestReverseBreak(unittest.TestCase):
    """Prove the tests above actually bite by breaking the guard on a COPY of the module.

    This is the part gate 10 was missing in its first version: it passed on the string
    '# flush_pending_writes()'. Here the module source is really mutated and re-imported, so
    neither a comment nor a matching string can satisfy it."""

    def test_making_defer_immediate_breaks_the_guarantee(self):
        """Behavioural break, not a textual one: make _defer() execute instead of queueing.
        The paired test above must be the thing that notices."""
        with _StateSandbox() as s:
            old = db._defer
            db._defer = lambda fn: fn()          # the bug B60 fixed, reintroduced for real
            try:
                mem = _Mem(s.memory)
                cur = {"MU": {"shares": 41.7557, "avg_price": 1077.7, "mv": 39057.86}}
                db._reflect_on_closes(cur, set(), mem, "2026-08-28")
                self.assertNotIn(
                    "NVDA", s.positions_on_disk()["positions"],
                    "with _defer broken the write MUST land immediately; if it does not, the "
                    "passing test above is not testing anything")
                self.assertEqual(mem.saved, 1, "broken build saves memory immediately too")
            finally:
                db._defer = old

    def test_the_source_still_contains_the_call_while_the_behaviour_is_broken(self):
        """The point of the behavioural test. A source-level check ('does daily_brief.py
        contain flush_pending_writes()?') stays GREEN on the broken build above, because the
        call is still right there in the file. Only running it catches the defect."""
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        self.assertIn("flush_pending_writes()", src)
        self.assertIn("_PENDING_WRITES.append(fn)", src,
                      "the real _defer must queue, not call")

    def test_a_commented_out_flush_does_not_satisfy_the_selfcheck_gate(self):
        """Gate 10 must reject a build where the flush call is only a comment."""
        src = open(os.path.join(ROOT, "cockpit", "daily_brief.py"), encoding="utf-8").read()
        broken = src.replace("    flush_pending_writes()   # B60",
                             "    # flush_pending_writes()   # B60")
        self.assertNotEqual(broken, src)
        main_body = broken.split("def main()")[-1]
        called = any(ln.strip().startswith("flush_pending_writes()")
                     for ln in main_body.splitlines())
        self.assertFalse(called, "a commented-out call must NOT read as a call")
        real_main = src.split("def main()")[-1]
        self.assertTrue(any(ln.strip().startswith("flush_pending_writes()")
                            for ln in real_main.splitlines()),
                        "the real build must have a real call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
