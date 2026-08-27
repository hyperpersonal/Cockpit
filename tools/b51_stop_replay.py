# -*- coding: utf-8 -*-
"""B51 止损规则历史回放（手动分析工具，**不接入日报管线**）。

目的是体检，不是调参 —— 刻意不做参数搜索、不做优化，只回答一个问题：
「20 日低 × 0.99 这条止损规则，放在用户自己的持仓上，过去一年会发生什么。」

用法：
  1) 先把 13 只持仓的日线收盘价拉成 /tmp/b51_prices.json
     格式 {"TICKER": [{"date":"YYYY-MM-DD","price":123.45}, ...], ...}（顺序无所谓，脚本会排序）
     来源：FMP historical-price-eod-light，from_date 取一年前
  2) python3 tools/b51_stop_replay.py

三种策略对照：
  A 无止损        —— 买入并持有到窗口结束
  B 止损不再入    —— 首次跌破即离场，之后空仓
  C 止损+再入场   —— 跌破离场，收盘重新站上 50 日线则再买入（B44 的规则）

两种止损口径对照：
  挂单口径  stop = min(前 20 根收盘) × 0.99   ← GTC 单实际会坐在的位置
  生产口径  stop = min(含当日 20 根)  × 0.99   ← daily_brief 的 hist[:20]，B58 修复前用于判破位

已知边界（结论必须带着这些读）：
  · 只用收盘价，不含盘中最低点 → **低估**真实洗出次数
  · 不含滑价、佣金、跳空
  · 窗口约一年，且这 13 只是「现在还持有的票」，有幸存者偏差
  · SKHY/RAM 上市不足一年，CCXI 合并前是流动性极差的 SPAC 壳，三者样本不可靠
"""
import json, statistics as st
D=json.load(open('/tmp/b51_prices.json'))

def series(t):
    rows=sorted(D[t], key=lambda r:r['date'])          # 转成时间正序
    return [r['date'] for r in rows], [float(r['price']) for r in rows]

def maxdd(px):
    peak=px[0]; worst=0.0
    for p in px:
        peak=max(peak,p); worst=min(worst,p/peak-1)
    return worst*100

def run(t):
    dates,px=series(t); n=len(px)
    if n<120: return None
    W=20; start=50                                     # 50 根热身（再入场要 50 日线）
    # ---- 事件统计：假设一直持有，每次跌破就记一次（连续跌破算一次）----
    events=[]; inbreach=False
    for i in range(start,n):
        stop=min(px[i-W:i])*0.99                       # 挂单口径：不含当日
        prod=min(px[i-W+1:i+1])*0.99                   # 生产代码口径：hist[:20] 含当日
        hit=px[i]<=stop
        if hit and not inbreach:
            f20 = px[i+20]/px[i]-1 if i+20<n else None
            fmin= min(px[i+1:i+21])/px[i]-1 if i+1<n else None
            events.append(dict(d=dates[i],px=px[i],stop=stop,f20=f20,fmin=fmin,
                               prod_would_fire = px[i]<=prod))
        inbreach=hit
    # ---- 三种策略期末收益 ----
    entry=px[start]
    A = px[-1]/entry-1                                  # 无止损
    # B：止损即出，不再入
    B=None; bexit=None
    for i in range(start,n):
        if px[i] <= min(px[i-W:i])*0.99:
            B=px[i]/entry-1; bexit=dates[i]; break
    if B is None: B=A
    # C：止损 + 站上 50 日线再入场
    cash=1.0; pos=entry; holding=True; trips=0
    for i in range(start+1,n):
        ma50=sum(px[i-50:i])/50
        if holding and px[i] <= min(px[i-W:i])*0.99:
            cash*= px[i]/pos; holding=False; trips+=1
        elif not holding and px[i] > ma50:
            pos=px[i]; holding=True
    C = (cash*(px[-1]/pos)-1) if holding else cash-1
    return dict(t=t,n=n,A=A*100,B=B*100,C=C*100,bexit=bexit,trips=trips,
                dd=maxdd(px[start:]),ev=events)

R=[r for r in (run(t) for t in sorted(D)) if r]
print("样本：%d 只票，窗口 2025-08-01 → 2026-08-27（每只热身 50 根后开始）\n"%len(R))
print("%-6s %5s %8s %8s %8s %6s %9s"%("票","事件","无止损%","止损不再入%","止损+再入%","往返","持有期最大回撤%"))
for r in sorted(R,key=lambda r:-len(r['ev'])):
    print("%-6s %5d %8.1f %8.1f %8.1f %6d %9.1f"%(r['t'],len(r['ev']),r['A'],r['B'],r['C'],r['trips'],r['dd']))
allev=[e for r in R for e in r['ev']]
print("\n=== 核心问题：止损后会不会「刚卖就反弹」？（%d 次触发）==="%len(allev))
f20=[e['f20'] for e in allev if e['f20'] is not None]
fmin=[e['fmin'] for e in allev if e['fmin'] is not None]
up=[x for x in f20 if x>0]
print("  卖出后 20 日：上涨 %d 次 / 下跌 %d 次（卖飞率 %.0f%%）"%(len(up),len(f20)-len(up),100*len(up)/len(f20)))
print("  中位数 %+.1f%% ｜ 平均 %+.1f%% ｜ 最好 %+.1f%% ｜ 最差 %+.1f%%"%(
    100*st.median(f20),100*sum(f20)/len(f20),100*max(f20),100*min(f20)))
print("  卖出后 20 日内曾再跌 >10%% 的次数：%d / %d（%.0f%%）—— 这些是止损救了你的"%(
    sum(1 for x in fmin if x<-0.10),len(fmin),100*sum(1 for x in fmin if x<-0.10)/len(fmin)))
print("  卖出后 20 日内最深回撤中位数 %+.1f%%"%(100*st.median(fmin)))
pf=sum(1 for e in allev if e['prod_would_fire'])
print("\n=== 生产代码口径对照 ===")
print("  真实挂单口径触发 %d 次；生产代码 hist[:20]（含当日）口径只会触发 %d 次"%(len(allev),pf))
