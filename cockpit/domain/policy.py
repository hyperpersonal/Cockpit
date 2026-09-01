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

entry_decisions_enabled
-----------------------
P0A (2026-09-01): the sell / position-risk / portfolio-adjudication half of the engine is
accepted; the BUY half is not. Candidate scoring feeds it, and the three entry conditions
(Serenity-14, VCP, dilution) are not computed by this system at all -- every production
candidate resolves to WATCH today only because the verification gate happens to catch it.
That is a coincidence, not a control: one config edit or one enriched candidate record would
turn it into an executable order that nobody has validated end to end.

So the switch is explicit and it FAILS CLOSED. A missing key means disabled. Only a literal
true enables buys, and production config.yaml keeps it false until P0B is accepted.
"""
from __future__ import annotations

KEY = "single_name_hard_cap_usd"
LEGACY_TOTAL = "total_assets_usd"
LEGACY_PCT = "single_name_hard_cap_pct_of_total"

ENTRY_FLAG_KEY = "entry_decisions_enabled"
_TRUE = ("true", "yes", "on", "1")

# The exact first-screen wording the user specified for the P0A block (2026-09-01).
P0A_BUY_BLOCK_REASON = "P0A 仅启用持仓卖出/风控裁决；可执行买入等待 P0B 数据与规则验收。"


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


def entry_decisions_enabled(cfg) -> bool:
    """May this run produce a BINDING buy? Fails closed.

    Absent key -> False. Anything that is not literally true -> False. There is deliberately
    no `.get(KEY, True)` anywhere: a default that opens the buy path is a default that opens
    it silently the first time someone hands the engine a config it has not seen.
    """
    v = ((cfg or {}).get("risk") or {}).get(ENTRY_FLAG_KEY)
    if v is True:
        return True
    if isinstance(v, str):
        return v.strip().lower() in _TRUE
    return False


def entry_block_reason(cfg):
    """The reason string when buys are off, or None when they are on."""
    return None if entry_decisions_enabled(cfg) else P0A_BUY_BLOCK_REASON
