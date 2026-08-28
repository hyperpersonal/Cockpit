# -*- coding: utf-8 -*-
"""B50 一次性回填：从 signal_history 补出 B44 上线前发生的离场，写进 state/reentry_watch.json。

为什么需要：B44 的台账 2026-08-22 才上线，而 MSFT/SPCX/TSM 分别在 07-30 / 08-20 / 08-14 就离场了。
不回填的话 B50「离场后跟踪」会空转好几个月，等于新功能上线即静默——正是 workflow-discipline Rule 6
要防的那种「该出现的东西从来没出现过」。

口径与诚实边界：
  · exit_price 用的是**该票在 signal_history 里最后一次被观察到的价格**，不是真实成交价。
    日报是快照，离场当天的成交价拿不到，所以这几行的「离场后 ±%」有系统性误差。
  · 因此回填的条目标 kind="backfill"，与真实捕获的 full/partial 区分开，不要混着读。
  · 只回填「出现过又消失、且现在确实不在持仓里」的票。

一次性脚本，跑完即可；不接入日报管线。
"""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
CUR = set(json.load(open(ROOT/"state"/"last_positions.json", encoding="utf-8"))["positions"])
days = json.load(open(ROOT/"state"/"signal_history.json", encoding="utf-8"))["days"]

last = {}
for d in days:
    for t, v in (d.get("holdings") or {}).items():
        if v.get("price"):
            last[t] = {"date": d["date"], "price": v["price"], "shares": v.get("shares")}

p = ROOT/"state"/"reentry_watch.json"
watch = json.load(open(p, encoding="utf-8")).get("watch", {}) if p.exists() else {}
added = []
for t, v in sorted(last.items()):
    if t in CUR or t in watch:
        continue
    watch[t] = {"exit_date": v["date"], "exit_price": v["price"], "kind": "backfill",
                "prompted": "n/a", "note": "seeded from signal_history; exit_price = last observed, not fill"}
    added.append("%s @%.2f (%s, %.4f股)" % (t, v["price"], v["date"], v["shares"] or 0))
json.dump({"watch": watch}, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("回填 %d 笔：%s" % (len(added), " ｜ ".join(added) if added else "无"))
