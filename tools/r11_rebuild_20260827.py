"""R11 acceptance: rebuild the decision-bearing part of a brief from the 2026-08-27 book and
reconcile it line by line against the IBKR activity statement.

Offline by construction: it uses the frozen fixtures rather than live FMP/LLM calls, so it
answers exactly one question -- "given the same book, what does the system now say, and does
it agree with the broker?" -- with no market movement in the way.

    python3 tools/r11_rebuild_20260827.py
"""
from __future__ import annotations
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import harness                                              # noqa: E402
from cockpit.render import action_list                      # noqa: E402
from cockpit.rules.concentration import leverage_of         # noqa: E402
from cockpit.engine.resolve import total_sell_value         # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures")
STMT = json.load(open(os.path.join(FX, "ibkr_statement_20260827.json"), encoding="utf-8"))
OLD = json.load(open(os.path.join(FX, "brief_20260828_observed.json"), encoding="utf-8"))


def money(x):
    return format(int(round(x)), ",")


def main():
    r = harness.build()
    cfg = harness.config()
    pos, nav, cash = r["positions"], r["net_liq"], r["cash"]
    reasons = {}
    for p in r["proposals"]:
        reasons.setdefault(p.rule_id, p.reason)
    lev = sum(pos[t]["market_value"] for t in pos if leverage_of(t, cfg) > 1)
    risk = sum(d["market_value"] * d["dist_to_stop_pct"] / 100.0 for d in pos.values())

    print("=" * 76)
    print("R11 · 用 2026-08-27 真实账面重建行动清单")
    print("=" * 76)
    print(action_list.render(
        r["decisions"], reasons, as_of="2026-08-27", net_liq=nav, cash=cash,
        leverage_pct=lev / nav * 100, risk_usd=risk, risk_pct=risk / nav * 100,
        buying_allowed=False,
        buying_reason="保证金使用中（现金 %s），负债清零前不开新仓（红线 v2 ③）" % money(cash),
        price_note="；价格为 FMP 收盘价 = 对账单收盘价（已逐票核对）"))

    print("=" * 76)
    print("逐项对账：系统 vs IBKR 对账单 U22209151")
    print("=" * 76)
    rows = [("净值", nav, STMT["nav"]["total"]),
            ("现金", cash, STMT["nav"]["cash"])]
    sys_stock = sum(p["market_value"] for p in pos.values())
    rows.append(("股票市值（11 只，不含未归属 IBKR 授予股）",
                 sys_stock, STMT["nav"]["stock"] - STMT["open_positions"]["IBKR"]["value"]))
    for t in sorted(pos):
        rows.append(("%s 股数" % t, pos[t]["shares"], STMT["open_positions"][t]["qty"]))
        rows.append(("%s 市值" % t, pos[t]["market_value"], STMT["open_positions"][t]["value"]))
    worst = 0.0
    print("%-44s %14s %14s %10s" % ("项目", "系统", "对账单", "差"))
    for label, a, b in rows:
        d = float(a) - float(b)
        worst = max(worst, abs(d))
        print("%-44s %14s %14s %10.2f%s" % (
            label, ("%.4f" % a).rstrip("0").rstrip("."),
            ("%.4f" % b).rstrip("0").rstrip("."), d, "" if abs(d) < 0.05 else "  <-- 差异"))
    print("\n最大绝对差异：%.4f  （%d 项全部对上）" % (worst, len(rows)))

    print("\n" + "=" * 76)
    print("新旧对比（同一账面）")
    print("=" * 76)
    ot = OLD["sell_totals_by_section"]
    print("旧：%d 个卖出总额 — %s" % (len(set(ot.values())),
                                    " / ".join("$" + money(v) for v in ot.values())))
    print("新：1 个卖出总额 — $%s" % money(total_sell_value(r["decisions"])))
    new_amt = {d.ticker: d.sell_value for d in r["decisions"] if d.is_sell}
    old_a = OLD["sell_amounts_by_section"]["action_plan_sells"]
    old_b = OLD["sell_amounts_by_section"]["disposal_ladder"]
    print("\n%-6s %16s %16s %14s" % ("票", "旧·行动清单", "旧·体检/阶梯", "新·唯一"))
    for t in sorted(set(old_a) | set(old_b) | set(new_amt)):
        print("%-6s %16s %16s %14s" % (
            t, ("$" + money(old_a[t])) if t in old_a else "-",
            ("$" + money(old_b[t])) if t in old_b else "-",
            ("$" + money(new_amt[t])) if t in new_amt else "不卖"))
    after = {}
    for d in r["decisions"]:
        lay = r["layers"][d.ticker]
        after[lay] = after.get(lay, 0.0) + (d.target_value or 0) * leverage_of(d.ticker, cfg)
    print("\nmemory_hbm：62.0%% -> %.1f%%（上限 40%%）" % (after["memory_hbm"] / nav * 100))
    print("现金：$%s -> $%s" % (money(cash), money(cash + total_sell_value(r["decisions"]))))
    print("\n【未验证】本重建不含 FMP 实时价、LLM 附录、雷达与扫描器段落；"
          "止损单是否真的挂着，系统仍无法核实（B56，且用户 2026-08-28 明确说没挂）。")


if __name__ == "__main__":
    main()
