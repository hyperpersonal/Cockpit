#!/usr/bin/env python3
"""Cockpit self-check: mechanical guards so drift/omissions get CAUGHT, not forgotten.
Run before claiming 'done', and (optionally) as a CI step. Pure stdlib.
Checks: (1) all modules compile, (2) behavioral config keys are actually used,
(3) biweekly has parity with daily (holdings_snapshot + position_caps), (4) surface TODOs,
(5) flag config keys unreferenced anywhere. Exit nonzero on hard failures."""
import ast, glob, re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent
SRC = {p: open(p, encoding="utf-8").read() for p in glob.glob(str(ROOT / "cockpit" / "*.py"))}
ALLSRC = "\n".join(SRC.values())
fail, warn = [], []

# 1. compile
for p, s in SRC.items():
    try: ast.parse(s)
    except SyntaxError as e: fail.append(f"compile: {pathlib.Path(p).name}: {e}")

# 2. behavioral config keys MUST be referenced in code
MUST_USE = ["total_assets_usd", "net_liq_fallback", "single_name_hard_cap_pct_of_total",
            "no_chase_bias_threshold_pct", "biweekly_anchor_date", "subthemes", "holdings", "exclude"]
cfg = open(ROOT / "config.yaml", encoding="utf-8").read()
for k in MUST_USE:
    if k in cfg and k not in ALLSRC:
        fail.append(f"config key '{k}' is declared but NOT used in any module (dead/behavioral)")

# 2b. flag config keys that LOOK behavioral but are unused (warn) -- catches vol_window_days etc.
SUSPECT = ["vol_window_days", "corr_window_days", "news_max_age_days", "dilution_atm_disqualifier"]
for k in SUSPECT:
    if k in cfg and k not in ALLSRC:
        warn.append(f"config key '{k}' unreferenced in code (dead key / misleading -- remove or wire)")

# 3. biweekly parity with daily (must show real IBKR holdings + use the risk engine)
bi = SRC.get(str(ROOT / "cockpit" / "biweekly_review.py"), "")
for needle, why in [("holdings_snapshot", "real IBKR holdings table"),
                    ("position_caps", "upgraded risk engine"),
                    ("_holdings_snapshot", "snapshot builder")]:
    if needle not in bi:
        fail.append(f"biweekly_review.py missing '{needle}' ({why}) -- STALE vs daily_brief")

# 4. TODO surface
for p, s in SRC.items():
    for i, line in enumerate(s.splitlines(), 1):
        if re.search(r"\bTODO\b|\bFIXME\b", line):
            warn.append(f"TODO {pathlib.Path(p).name}:{i}: {line.strip()[:80]}")

print("=== Cockpit self-check ===")
for w in warn: print("  WARN:", w)
if fail:
    print("\nHARD FAILURES:")
    for f in fail: print("  FAIL:", f)
    print(f"\n{len(fail)} failure(s), {len(warn)} warning(s).")
    sys.exit(1)
print(f"\nPASS (0 hard failures, {len(warn)} warning(s)).")
