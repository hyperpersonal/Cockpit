#!/usr/bin/env python3
"""Cockpit self-check: mechanical guards so drift/omissions get CAUGHT, not forgotten.
Run before claiming 'done'; optional CI step. Pure stdlib.
Checks: (1) all modules compile, (2) behavioral config keys are used, (3) biweekly has parity with
daily (holdings_snapshot + position_caps), (4) surface TODOs, (5) flag UNEXPECTED dead config keys
(informational/constitution keys are allowlisted), (6) BACKLOG hygiene (no row both OPEN and DONE),
(7) done-manifest (every DONE claim must have a real code/doc fingerprint). Exit nonzero on hard fail."""
import ast, glob, re, sys, pathlib, yaml
ROOT = pathlib.Path(__file__).resolve().parent
SRC = {p: open(p, encoding="utf-8").read() for p in glob.glob(str(ROOT / "cockpit" / "*.py"))}
ALLSRC = "\n".join(SRC.values())
fail, warn = [], []

for p, s in SRC.items():
    try: ast.parse(s)
    except SyntaxError as e: fail.append(f"compile {pathlib.Path(p).name}: {e}")

MUST_USE = ["total_assets_usd", "net_liq_fallback", "single_name_hard_cap_pct_of_total",
            "no_chase_bias_threshold_pct", "hist_window_days", "news_max_age_days",
            "dilution_atm_disqualifier", "biweekly_anchor_date", "subthemes", "holdings", "exclude"]
# informational / constitution-only keys not expected in code:
ALLOW_UNUSED = {"daily_brief_cron_utc", "biweekly_review_cron_utc", "skip_us_holidays", "deep_dive",
                "primary", "positions", "cross_validate", "fail_open", "strategy", "schwab_core",
                "instrument", "target_usd", "note", "redlines", "role", "vol_window_days", "corr_window_days"}
cfg_raw = open(ROOT / "config.yaml", encoding="utf-8").read()
cfg = yaml.safe_load(cfg_raw)
for k in MUST_USE:
    if k in cfg_raw and k not in ALLSRC:
        fail.append(f"config key '{k}' declared but UNUSED in code (behavioral key must be wired)")

def leaves(d, out):
    if isinstance(d, dict):
        for k, v in d.items():
            out.add(k); leaves(v, out)
    elif isinstance(d, list):
        for v in d: leaves(v, out)
allk = set(); leaves(cfg, allk)
data_keys = set(cfg.get("subthemes", {}).keys()) | {h.get("ticker") for h in cfg.get("holdings", []) if isinstance(h, dict)}
for k in sorted(allk):
    if isinstance(k, str) and re.match(r"^[a-z_]+$", k) and k not in ALLSRC and k not in ALLOW_UNUSED and k not in MUST_USE and k not in data_keys:
        warn.append(f"config key '{k}' appears unreferenced (add to code or to selfcheck ALLOW_UNUSED)")

bi = SRC.get(str(ROOT / "cockpit" / "biweekly_review.py"), "")
for needle, why in [("holdings_snapshot", "real IBKR holdings"), ("position_caps", "risk engine"),
                    ("_holdings_snapshot", "snapshot builder")]:
    if needle not in bi:
        fail.append(f"biweekly_review.py missing '{needle}' ({why}) -- STALE vs daily")

for p, s in SRC.items():
    for i, line in enumerate(s.splitlines(), 1):
        if re.search(r"\bTODO\b|\bFIXME\b", line):
            warn.append(f"TODO {pathlib.Path(p).name}:{i}: {line.strip()[:80]}")

# (6) BACKLOG hygiene: an item row must not be BOTH open and resolved (the stale-OPEN bug).
bl = ROOT / "BACKLOG.md"
backlog_txt = bl.read_text(encoding="utf-8") if bl.exists() else ""
for i, line in enumerate(backlog_txt.splitlines(), 1):
    m = re.match(r"^\|\s*(B\d+|D\d+)\b", line)
    if not m:
        continue
    has_open = "OPEN" in line
    has_done = ("DONE" in line) or ("WON'T-DO" in line) or ("✅" in line) or ("❌" in line)
    if has_open and has_done:
        fail.append(f"BACKLOG {m.group(1)} (line {i}): row marked BOTH OPEN and DONE -- ambiguous")

# (7) Done-manifest: anything claimed DONE must have its code/doc fingerprint present, so a 'DONE'
# label can never be just prose. id -> (path relative to ROOT, substring that must exist).
DONE_FINGERPRINTS = {
    "B4":  ("cockpit/biweekly_review.py", "_performance"),
    "B5":  ("cockpit/daily_brief.py", "_reflect_on_closes"),
    "B7":  ("config.yaml", "hist_window_days"),
    "B8":  ("README.md", "EWMA"),
    "B10": ("cockpit/screener.py", "_lifecycle"),
    "B11": ("cockpit/risk.py", "eff_corr"),
    "B12": ("cockpit/calendars.py", "_session_utc"),
    "B13": ("cockpit/llm.py", "temperature"),
    "B19": ("cockpit/risk.py", "n_theme_peers"),
    "B20": ("cockpit/daily_brief.py", "ibkr_mv_refonly"),
    "B22": ("cockpit/daily_brief.py", "_candidates_md"),
}
for bid, (relpath, needle) in DONE_FINGERPRINTS.items():
    if not re.search(rf"^\|\s*{bid}\b.*(DONE|✅)", backlog_txt, re.M):
        continue                                   # only enforce ids BACKLOG actually claims DONE
    fp = ROOT / relpath
    txt = fp.read_text(encoding="utf-8") if fp.exists() else ""
    if needle not in txt:
        fail.append(f"BACKLOG {bid} marked DONE but fingerprint '{needle}' MISSING in {relpath} "
                    f"(DONE claim not backed by code)")

print("=== Cockpit self-check ===")
for w in warn: print("  WARN:", w)
if fail:
    print("\nHARD FAILURES:")
    for f in fail: print("  FAIL:", f)
    print(f"\n{len(fail)} failure(s), {len(warn)} warning(s)."); sys.exit(1)
print(f"\nPASS (0 hard failures, {len(warn)} warning(s)).")
