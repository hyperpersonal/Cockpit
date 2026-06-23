# Cockpit BACKLOG — 唯一权威待办登记册
> 规则：任何 caveat / 待验证 / 待完成，一律登记在此（不再只活在对话里）。宣布"完成/正确"前必须：
> ① 跑 `python3 selfcheck.py`（须 PASS）；② 把真实输出（邮件）对照 **live IBKR 连接器** 逐项核对；
> ③ 确认 config 键全被引用、文档与代码一致。最后更新本表状态。

更新：2026-06-23（P0 B1/B2/B3 已修，selfcheck PASS）

## P0 — 会按时发错 / 数据错误（最高优先）
| id | 项 | 详情 | 状态 |
|----|----|------|------|
| B1 | ~~biweekly_review.py 旧版~~ | 6/27 触发；缺 holdings_snapshot/IBKR真实盈亏、未用 position_caps 升级风控、无选股雷达。selfcheck 对此硬失败。 | ✅ DONE 2026-06-23 |
| B2 | ~~portfolio_heat_pct 算错+标错~~ | 我写成 Σ(超上限$)/净值；position-sizer 的"组合热度"=Σ(各仓到止损在险)/账户。还误给 <6-8% 阈值。已改为真实热度=Σ(在险到止损)/净值。 | ✅ DONE 2026-06-23 |
| B3 | ~~stop_review_level 对落后股无意义~~ | =max(200DMA, 成本×0.8)，落后股会高于现价→"止损"在现价之上、trivially 破位。已改：止损取现价下方最高者；全破→already_broken_down。 | ✅ DONE 2026-06-23 |

## P1 — 正确性 / 功能缺口
| id | 项 | 状态 |
|----|----|------|
| B4 | biweekly **完全没算 TWR/业绩**（仅一句 note）；接 IBKR get_pa_performance 或用 NAV 历史算。 | OPEN |
| B5 | 反思记忆**平仓自动写入**未接（memory.py docstring 还谎称会自动增长）。 | OPEN | ✅ DONE 2026-06-23 — 平仓离场→自动追加反思教训(daily_brief) |
| B6 | **新日报（risk/fmp/daily_brief）需对 live IBKR 再核对**后才算定稿。 | OPEN |
| B7 | **死配置键**：vol_window_days/corr_window_days(且与1年EWMA矛盾)/news_max_age_days/dilution_atm_disqualifier/skip_us_holidays — 删除或接通。 | OPEN | ✅ DONE 2026-06-23 — 删 vol/corr_window_days 死键, 加 hist_window_days, 接通 news_max_age/dilution 旗标 |
| B8 | **README 过时**（0 提及 EWMA/选股雷达/position_size/9段）——重写。 | OPEN | ✅ DONE 2026-06-23 — README 重写 |
| B9 | 配置宪法 §3 持仓快照旧股数 + §5 风控描述非EWMA/兜底——标"以 IBKR 为准"并更新。 | OPEN | ✅ DONE 2026-06-23 — 宪法 §3 标'以IBKR为准' §5 写EWMA/兜底 |

## P2 — 暂缓 / 小项
| id | 项 |
|----|----|
| B10 | 生命周期阈值偏激进（raw vs200，半导体几乎全标过热）→改"乖离+距高点"。 |
| B11 | 风控乘数用 avg_corr 非 max_corr（L4 偏好 pairwise）。 |
| B12 | calendars 盘段 UTC 边界冬令时差 1 小时。 |
| B13 | llm.py 未设 temperature=0（简报非确定性，影响可复现）。 |
| B14 | 盘中事件警报（宪法 §7"后续"）从未建。 |
| B15 | headroom token 压缩未集成（现用粗暴 [:90000] 截断）。 |
| B16 | 并排对比原版 serenity（验收 §D-5）未跑。 |
| B17 | SEC EDGAR 深度交叉验证（现仅 Yahoo 价格；其余人工/13F）。 |
| B18 | 邮件 HTML 美化（用户已说稳定后再做）。 |
| B19 | 风控相关性对等集仅 holdings+4 半导体（peer 窄）。 |

## 待你拍板
| D1 | 嘉信 QQQ 建仓节奏（一次性 / 定投 / 等回调）——始终未决。 |

## 已修（留痕）
IBKR 持仓翻倍(Flex Summary+Lot)、P&L 口径不一致、组合快照列满universe、$30k 硬顶基数、notify 空串崩溃、风控 EWMA+1年相关性+同板块兜底、position_size、财报日历、选股雷达、稀释代理。
