"""Single-source resolvers for policy numbers that more than one module needs.

Rule: a policy number has exactly ONE definition. If two places compute it, they will
eventually compute it differently -- that is the same failure that produced three sell
totals from one book on 2026-08-28, only one level further down.

single_name_hard_cap_usd
------------------------
User decision 2026-08-28: the single-name ceiling is a FIXED $30,000, defined as an absolute
limit derived from total-wealth risk tolerance. It is NOT a percentage of anything. The old
derivation (account.total_assets_usd x risk.single_name_hard_cap_pct_of_total) meant the
ceiling silently moved whenever the Schwab side -- which this system does not manage -- was
re-estimated. Those two keys are kept for ONE migration cycle as a fallback and emit a
deprecation notice; they no longer decide the number when the authoritative key is present.
"""
from __future__ import annotations

KEY = "single_name_hard_cap_usd"
LEGACY_TOTAL = "total_assets_usd"
LEGACY_PCT = "single_name_hard_cap_pct_of_total"


def hard_cap_usd(cfg) -> float:
    """THE single-name ceiling, in dollars. Every caller reads this and nothing else."""
    rk = (cfg or {}).get("risk") or {}
    v = rk.get(KEY)
    if v is not None:
        return float(v)
    total = float(((cfg or {}).get("account") or {}).get(LEGACY_TOTAL, 250000))
    pct = float(rk.get(LEGACY_PCT, 12))
    return total * pct / 100.0


def deprecation_notices(cfg) -> list:
    """Rendered in the brief so a stale config says so out loud instead of quietly steering."""
    out = []
    rk = (cfg or {}).get("risk") or {}
    acct = (cfg or {}).get("account") or {}
    if rk.get(KEY) is None:
        out.append("⚠️ config 缺 `risk.%s`，正回退到 `account.%s × risk.%s` 计算单票硬顶。"
                   "该回退只保留一个迁移周期——硬顶应是固定美元值，不随另一个账户的估值漂移。"
                   % (KEY, LEGACY_TOTAL, LEGACY_PCT))
    else:
        legacy = None
        if acct.get(LEGACY_TOTAL) is not None and rk.get(LEGACY_PCT) is not None:
            legacy = float(acct[LEGACY_TOTAL]) * float(rk[LEGACY_PCT]) / 100.0
        if legacy is not None and abs(legacy - hard_cap_usd(cfg)) > 1:
            out.append("ℹ️ `account.%s`/`risk.%s` 仍在 config 中（旧口径会得出 $%s），"
                       "但已**不参与**硬顶决定；当前硬顶 = `risk.%s` = $%s。下个迁移周期后可删。"
                       % (LEGACY_TOTAL, LEGACY_PCT, format(int(legacy), ","), KEY,
                          format(int(hard_cap_usd(cfg)), ",")))
    return out
