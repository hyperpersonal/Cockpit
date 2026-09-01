"""Stdlib test runner -- no pytest required (the delivery machine has none).

    python3 tests/run.py            # both suites
    python3 tests/run.py baseline   # only the frozen-account suite

Exit code 0 only when the BASELINE suite is fully green AND the consistency suite has
no unexpected regressions. The consistency suite is expected to be RED until the
unified Decision layer lands; that expectation is stated here, in code, so nobody has
to remember it.
"""
from __future__ import annotations
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

# Tests that are RED ON PURPOSE right now. Remove a name from this set the moment its
# feature lands -- an entry left here after the fact would hide a real regression.
EXPECTED_RED = set()


def _short(test):
    return "%s.%s" % (test.__class__.__name__, test._testMethodName)


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    if which in ("all", "baseline"):
        suite.addTests(loader.discover(HERE, pattern="test_baseline.py", top_level_dir=HERE))
    if which in ("all", "consistency"):
        suite.addTests(loader.discover(HERE, pattern="test_decision_consistency.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_resolution_*.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_ledger.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_render_*.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_b60_*.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_facts.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_safety_and_freshness.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_policy_consistency.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_theme_allocation.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_pipeline_sequential.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_entry_decisions.py", top_level_dir=HERE))
        suite.addTests(loader.discover(HERE, pattern="test_p0a_*.py", top_level_dir=HERE))

    res = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    red = {_short(t) for t, _ in list(res.failures) + list(res.errors)}

    unexpected = sorted(red - EXPECTED_RED)
    fixed = sorted(EXPECTED_RED - red) if which in ("all", "consistency") else []

    print("\n" + "=" * 68)
    print("ran %d | failed %d | expected-red %d" % (res.testsRun, len(red), len(EXPECTED_RED)))
    if unexpected:
        print("\nUNEXPECTED FAILURES (real regressions):")
        for n in unexpected:
            print("  x", n)
    if fixed:
        print("\nNOW GREEN -- delete these from EXPECTED_RED in tests/run.py:")
        for n in fixed:
            print("  +", n)
    if not unexpected and not fixed:
        print("\nstate matches expectations: baseline green, %d consistency tests still red by design."
              % len(EXPECTED_RED))
    print("=" * 68)
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
