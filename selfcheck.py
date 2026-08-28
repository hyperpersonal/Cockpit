#!/usr/bin/env python3
"""Cockpit self-check: mechanical guards so drift/omissions get CAUGHT, not forgotten.
Run before claiming 'done'; optional CI step. Pure stdlib.
Checks: (1) all modules compile, (2) behavioral config keys are used, (3) biweekly has parity with
daily (holdings_snapshot + position_caps), (4) surface TODOs, (5) flag UNEXPECTED dead config keys
(informational/constitution keys are allowlisted), (6) BACKLOG hygiene (no row both OPEN and DONE),
(7) done-manifest (every DONE claim must have a real code/doc fingerprint),
(8) every held ticker resolves to a layer (B53),
(9) every holdings[].role carries a verification stamp (B54 -- unverified annotations drive real sell advice),
(10) one-shot state is spent only on a DELIVERED brief (B60).
Exit nonzero on hard fail."""
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
            "dilution_atm_disqualifier", "biweekly_anchor_date", "subthemes", "holdings", "exclude",
            "intraday_move_pct"]
# informational / constitution-only keys not expected in code:
ALLOW_UNUSED = {"daily_brief_cron_utc", "biweekly_review_cron_utc", "skip_us_holidays", "deep_dive",
                "primary", "positions", "cross_validate", "fail_open", "strategy", "schwab_core",
                "instrument", "target_usd", "note", "redlines", "role", "vol_window_days", "corr_window_days",
                "alerts", "intraday_cron_utc", "news_alerts"}
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
    "B14": ("cockpit/intraday_alert.py", "build_alerts"),
    "B17": ("cockpit/crossval.py", "edgar_dossier"),
    "B19": ("cockpit/risk.py", "n_theme_peers"),
    "B20": ("cockpit/daily_brief.py", "ibkr_mv_refonly"),
    "B22": ("cockpit/daily_brief.py", "_candidates_md"),
    "B24": ("cockpit/daily_brief.py", "B24"),
    "B29": ("cockpit/biweekly_review.py", "_adherence_md"),
    "B31": ("cockpit/fmp.py", "fall back to per-symbol"),
    "B32": ("cockpit/daily_brief.py", "_action_plan"),
    "B33": ("cockpit/daily_brief.py", "IBKR-DRIVEN"),
    "B34": ("cockpit/daily_brief.py", "_opens_and_violations"),
    "B37": ("cockpit/daily_brief.py", "AVERAGED DOWN"),
    "B38": ("cockpit/screener.py", "wait_20pct"),
    "B39": ("cockpit/screener.py", "market_position"),
    "B42": ("cockpit/daily_brief.py", "_lamps_md"),
    "B43": (".github/workflows/daily-brief.yml", "timeout-minutes"),
    "B36": ("cockpit/daily_brief.py", "_theme_exposure"),
    "B44": ("cockpit/daily_brief.py", "_reentry_update"),
    "B45": ("cockpit/daily_brief.py", "profit_take_trigger_pct"),
    "B46": ("cockpit/daily_brief.py", "B46"),
    "B28": ("cockpit/scanner.py", "_clusters"),
    "B41": ("cockpit/scanner.py", "_growth_ok"),
    "B48": ("cockpit/daily_brief.py", "_position_audit"),
    "B49": ("cockpit/biweekly_review.py", "_attribution"),
    "B50": ("cockpit/daily_brief.py", "_exit_tracking"),
    "B51": ("tools/b51_stop_replay.py", "inbreach"),
    "B53": ("cockpit/daily_brief.py", "OUTSIDE_LAYER"),
    "B54": ("selfcheck.py", "\u7f3a\u6838\u5b9e\u6807\u8bb0"),
    "B55": ("cockpit/scanner.py", "fresh_md"),
    "B57": (".github/workflows/daily-brief.yml", "30 22"),
    "B58": ("cockpit/daily_brief.py", "prior20"),
    "B59": ("cockpit/daily_brief.py", "EMAIL SEND FAILED"),
    "B60": ("cockpit/daily_brief.py", "flush_pending_writes"),
}
# NOT yet fingerprinted (older DONE rows, coverage gap I have not closed):
# B1 B2 B3 B6 B9 B23 B25 B27 B40 B52 -- gate 7 is silent about these by construction.
for bid, (relpath, needle) in DONE_FINGERPRINTS.items():
    if not re.search(rf"^\|\s*{bid}\b.*(DONE|✅)", backlog_txt, re.M):
        continue                                   # only enforce ids BACKLOG actually claims DONE
    fp = ROOT / relpath
    txt = fp.read_text(encoding="utf-8") if fp.exists() else ""
    if needle not in txt:
        fail.append(f"BACKLOG {bid} marked DONE but fingerprint '{needle}' MISSING in {relpath} "
                    f"(DONE claim not backed by code)")

# (8) B53: every held ticker must resolve to a LAYER (subthemes.names or risk.theme_overrides).
# An unmapped holding is silent: B36 exempts "unmapped" from theme alerts, and B48's layer ranking
# lumps everything unmapped into one pseudo-layer. On 2026-08-24 SKHY (31% of NAV) was unmapped, so
# the memory layer read 31% instead of 63% and no alert ever fired. Mechanical guard, not a habit.
_mapped = set()
for _v in (cfg.get("subthemes") or {}).values():
    _mapped |= set((_v or {}).get("names") or [])
_mapped |= set(((cfg.get("risk") or {}).get("theme_overrides") or {}).keys())
_held = {h.get("ticker") for h in (cfg.get("holdings") or []) if isinstance(h, dict)}
_unmapped = sorted(t for t in (_held - set(cfg.get("exclude") or [])) if t and t not in _mapped)
if _unmapped:
    fail.append("holdings with NO subtheme/theme_override mapping (silently breaks B36 theme alerts "
                "and B48 layer ranking): " + ", ".join(_unmapped))

# (9) B54: every holdings[].role annotation that makes a factual claim about the OUTSIDE WORLD
# must carry a verification stamp. An unverified annotation ("CCXI = SPAC空壳，体系外") once drove
# theme_overrides -> B48 disposal ordering and put a real de-SPAC at the top of a liquidation list.
# A memory rule only binds the agent; this binds every session.
_bad = []
for _h in (cfg.get("holdings") or []):
    if not isinstance(_h, dict): continue
    _r = str(_h.get("role") or "")
    if not _r:
        _bad.append("%s(无 role)" % _h.get("ticker")); continue
    if ("核实" not in _r) and ("未核实" not in _r):
        _bad.append(_h.get("ticker"))
if _bad:
    fail.append("holdings[].role 缺核实标记（须含「核实 YYYY-MM-DD，源=...」或明写「未核实」）: "
                + ", ".join(str(x) for x in _bad))

# (10) B60: one-shot state (close detection, re-entry prompts) must be spent only on a DELIVERED
# brief. On 2026-08-28 an undelivered 06:14 UTC run consumed the NVDA/AVGO exit postmortem and the
# NVDA re-entry prompt; the delivered 06:55 run had nothing left to report. If daily_brief queues
# deferred writes but main() never flushes them, the opposite failure appears -- state silently
# never advances -- so both halves are checked.
_db = SRC.get(str(ROOT / "cockpit" / "daily_brief.py"), "")
if "_defer(" in _db:
    _main = _db.split("def main()")[-1]
    # must be a REAL call, not a commented-out one (the first version of this gate passed on
    # "# flush_pending_writes()" -- a checker that accepts a comment checks nothing)
    _called = any(ln.strip().startswith("flush_pending_writes()") for ln in _main.splitlines())
    if not _called:
        fail.append("daily_brief.main() never calls flush_pending_writes() -- B60 deferred one-shot "
                    "state would be dropped on EVERY run")
    if 'json.dump({"date": today, "positions": cur}' in _db:
        fail.append("daily_brief writes last_positions.json EAGERLY again -- B60 regression: an "
                    "undelivered brief would consume the close detection")
    if 'w["prompted"] = today' in _db:
        fail.append("daily_brief stamps reentry 'prompted' EAGERLY again -- B60 regression: an "
                    "undelivered brief would spend the re-entry prompt")

print("=== Cockpit self-check ===")
for w in warn: print("  WARN:", w)
if fail:
    print("\nHARD FAILURES:")
    for f in fail: print("  FAIL:", f)
    print(f"\n{len(fail)} failure(s), {len(warn)} warning(s)."); sys.exit(1)
print(f"\nPASS (0 hard failures, {len(warn)} warning(s)).")
