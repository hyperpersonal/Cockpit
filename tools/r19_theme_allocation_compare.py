"""R19: compare three ways of allocating a theme-concentration cut.

DECIDED 2026-08-28: scheme (a) -- leveraged members first, then ordinary members pro-rata --
is now the live rule in cockpit/rules/concentration.py. RS is observation only.

This tool is kept as the record of WHY, and as a way to re-run the comparison if the question
is ever reopened. Scheme (c) here reproduces the rule that was replaced; it is not live.

    python3 tools/r19_theme_allocation_compare.py
"""
from __future__ import annotations
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import yaml                                                  # noqa: E402
import harness                                               # noqa: E402
from cockpit.domain.policy import hard_cap_usd               # noqa: E402
from cockpit.rules.concentration import theme_map, leverage_of  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
BOOK = harness.BOOK
POS, NAV, CASH = BOOK["positions"], BOOK["net_liq"], BOOK["cash"]
THR = float(CFG["risk"]["theme_exposure_alert_pct"])


def after_hard_cap():
    cap = hard_cap_usd(CFG)
    return {t: min(p["market_value"], cap) for t, p in POS.items()}


def exposures(targets):
    lay = theme_map(list(POS), CFG)
    e = {}
    for t, v in targets.items():
        e[lay[t]] = e.get(lay[t], 0.0) + v * leverage_of(t, CFG)
    return e


def scheme(name, allocate):
    base = after_hard_cap()
    lay = theme_map(list(POS), CFG)
    tgt = dict(base)
    for theme, usd in sorted(exposures(base).items()):
        limit = NAV * THR / 100.0
        if theme == "unmapped" or usd <= limit + 1:
            continue
        members = [t for t in base if lay[t] == theme]
        allocate(tgt, members, usd - limit)
    return {"name": name, "targets": tgt,
            "sells": {t: round(POS[t]["market_value"] - tgt[t], 2)
                      for t in tgt if POS[t]["market_value"] - tgt[t] > 1},
            "expo": exposures(tgt)}


def alloc_leveraged_first(tgt, members, need):
    """(a) take it out of the leveraged product first; spread any remainder pro-rata."""
    lev = sorted([t for t in members if leverage_of(t, CFG) > 1],
                 key=lambda t: -tgt[t] * leverage_of(t, CFG))
    for t in lev:
        if need <= 1:
            break
        f = leverage_of(t, CFG)
        take = min(tgt[t] * f, need)
        tgt[t] = round((tgt[t] * f - take) / f, 2)
        need -= take
    rest = [t for t in members if leverage_of(t, CFG) == 1 and tgt[t] > 0]
    total = sum(tgt[t] for t in rest)
    if need > 1 and total > 0:
        for t in rest:
            tgt[t] = round(tgt[t] - need * (tgt[t] / total), 2)


def alloc_pro_rata(tgt, members, need):
    """(b) shrink every member by the same proportion of leverage-adjusted exposure."""
    total = sum(tgt[t] * leverage_of(t, CFG) for t in members)
    if total <= 0:
        return
    scale = (total - need) / total
    for t in members:
        tgt[t] = round(tgt[t] * scale, 2)


def alloc_weakest_rs_first(tgt, members, need):
    """(c) the rule as currently written: cut the weakest RS in the layer first."""
    order = sorted(members, key=lambda t: (POS[t].get("rs") if POS[t].get("rs") is not None
                                           else 9999, t))
    for t in order:
        if need <= 1:
            break
        f = leverage_of(t, CFG)
        take = min(tgt[t] * f, need)
        tgt[t] = round((tgt[t] * f - take) / f, 2)
        need -= take


def main():
    rs = {t: POS[t].get("rs") for t in POS}
    schemes = [scheme("(a) 杠杆优先【现行】", alloc_leveraged_first),
               scheme("(b) 全员同比例缩减", alloc_pro_rata),
               scheme("(c) 最弱 RS 优先【已废弃】", alloc_weakest_rs_first)]

    hb = [t for t in POS if theme_map(list(POS), CFG)[t] == "memory_hbm"]
    print("=" * 92)
    print("R19 · 主题超限分配方案比较 —— 2026-08-27 账面，memory_hbm 62.0%% 净值 > %.0f%% 上限" % THR)
    print("硬顶 $%s 先生效（SKHY/MU 各降到该值），再分配剩余超额。RS 仅作展示。" %
          format(int(hard_cap_usd(CFG)), ","))
    print("=" * 92)
    print("%-6s %8s %10s %12s | %s" % ("票", "RS", "杠杆", "现仓$",
                                        "  ".join("%-22s" % s["name"] for s in schemes)))
    for t in sorted(hb, key=lambda x: -POS[x]["market_value"]):
        cells = []
        for s in schemes:
            cells.append("目标%9s 卖%8s" % (
                format(int(s["targets"][t]), ","),
                format(int(s["sells"].get(t, 0)), ",") if s["sells"].get(t) else "—"))
        print("%-6s %8s %10s %12s | %s" % (
            t, rs[t], "%.0fx" % leverage_of(t, CFG),
            format(int(POS[t]["market_value"]), ","), "  ".join(cells)))
    print("-" * 92)
    print("%-39s | %s" % ("卖出总额（含硬顶部分）",
          "  ".join("%-22s" % ("$" + format(int(sum(s["sells"].values())), ",")) for s in schemes)))
    print("%-39s | %s" % ("执行后 memory_hbm 占净值",
          "  ".join("%-22s" % ("%.1f%%" % (s["expo"]["memory_hbm"] / NAV * 100)) for s in schemes)))
    print("%-39s | %s" % ("执行后现金",
          "  ".join("%-22s" % ("$" + format(int(CASH + sum(s["sells"].values())), ",")) for s in schemes)))
    print("%-39s | %s" % ("执行后杠杆产品占净值",
          "  ".join("%-22s" % ("%.2f%%" % (s["targets"]["RAM"] / NAV * 100)) for s in schemes)))
    print("%-39s | %s" % ("动到几只票",
          "  ".join("%-22s" % str(len(s["sells"])) for s in schemes)))

    print("\n" + "=" * 92)
    print("分歧点：把 RAM 换成本层最强（把 RS 改成 +99）会怎样")
    print("=" * 92)
    saved = POS["RAM"]["rs"]
    POS["RAM"]["rs"] = 99.0
    try:
        alt = [scheme("(a) 杠杆优先", alloc_leveraged_first),
               scheme("(b) 同比例", alloc_pro_rata),
               scheme("(c) 最弱RS优先", alloc_weakest_rs_first)]
        for s in alt:
            print("%-18s 卖出 %s" % (s["name"], ", ".join(
                "%s $%s" % (t, format(int(v), ",")) for t, v in sorted(s["sells"].items()))))
    finally:
        POS["RAM"]["rs"] = saved


if __name__ == "__main__":
    main()
