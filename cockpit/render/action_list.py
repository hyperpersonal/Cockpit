"""The action list. Reads Decisions and nothing else.

Format is the user's specification (2026-08-28). First screen carries only:
    account timestamp / NAV / cash / leverage / portfolio risk
    whether buying is allowed today
    THE final sell total
    the trade decisions, in priority order

and every suggestion carries: ticker, direction, current -> target shares, shares to trade,
target position value, binding rule, core reason, valid until, invalidation conditions,
data timestamp.

Hard constraint enforced by construction: this module performs no sizing arithmetic. Every
number it prints comes off a Decision field. `total_sell_value()` is the only total there is.
"""
from __future__ import annotations

from ..engine.resolve import total_sell_value

_DIR = {"SELL": "卖出", "EXIT": "清仓", "BUY": "买入", "HOLD": "持有"}


def _money(x):
    v = int(round(x or 0))
    return ("-$" + format(-v, ",")) if v < 0 else ("$" + format(v, ","))


def _shares(x):
    if x is None:
        return "-"
    return ("%.4f" % float(x)).rstrip("0").rstrip(".")


def header(as_of, price_note, net_liq, cash, leverage_pct, risk_usd, risk_pct,
           buying_allowed, buying_reason, sell_total, staleness=None):
    gate = "✅ 是" if buying_allowed else "⛔ 否"
    rows = [
        "## ✅ 今日行动清单（规则直出 · 提示非指令 · 手动执行）",
        "",
        "| | |",
        "|---|---|",
        "| ⏱️ 账户数据时间 | **%s**（IBKR Flex 统计期末日）%s |" % (as_of, price_note or "")]
    for w in (staleness or []):
        rows.append("| ⚠️ 数据新鲜度 | **%s** |" % w)
    return "\n".join(rows + [
        "| 净值 / 现金 | %s / %s |" % (_money(net_liq), _money(cash)),
        "| 杠杆产品占净值 | %.1f%% |" % (leverage_pct or 0),
        "| 组合在险 | %s（%.1f%% 净值，预算 6-8%%，**警示层，不产生卖出金额**） |" % (
            _money(risk_usd), risk_pct or 0),
        "| 今天是否允许买入 | %s —— %s |" % (gate, buying_reason),
        "| **最终卖出总额** | **%s** |" % _money(sell_total),
        "",
    ])


def decision_block(i, d, reason):
    lines = [
        "**%d. %s · %s**" % (i, d.ticker, _DIR.get(d.action, d.action)),
        "",
        "| 字段 | 值 |",
        "|---|---|",
        "| 当前股数 → 目标股数 | %s → %s |" % (_shares(d.current_shares), _shares(d.target_shares)),
        "| 需要买卖的股数 | **%s%s 股** |" % (
            "-" if (d.delta_shares or 0) < 0 else "+", _shares(abs(d.delta_shares or 0))),
        "| 目标仓位金额 | %s |" % _money(d.target_value),
        "| 本次金额 | **%s** |" % _money(abs(d.delta_value or 0)),
        "| 绑定规则 | `%s` |" % d.binding_rule,
        "| 核心理由 | %s |" % reason,
        "| 目标仓位在险$ | %s |" % ((_money(d.expected_risk_usd))
                                    if d.expected_risk_usd is not None else "-"),
        "| 有效期 | %s |" % d.valid_until,
        "| 失效条件 | %s |" % "；".join(d.invalidation_conditions or ["-"]),
        "| 数据时间 | %s |" % d.as_of,
        "| 下单提示 | %s |" % d.order_hint,
    ]
    if d.source_confidence != "verified":
        lines.append("| ⚠️ 依据强度 | **%s** |" % d.source_confidence)
    if d.supporting_rules:
        lines.append("| 参考口径（不产生金额） | %s |" % ", ".join("`%s`" % r for r in d.supporting_rules))
    return "\n".join(lines) + "\n"


def render(decisions, reasons, as_of, net_liq, cash, leverage_pct, risk_usd, risk_pct,
           buying_allowed, buying_reason, price_note="", watch_notes=None, staleness=None):
    """decisions: list[Decision]; reasons: {rule_id: text} for the binding rule of each.

    `staleness`: explicit warnings about how old the data is. A conclusion drawn from
    yesterday's positions must never be presented as a live one (user constraint, 2026-08-28)."""
    total = total_sell_value(decisions)
    out = [header(as_of, price_note, net_liq, cash, leverage_pct, risk_usd, risk_pct,
                  buying_allowed, buying_reason, total, staleness=staleness)]

    acts = [d for d in decisions if d.action != "HOLD"]
    if acts:
        out.append("### 交易决策（按优先级，理由越硬越靠前）\n")
        for i, d in enumerate(sorted(acts, key=lambda x: (x.tier or 99, -x.sell_value)), 1):
            out.append(decision_block(i, d, reasons.get(d.binding_rule, "-")))
        out.append("> 每只标的**全篇只有一个金额**；下方任何附录只解释算法，**不产生第二个答案**。")
        out.append("> 系统不下单、不改单、不撤单；以上为提示，由你手动执行。\n")
    else:
        out.append("**今天没有需要执行的交易。** 所有绑定上限均已满足。\n")

    holds = [d for d in decisions if d.action == "HOLD" and d.notes]
    if holds:
        out.append("### 观察项（有标记但**不产生指令**）\n")
        out.append("| 票 | 标记 |")
        out.append("|---|---|")
        for d in sorted(holds, key=lambda x: x.ticker):
            out.append("| %s | %s |" % (d.ticker, "<br>".join(d.notes)))
        out.append("")
    for n in (watch_notes or []):
        out.append("> " + n)
    return "\n".join(out) + "\n\n---\n\n"
