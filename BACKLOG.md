# Cockpit BACKLOG — 唯一权威待办登记册
> 规则：任何 caveat / 待验证 / 待完成，一律登记在此（不再只活在对话里）。宣布"完成/正确"前必须：
> ① 跑 `python3 selfcheck.py`（须 PASS）；② 把真实输出（邮件）对照 **live IBKR 连接器** 逐项核对；
> ③ 确认 config 键全被引用、文档与代码一致。最后更新本表状态。

更新：2026-06-24（B13/B11/B12 已实现，selfcheck PASS；B15/B16 标 won't-do 移除）

## P0 — 会按时发错 / 数据错误（最高优先）
| id | 项 | 详情 | 状态 |
|----|----|------|------|
| B1 | ~~biweekly_review.py 旧版~~ | 6/27 触发；缺 holdings_snapshot/IBKR真实盈亏、未用 position_caps 升级风控、无选股雷达。selfcheck 对此硬失败。 | ✅ DONE 2026-06-23 |
| B2 | ~~portfolio_heat_pct 算错+标错~~ | 我写成 Σ(超上限$)/净值；position-sizer 的"组合热度"=Σ(各仓到止损在险)/账户。还误给 <6-8% 阈值。已改为真实热度=Σ(在险到止损)/净值。 | ✅ DONE 2026-06-23 |
| B3 | ~~stop_review_level 对落后股无意义~~ | =max(200DMA, 成本×0.8)，落后股会高于现价→"止损"在现价之上、trivially 破位。已改：止损取现价下方最高者；全破→already_broken_down。 | ✅ DONE 2026-06-23 |

## P1 — 正确性 / 功能缺口
| id | 项 | 状态 |
|----|----|------|
| B4 | ~~biweekly 没算业绩~~ ✅ DONE 2026-06-24 — _performance() 用 NAV 历史算区间收益 vs SPY + alpha（近似净值收益，非入金调整 TWR）。 |
| B5 | 反思记忆**平仓自动写入**未接（memory.py docstring 还谎称会自动增长）。 | ✅ DONE 2026-06-23 — 平仓离场→自动追加反思教训(daily_brief) |
| B6 | ~~新日报对 live IBKR 再核对~~ ✅ DONE 2026-06-23 — 见下方「B6 最终核对结果」，去重/止损/热度/风控/稀释/平仓逐项对 live 验证通过。 |
| B7 | **死配置键**：vol_window_days/corr_window_days(且与1年EWMA矛盾)/news_max_age_days/dilution_atm_disqualifier/skip_us_holidays — 删除或接通。 | ✅ DONE 2026-06-23 — 删 vol/corr_window_days 死键, 加 hist_window_days, 接通 news_max_age/dilution 旗标 |
| B8 | **README 过时**（0 提及 EWMA/选股雷达/position_size/9段）——重写。 | ✅ DONE 2026-06-23 — README 重写 |
| B9 | 配置宪法 §3 持仓快照旧股数 + §5 风控描述非EWMA/兜底——标"以 IBKR 为准"并更新。 | ✅ DONE 2026-06-23 — 宪法 §3 标'以IBKR为准' §5 写EWMA/兜底 |

## P2 — 暂缓 / 小项
| id | 项 |
|----|----|
| B10 | ~~生命周期阈值偏激进~~ ✅ DONE 2026-06-23 — 改用乖离(距50日)+距高点双轴；过热=大幅超50日线且贴近高点(抛物线)；实测杀跌日不再误标过热，trending/correcting 有区分。 |
| B11 | ~~风控乘数用 avg_corr 非 max_corr~~ ✅ DONE 2026-06-24 — eff_corr=max(avg_corr, 0.85×max_corr)，单一高相关 peer 也会收紧上限；输出新增 eff_corr 字段。 |
| B12 | ~~calendars 盘段 UTC 边界冬令时差 1 小时~~ ✅ DONE 2026-06-24 — 盘段开/收边界改由 exchange_calendars 取当日真实 UTC session（DST+半日自适应）；实测夏 20:00/冬 21:00 UTC 收盘正确。 |
| B13 | ~~llm.py 未设 temperature~~ ✅ DONE 2026-06-24 — llm.run 增 temperature 参数，默认 0.3（可复现且不过度死板）。 |
| B14 | ~~盘中事件警报~~ ✅ DONE 2026-06-24 — cockpit/intraday_alert.py：破位/日内±6%异动/当日新闻触发，每30min美股时段(GHA)，仅触发才发邮件，(票·条件·日)去重，代码直出无LLM；新增 intraday-alert.yml + config.alerts。 |
| B15 | ~~headroom token 压缩~~ ❌ WON'T-DO 2026-06-24（用户决定移除）——[:90000]/[:95000] 截断够用，候选表已代码直出不靠 token 预算。 |
| B16 | ~~并排对比原版 serenity~~ ❌ WON'T-DO 2026-06-24（用户决定移除）——系统已远超原版样板，无需回头对比。 |
| B17 | ~~SEC EDGAR 深度交叉验证~~ ✅ DONE 2026-06-24 — crossval.edgar_dossier：真实流通股YoY(拆股不误报)+近180天增发类备案(S-3/424B5/FWP,以流通股为准防发债误报)+最新关键备案；接入daily_brief风控/待验证段。UA走EDGAR_USER_AGENT/EMAIL_SENDER。 |
| B18 | 邮件 HTML 美化（用户已说稳定后再做）。 |
| B19 | ~~风控相关性对等集仅 holdings+4 半导体~~ ✅ DONE 2026-06-24 — universe 扩成 holdings+所属子板块全部成分股(只取持仓涉及板块,fan-out 可控)；position_caps 双桶: avg_corr 只对账面持仓、max_corr 扩到同板块成分股,单只拥挤板块票也吃集中度折价；输出加 n_book_peers/n_theme_peers。 |

## 待你拍板
| D1 | 嘉信 QQQ 建仓节奏（一次性 / 定投 / 等回调）——始终未决。 |

## 已修（留痕）
IBKR 持仓翻倍(Flex Summary+Lot)、P&L 口径不一致、组合快照列满universe、$30k 硬顶基数、notify 空串崩溃、风控 EWMA+1年相关性+同板块兜底、position_size、财报日历、选股雷达、稀释代理。

## B6 最终核对结果（2026-06-23，对 live IBKR 逐项）
代码全部正确：去重(NVDA50.42/MSFT24.53 与 live 完全一致)、止损位(NVDA$190.09/MSFT$326.18/META$499.20、ORCL already_broken_down)、组合热度1.9%(真实公式)、风控(ORCL超vol上限$5,951→TRIM)、稀释false、平仓检测(今日无)、IBKR-vs-FMP价差已标待验证、9段+检查清单+L1教训。✅ batch-1/2 修复在实盘简报全部生效。
但发现两个**数据时效**问题（非代码 bug）：
| B20 | ~~MV/盈亏用 Flex 旧价~~ ✅ DONE — 市值/盈亏改用 股数×FMP当前价(NVDA→+0.9%)；ibkr_mv 保留为参考。 |
| B21 | **Flex "Last Business Day" 滞后约1天**：简报显示 ORCL 94.78/META 5.34，但 live 已是 ORCL 112.90/META 10.67（你在简报跑完后又买入）。考虑 Flex 周期改 "Today" 或简报明确标注"持仓截至上一交易日收盘"。 | 🟡 缓解 — 已加 as-of 标注+提示改 Flex 周期 Today；股数即时性受 Flex EOD 限制(GHA 无实时) |

## B22 选股雷达不渲染 → 已根治(2026-06-23)
候选/板块表改由**代码直出并强制拼到邮件末尾**（不再交给 LLM，杜绝被 token 预算挤掉）。日报+双周都已接。 ✅ DONE
