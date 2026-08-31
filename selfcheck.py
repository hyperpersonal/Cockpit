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
import ast, glob, os, re, sys, pathlib, yaml
ROOT = pathlib.Path(__file__).resolve().parent
# R2/R8 (2026-08-28): RECURSIVE. The refactor moves logic into cockpit/{domain,engine,rules,
# ledger,render}/, and a flat "cockpit/*.py" glob would have made every gate below silently stop
# seeing that code -- the config-key-usage warning went quiet for three brand-new keys the moment
# they were only read inside cockpit/engine/. A check that stops applying without saying so is the
# exact failure mode this file exists to prevent.
SRC = {p: open(p, encoding="utf-8").read()
       for p in glob.glob(str(ROOT / "cockpit" / "**" / "*.py"), recursive=True)}
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

# (11) 2026-08-28 refactor: the golden regression suite is part of the gate, not a side file.
# It freezes the broker's own numbers (IBKR statement U22209151) and the resolution of the
# 2026-08-27 book, and it carries seven paired invariant tests -- each written so the checker
# is first proven to CATCH the historical defect before it is pointed at the system. A suite
# that lives beside the code but is never run is exactly this project's signature failure.
_tests = ROOT / "tests" / "run.py"
if not _tests.exists():
    fail.append("tests/run.py missing -- the golden regression suite is the gate for the "
                "unified Decision layer; without it nothing enforces one-amount-per-ticker")
else:
    import subprocess
    _r = subprocess.run([sys.executable, str(_tests)], capture_output=True, text=True, cwd=str(ROOT))
    if _r.returncode != 0:
        _tail = "\n      ".join([l for l in (_r.stdout or "").splitlines() if l.startswith("  x")][:8])
        fail.append("golden regression suite FAILED (tests/run.py exit %d):\n      %s"
                    % (_r.returncode, _tail or "see: python3 tests/run.py"))

# (12) the adjudicator must remain the ONLY place a trade amount is produced. Every rule module
# may emit proposals; none of them may render money into text.
_rule_dir = ROOT / "cockpit" / "rules"
if _rule_dir.exists():
    for _f in sorted(_rule_dir.glob("*.py")):
        _t = _f.read_text(encoding="utf-8")
        if "notify.send" in _t or "def _md" in _t or "## " in _t:
            fail.append("rule module %s renders output -- rules may only emit RuleProposal" % _f.name)

# (13) R10: exactly ONE sizing path. daily_brief must not regrow a function that renders a
# trade amount of its own. The three deleted paths (_action_plan, _dispose_order, and the
# audit table's 该减$ column) are named explicitly because they are what produced three
# different totals for one book on 2026-08-28.
_db_src = SRC.get(str(ROOT / "cockpit" / "daily_brief.py"), "")
for _dead in ("def _action_plan(", "def _dispose_order("):
    if _dead in _db_src:
        fail.append("daily_brief regrew %s -- trade amounts may come only from "
                    "cockpit/engine/resolve.py" % _dead.strip("def ("))
# match the markdown COLUMN, not the words. This gate tripped three times on comments that
# merely NAMED the removed column -- the same "a comment fooled the checker" failure as gate 10,
# in mirror image: a comment BREAKING a checker rather than satisfying one.
if re.search(r"\|\s*该减\$\s*\|", _db_src) or re.search(r"\|\s*目标仓位\$\s*\|", _db_src):
    fail.append("the 🩺 audit table regrew a 该减$ / 目标仓位$ COLUMN -- an appendix may explain "
                "the arithmetic but must not produce a second instruction")
_render_dir = ROOT / "cockpit" / "render"
if _render_dir.exists():
    for _f in sorted(_render_dir.glob("*.py")):
        _t = _f.read_text(encoding="utf-8")
        _body = "\n".join(l for l in _t.splitlines() if not l.strip().startswith("#"))
        for _tok in ("cap_usd", "market_value", "* price", "/ price"):
            if _tok in _body:
                fail.append("renderer %s performs sizing (%r) -- renderers read Decision "
                            "fields only" % (_f.name, _tok))

# (14) Intraday Alert stays DISABLED and stays OUT of the unified engine (user constraint,
# 2026-08-28). It was disabled in the GitHub Actions UI on/around 2026-07-17 -- the repo cannot
# see that, so what IS checkable is enforced here: none of the decision layers may import it,
# and its pre-B48 stop formula (max(200DMA, cost x 0.8)) may not leak into the adjudicator.
# Two stop definitions in one system is how you get two different answers.
_ENGINE_DIRS = ["domain", "engine", "rules", "render", "ledger"]
for _sub in _ENGINE_DIRS:
    _d = ROOT / "cockpit" / _sub
    if not _d.exists():
        continue
    for _f in sorted(_d.glob("*.py")):
        _t = _f.read_text(encoding="utf-8")
        if "intraday_alert" in _t:
            fail.append("cockpit/%s/%s references intraday_alert -- the disabled intraday path "
                        "must not enter the decision engine" % (_sub, _f.name))
        _code = "\n".join(l for l in _t.splitlines() if not l.strip().startswith("#"))
        if re.search(r"avg\s*\*\s*0\.8", _code) or re.search(r"priceAvg200.*0\.8", _code):
            fail.append("cockpit/%s/%s contains the pre-B48 stop formula (cost x 0.8) -- the "
                        "execution stop is the 20-day closing low x 0.99, one definition only"
                        % (_sub, _f.name))
if not (ROOT / ".github" / "workflows" / "intraday-alert.yml").exists():
    warn.append("intraday-alert.yml is gone; if that was deliberate, close the BACKLOG item "
                "that tracks its removal rather than leaving the registry stale")

# (15) Safety boundaries must survive every refactor. These are the lines that were agreed with
# the user, not implementation details, and a structural change is exactly when they get lost.
_all_src = ALLSRC
for _bad in ("place_order", "submit_order", "create_order(", "cancel_order", "transfer_funds"):
    if _bad in _all_src:
        fail.append("order/transfer call %r found in cockpit/ -- the system never trades, "
                    "transfers or cancels; it only proposes" % _bad)
if "IBKR" not in (cfg.get("exclude") or []):
    fail.append("config.exclude no longer contains IBKR -- the unvested stock-award grant would "
                "re-enter the active book as a tradable position")
if not ((cfg.get("risk") or {}).get("leveraged_etf_max_pct_nav")):
    fail.append("risk.leveraged_etf_max_pct_nav missing -- red line v2's leveraged-ETF ceiling "
                "would silently stop being enforced")
if not ((cfg.get("risk") or {}).get("leverage_factors")):
    fail.append("risk.leverage_factors missing -- a 2x ETF would count at 1x in theme exposure")
if not ((cfg.get("risk") or {}).get("fact_expiry_days")):
    fail.append("risk.fact_expiry_days missing -- verified external facts would never expire")
_scope_terms = ["QQQ 定投", "回调子弹", "\u5609\u4fe1", "\u7a0e\u52a1\u7b56\u7565", "\u8d44\u672c\u5229\u5f97\u7a0e"]
for _sub in _ENGINE_DIRS + [""]:
    _d = (ROOT / "cockpit" / _sub) if _sub else (ROOT / "cockpit")
    for _f in sorted(_d.glob("*.py")):
        _t = _f.read_text(encoding="utf-8")
        _hits = [w for w in _scope_terms if w in _t]
        if _hits:
            fail.append("out-of-scope content in %s: %s (Schwab/QQQ and tax are excluded from "
                        "this system by decision)" % (_f.name, ", ".join(_hits)))

# (16) The single-name hard cap has exactly ONE definition (user decision 2026-08-28: a fixed
# $30,000 absolute ceiling). It must not be re-derived from account.total_assets_usd x
# risk.single_name_hard_cap_pct_of_total anywhere -- that derivation made the ceiling drift
# whenever the Schwab side, which this system does not manage, was re-estimated.
try:
    sys.path.insert(0, str(ROOT))
    from cockpit.domain.policy import hard_cap_usd as _hc
    _cap = _hc(cfg)
    if abs(_cap - 30000.0) > 0.01:
        fail.append("single-name hard cap resolves to $%.2f, not the agreed fixed $30,000" % _cap)
    if (cfg.get("risk") or {}).get("single_name_hard_cap_usd") is None:
        warn.append("risk.single_name_hard_cap_usd absent -- running on the deprecated "
                    "total_assets x pct fallback, which is kept for one migration cycle only")
except Exception as _e:
    fail.append("cannot resolve the single-name hard cap: %s" % _e)
_CAP_DERIV = re.compile(r"total_assets\w*\s*\*|single_name_hard_cap_pct_of_total\W*\]?\s*/")
for _p, _t in SRC.items():
    if _p.endswith(os.sep.join(["domain", "policy.py"])) or _p.endswith("domain/policy.py"):
        continue
    _code = "\n".join(l for l in _t.splitlines() if not l.strip().startswith("#"))
    if _CAP_DERIV.search(_code):
        fail.append("%s derives the single-name hard cap itself -- it must call "
                    "cockpit.domain.policy.hard_cap_usd()" % os.path.basename(_p))

# (17) Red line v2 clause 4: the leveraged-ETF hard stop must be WIRED, not a spare constant.
_acct = SRC.get(str(ROOT / "cockpit" / "rules" / "account.py"), "")
if "RULE_LEV_STOP" in _acct and _acct.count("RULE_LEV_STOP") < 3:
    fail.append("account.RULE_LEV_STOP is declared but never emitted -- the leveraged-ETF hard "
                "stop would be a constant, not a rule (this was true until 2026-08-28)")
if "leveraged_etf_hard_stop_pct" not in _acct:
    fail.append("the leveraged-ETF hard stop no longer reads risk.leveraged_etf_hard_stop_pct")
if not ((cfg.get("risk") or {}).get("leveraged_etf_hard_stop_pct")):
    fail.append("risk.leveraged_etf_hard_stop_pct missing -- red line v2 clause 4 unenforced")

# (18) Heat is a gate on ADDING risk and never a sell instruction. The rendered gate must not
# argue with itself: a ⛔ next to "this is a warning, not a prohibition" is unreadable.
_dbs = SRC.get(str(ROOT / "cockpit" / "daily_brief.py"), "")
_dbcode = "\n".join(l for l in _dbs.splitlines() if not l.strip().startswith("#"))
for _contradiction in ("这是警示，不是禁令", "仅警示，不阻止"):
    if _contradiction in _dbcode:
        fail.append("the buying gate contradicts itself (%r): heat DOES stop new buying; what "
                    "it must not do is demand selling" % _contradiction)
if "暂停新增风险" not in _dbcode:
    fail.append("the buying gate no longer states that heat only pauses ADDING risk")

# (19) Theme-concentration allocation is scheme (a) (user decision 2026-08-28): leveraged
# members first, then ordinary members pro-rata. RS is observation only and must not reach the
# allocation path -- under the rule this replaced, flipping RAM's RS to the strongest in its
# layer left the 2x ETF untouched and sold $26,097 of an ordinary holding instead.
_conc = SRC.get(str(ROOT / "cockpit" / "rules" / "concentration.py"), "")
_conc_code = "\n".join(l for l in _conc.splitlines() if not l.strip().startswith("#"))
_conc_body = _conc_code.split('"""', 2)[-1] if '"""' in _conc_code else _conc_code
for _tok in ("rs_by_ticker", "rs=", '"rs"'):
    if _tok in _conc_body:
        fail.append("RS reached the theme-allocation path (%r in concentration.py) -- RS is "
                    "observation only; it may not decide a binding amount or an ordering" % _tok)
if "scheme_a_leveraged_first" not in _conc:
    fail.append("the theme allocation no longer records scheme_a_leveraged_first -- either the "
                "agreed allocation changed without a decision, or its evidence stopped saying so")

# (20) Adjudication must be ORDERED and portfolio-level. The flattener
# (engine.resolve.resolve_decisions) takes the strictest value per ticker from proposals that
# were all generated against the ORIGINAL book -- it cannot know that another ticker's exit
# already satisfied a ceiling. On 2026-08-28 that produced a $70,000 instruction where the
# whole requirement was $60,000: an ordinary holding stop-exited while the theme rule, sized
# against the exposure that holding was still nominally carrying, zeroed a 2x ETF as well.
_pipe = ROOT / "cockpit" / "engine" / "pipeline.py"
if not _pipe.exists():
    fail.append("cockpit/engine/pipeline.py missing -- ordered portfolio-level adjudication is "
                "the fix for cross-ticker over-selling and must not be removed")
else:
    _pt = _pipe.read_text(encoding="utf-8")
    if "PASS A" not in _pt or "HARD_EXIT" not in _pt:
        fail.append("pipeline.py no longer hoists unconditional exits ahead of sizing -- "
                    "ceilings would again budget against exposure that is already leaving")
_dbs2 = SRC.get(str(ROOT / "cockpit" / "daily_brief.py"), "")
if "pipeline.adjudicate(" not in _dbs2:
    fail.append("daily_brief no longer calls pipeline.adjudicate() -- the brief would be back "
                "on the per-ticker flattener")
if re.search(r"^\s*decisions\s*=\s*resolve_decisions\(", _dbs2, re.M):
    fail.append("daily_brief calls resolve_decisions() directly; real adjudication goes "
                "through pipeline.adjudicate()")

# (21) Every module-level name a module CALLS must exist. Python resolves names at call time,
# so `compileall` is happy with a function that was deleted while its call site stayed -- the
# brief then dies with NameError at runtime. This happened on 2026-08-28: a block replacement
# swallowed _cash_routing_md and _followups_md and left both calls in build(). Only the BACKLOG
# fingerprint gate noticed, and only because one of them carried a B-number.
for _p, _t in sorted(SRC.items()):
    try:
        _tree = ast.parse(_t)
    except SyntaxError:
        continue
    _defined = {n.name for n in ast.walk(_tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    _defined |= {t2.id for n in ast.walk(_tree) if isinstance(n, ast.Assign)
                 for t2 in n.targets if isinstance(t2, ast.Name)}
    for _n in ast.walk(_tree):
        if isinstance(_n, ast.Import):
            _defined |= {(a.asname or a.name.split(".")[0]) for a in _n.names}
        elif isinstance(_n, ast.ImportFrom):
            _defined |= {(a.asname or a.name) for a in _n.names}
        elif isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _defined |= {a.arg for a in _n.args.args + _n.args.kwonlyargs}
            for _s2 in ast.walk(_n):
                if isinstance(_s2, ast.Assign):
                    _defined |= {t2.id for t2 in _s2.targets if isinstance(t2, ast.Name)}
                elif isinstance(_s2, (ast.For, ast.comprehension)):
                    _tg = getattr(_s2, "target", None)
                    if isinstance(_tg, ast.Name):
                        _defined.add(_tg.id)
                    elif isinstance(_tg, ast.Tuple):
                        _defined |= {e.id for e in _tg.elts if isinstance(e, ast.Name)}
                elif isinstance(_s2, (ast.Lambda,)):
                    _defined |= {a.arg for a in _s2.args.args}
                elif isinstance(_s2, ast.ExceptHandler) and _s2.name:
                    _defined.add(_s2.name)
                elif isinstance(_s2, ast.withitem) and isinstance(_s2.optional_vars, ast.Name):
                    _defined.add(_s2.optional_vars.id)
    _called = {n.func.id for n in ast.walk(_tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    _missing = sorted(c for c in _called
                      if c.startswith("_") and c not in _defined and not hasattr(__builtins__, c))
    if _missing:
        fail.append("%s calls names that are not defined in it: %s -- the module imports and "
                    "compiles, and dies at runtime" % (os.path.basename(_p), ", ".join(_missing)))

# (22) Entries must be adjudicated, not printed. Until 2026-08-28 sells went through the
# Decision layer while buys did not: _followups_md() printed "可考虑买 X 股 ≈ $Y，止损=入场−8%",
# the re-entry prompt printed its own share count and value, and the radar table printed a
# "1% 风险示例股数" column. Three more producers of executable numbers outside the adjudicator.
_pipe_src = SRC.get(str(ROOT / "cockpit" / "engine" / "pipeline.py"), "")
if "TIER_ENTRY" not in _pipe_src:
    fail.append("pipeline.default_stages() has no TIER_ENTRY stage -- buys would again be "
                "generated outside the Decision layer")
if "if p.ticker not in original" not in _pipe_src:
    fail.append("pipeline no longer seeds a never-held ticker for a BUY -- new entries would "
                "be silently dropped (the `cur is None -> continue` defect)")
if not (ROOT / "cockpit" / "rules" / "entry.py").exists():
    fail.append("cockpit/rules/entry.py missing -- candidates and re-entries must become "
                "RuleProposals before they can become numbers")

# (23) Only the action-list renderer may print executable numbers. Everything else may show a
# score, a wait price, or a reason to stay out -- never a share count, an amount or a stop.
_ORDER_PATTERNS = [
    (r"可考虑买[^\n]{0,40}股", "buy share count"),
    (r"示例\s*%?s?\s*股", "example share count"),
    (r"1%风险示例股数", "example-size column"),
    (r"若再入场按[^\n]{0,30}股", "re-entry share count"),
    (r"size_1pct_stop8", "example-size field"),
]
_db_src3 = SRC.get(str(ROOT / "cockpit" / "daily_brief.py"), "")
for _fn in ("_followups_md", "_candidates_md"):
    if ("def %s(" % _fn) not in _db_src3:
        continue
    _body = _db_src3.split("def %s(" % _fn, 1)[1].split("\ndef ", 1)[0]
    for _pat, _what in _ORDER_PATTERNS:
        if re.search(_pat, _body):
            fail.append("%s prints a %s -- executable numbers come only from a Decision, "
                        "rendered by cockpit/render/action_list.py" % (_fn, _what))
for _name in ("daily_brief.py", "scanner.py", "screener.py"):
    _t3 = SRC.get(str(ROOT / "cockpit" / _name), "")
    if "size_1pct_stop8" in _t3:
        fail.append("%s still carries size_1pct_stop8 -- an 'example' order is still an order"
                    % _name)
_al = ROOT / "cockpit" / "render" / "action_list.py"
if _al.exists() and "order_hint" not in _al.read_text(encoding="utf-8"):
    fail.append("action_list.py no longer renders order_hint -- the one place allowed to show "
                "executable numbers stopped showing them")

print("=== Cockpit self-check ===")
for w in warn: print("  WARN:", w)
if fail:
    print("\nHARD FAILURES:")
    for f in fail: print("  FAIL:", f)
    print(f"\n{len(fail)} failure(s), {len(warn)} warning(s)."); sys.exit(1)
print(f"\nPASS (0 hard failures, {len(warn)} warning(s)).")
