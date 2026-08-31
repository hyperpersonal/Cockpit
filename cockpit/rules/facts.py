"""Verification status of every external-world fact the system is allowed to act on.

Constraint (user, 2026-08-28): any external fact that can drive a buy/sell, a theme
classification, or a liquidation ordering must carry a SOURCE, a VERIFICATION DATE and an
EXPIRY STATUS. Unverified or expired facts may raise a warning; they may never be the sole
trigger for an exit.

Why this exists. config.holdings[].role once said "CCXI = Churchill Capital XI, a SPAC shell,
outside the framework". Nobody had ever checked it. It drove risk.theme_overrides ->
outside_framework -> the disposal ordering, and the 2026-08-24 brief put a real de-SPAC
(Agility Robotics) at the top of a liquidation list. The annotation was not wrong because it
was old; it was wrong because it was never verified and nothing in the system could tell.

Stamp format, enforced by selfcheck gate 9:
    ...（核实 2026-08-25，源=FMP profile + SEC S-4）
or an explicit 「未核实」.
"""
from __future__ import annotations
import datetime as dt
import re

# The canonical stamp is 「核实 YYYY-MM-DD，源=…」. Two legacy spellings are in the live config
# and are just as verified, so the parser accepts them rather than declaring real work
# unverified: 「核实于 YYYY-MM-DD，源=…」 (AAOI) and 「核实 YYYY-MM-DD：<source text>」 (SKHY).
# Getting this wrong is not a harmless strictness: an over-tight parser would have downgraded
# SKHY -- 31% of NAV -- to "unverified" and told the user two real checks had never happened.
STAMP = re.compile(r"核实(?:于)?\s*[:：]?\s*(\d{4}-\d{2}-\d{2})")
SOURCE = re.compile(r"源\s*=\s*([^）)，,；;]+)")
SOURCE_AFTER_DATE = re.compile(r"^\s*[:：]\s*(\S[^；;]{3,})")
EXPLICIT_UNVERIFIED = "未核实"

VERIFIED = "verified"
UNVERIFIED = "unverified"
EXPIRED = "expired"

DEFAULT_EXPIRY_DAYS = 180


def expiry_days(cfg):
    return int(((cfg or {}).get("risk") or {}).get("fact_expiry_days", DEFAULT_EXPIRY_DAYS))


def assess(annotation, cfg=None, today=None):
    """Return {status, verified_on, source, age_days, reason}.

    status is one of verified / unverified / expired. Only `verified` may back a hard exit.
    """
    text = str(annotation or "")
    today = today or dt.date.today()
    if isinstance(today, str):
        try:
            today = dt.date.fromisoformat(today[:10])
        except Exception:
            today = dt.date.today()

    m = STAMP.search(text)
    if not m:
        return {"status": UNVERIFIED, "verified_on": None, "source": None, "age_days": None,
                "reason": ("注释明写「未核实」" if EXPLICIT_UNVERIFIED in text
                           else "注释缺「核实 YYYY-MM-DD，源=…」戳")}
    try:
        d = dt.date.fromisoformat(m.group(1))
    except Exception:
        return {"status": UNVERIFIED, "verified_on": m.group(1), "source": None,
                "age_days": None, "reason": "核实日期无法解析"}

    src = SOURCE.search(text)
    if src:
        source = src.group(1).strip()
    else:                                   # legacy form: 「核实 YYYY-MM-DD：<source text>」
        tail = SOURCE_AFTER_DATE.match(text[m.end():])
        source = tail.group(1).strip()[:80] if tail else None
    age = (today - d).days
    limit = expiry_days(cfg)
    if not source:
        return {"status": UNVERIFIED, "verified_on": d.isoformat(), "source": None,
                "age_days": age, "reason": "有核实日期但没写来源——无法复查即等于未核实"}
    if age > limit:
        return {"status": EXPIRED, "verified_on": d.isoformat(), "source": source,
                "age_days": age,
                "reason": "核实于 %s，已过 %d 天（上限 %d 天）——需重新核实" % (
                    d.isoformat(), age, limit)}
    return {"status": VERIFIED, "verified_on": d.isoformat(), "source": source,
            "age_days": age, "reason": "核实 %s，源=%s（%d 天前）" % (d.isoformat(), source, age)}


def may_force_exit(assessment):
    """Only a currently-verified fact may drive a position to zero on its own."""
    return (assessment or {}).get("status") == VERIFIED


def audit(cfg, today=None):
    """Every holdings[].role, assessed. Used by the brief and by selfcheck."""
    out = {}
    for h in (cfg.get("holdings") or []):
        if isinstance(h, dict) and h.get("ticker"):
            out[h["ticker"]] = assess(h.get("role"), cfg, today)
    return out
