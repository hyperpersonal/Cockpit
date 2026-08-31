"""The two domain objects the whole system now agrees on.

Before this module every section of the brief did its own sizing arithmetic, so one
email could tell you to cut SKHY by $35,911 in one place and $40,154 in another, and
carried three different "total to sell" figures (72,858 / 86,442 / 109,543).

The contract now:
  * a RULE may only emit RuleProposal objects. It never renders text and never
    decides anything on its own.
  * the ADJUDICATOR turns all proposals for one ticker into exactly one Decision.
  * a RENDERER may only read Decision fields. It must never recompute an amount.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import hashlib

# --- adjudication tiers, strictest first ------------------------------------------------
# The order is the user's decision (2026-08-28). A tier only ever restricts further.
TIER_ACCOUNT_SAFETY   = 1   # margin / leverage red lines
TIER_THESIS_BROKEN    = 2   # VERIFIED structural veto (de-listing, shell, thesis dead)
TIER_CONCENTRATION    = 3   # theme cap + single-name hard cap
TIER_SIZING           = 4   # risk budget & position ceilings -- ADVISORY (see resolve.py)
TIER_TREND_STOP       = 5   # execution stop breached
TIER_PROFIT_TAKE      = 6
TIER_ENTRY            = 7

TIER_NAMES = {
    TIER_ACCOUNT_SAFETY: "账户安全",
    TIER_THESIS_BROKEN:  "结构性否决（已核实）",
    TIER_CONCENTRATION:  "集中度",
    TIER_SIZING:         "风险预算/仓位上限",
    TIER_TREND_STOP:     "趋势与止损",
    TIER_PROFIT_TAKE:    "止盈",
    TIER_ENTRY:          "新买入",
}

# --- proposal kinds ---------------------------------------------------------------------
HARD_EXIT  = "hard_exit"    # target position = 0, no negotiation
CAP_VALUE  = "cap_value"    # position value may not exceed target_value
TRIM_TO    = "trim_to"      # a partial, non-binding trim suggestion (profit take)
BUY        = "buy"
REVIEW     = "review"       # produces NO number: a candidate for human review
FLAG       = "flag"         # informational only


@dataclass(frozen=True)
class RuleProposal:
    """What a rule is allowed to say. Nothing here is an instruction on its own."""
    rule_id: str
    ticker: str
    kind: str
    reason: str
    tier: int
    target_value: float | None = None      # USD ceiling (CAP_VALUE / TRIM_TO / BUY)
    binding: bool = True                   # False => advisory: never sets the final number
    confidence: str = "verified"           # "verified" | "unverified"
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind in (CAP_VALUE, TRIM_TO, BUY) and self.target_value is None:
            raise ValueError("%s proposal for %s must carry target_value" % (self.kind, self.ticker))
        if self.kind in (REVIEW, FLAG) and self.binding:
            object.__setattr__(self, "binding", False)


@dataclass
class Decision:
    """Exactly one per ticker per run. The single source every renderer reads."""
    decision_id: str
    as_of: str
    ticker: str
    action: str                    # BUY | SELL | HOLD | EXIT
    current_shares: float | None
    target_shares: float | None
    delta_shares: float | None
    target_value: float | None
    binding_rule: str
    supporting_rules: list
    order_hint: str
    valid_until: str
    invalidation_conditions: list
    expected_risk_usd: float | None
    source_confidence: str
    delta_value: float | None = None
    price: float | None = None
    tier: int | None = None
    notes: list = field(default_factory=list)

    @property
    def is_sell(self) -> bool:
        return self.action in ("SELL", "EXIT")

    @property
    def sell_value(self) -> float:
        """The ONE amount. Every section that shows money for this ticker shows this."""
        return abs(self.delta_value or 0.0) if self.is_sell else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_decision_id(as_of: str, ticker: str, action: str, target_value) -> str:
    raw = "%s|%s|%s|%s" % (as_of, ticker, action, round(float(target_value or 0), 2))
    return "%s-%s-%s" % (str(as_of).replace("-", ""), ticker,
                         hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6])
