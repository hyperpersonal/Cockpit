# Cockpit BACKLOG — 唯一权威待办登记册
> 规则：任何 caveat / 待验证 / 待完成，一律登记在此（不再只活在对话里）。宣布"完成/正确"前必须：
> ① 跑 `python3 selfcheck.py`（须 PASS）；② 把真实输出（邮件）对照 **live IBKR 连接器** 逐项核对；
> ③ 确认 config 键全被引用、文档与代码一致。最后更新本表状态。

更新：2026-07-11（07-10/11 邮件验收：B11/B12/B19/B17/B20/B21/B22 实盘生效；新增 B23–B29）

## P0 — 会按时发错 / 数据错误（最高优先）
| id | 项 | 详情 | 状态 |
|----|----|------|------|
| B1 | ~~biweekly_review.py 旧版~~ | 6/27 触发；缺 holdings_snapshot/IBKR真实盈亏、未用 position_caps 升级风控、无选股雷达。selfcheck 对此硬失败。 | ✅ DONE 2026-06-23 |
| B2 | ~~portfolio_heat_pct 算错+标错~~ | 我写成 Σ(超上限$)/净值；position-sizer 的"组合热度"=Σ(各仓到止损在险)/账户。还误给 <6-8% 阈值。已改为真实热度=Σ(在险到止损)/净值。 | ✅ DONE 2026-06-23 |
| B3 | ~~stop_review_level 对落后股无意义~~ | =max(200DMA, 成本×0.8)，落后股会高于现价→"止损"在现价之上、trivially 破位。已改：止损取现价下方最高者；全破→already_broken_down。 | ✅ DONE 2026-06-23 |
| B23 | **llm.py temperature 被新模型拒绝→双周复盘 LLM 段丢失**：2026-07-11 双周邮件报 400 `temperature is deprecated for this model`（opus-4-8），fail-open 只发出了雷达表。已改 llm.py：先带 temperature 调用，被拒即去掉 temperature 自动重试一次（B13 对 sonnet 日报仍生效）。 | 🔴 OPEN — 代码已交付，待手动重跑 biweekly(force_run) 出真实邮件核对后再转完成 |
| B25 | **config.holdings 与 live IBKR 严重漂移（结构性盲区）**：2026-07-11 live 共 15 个持仓、净值 $158.6k、现金仅 $126，而 config 只登记 NVDA/ORCL/MSFT/META(已平)。快照/风控表/热度/新闻/财报/EDGAR/盘中警报的宇宙全部取自 config → 约 $117k(≈74%) 持仓完全不被跟踪；MU $36.5k、SKHY $40.4k 均已超 $30k 硬顶但系统无感知；组合热度 2.0% 严重低估；雷达把已持有的 MU/MRVL 标为"未持有"候选；警报仍盯已平仓的 META。修法待拍板：①持仓改由 IBKR Flex 驱动（config 降级为角色注释） ②仅加 Flex↔config 漂移报警（selfcheck+日报顶部） ③手动更新 config（最快但会再漂移）。 | 🔴 OPEN — 已拍板混合方案(2026-07-11)：config.holdings 已同步为 live 14 票(删META) + daily 顶部新增代码直出漂移警报；待重跑邮件核对后收尾 |
| B27 | **daily 的 state 回写从 6/29 起每天静默失败**：GitHub 上 nav_history/last_positions 冻结在 06-27，而 alert_state 已是 07-10——daily-brief.yml 的 push 没有 rebase 重试（`git push || echo skipped`），被盘中警报的频繁 push 挤掉(non-fast-forward)后静默吞掉；且 workflow 双向漂移（GitHub=整点cron 0 16+permissions.write；本地=错峰cron 23 16+无permissions）。后果：NAV 史断档 9 个交易日(双周业绩失真)、META 每天被重复判"新平仓"、反思记忆写入全部丢失。已交付合并版 daily-brief.yml（错峰 cron+permissions.write+rebase 重试，与 intraday 同款）。 | 🔴 OPEN — 已交付，待下次 daily 跑完查 GitHub state 有无更新 |

## P1 — 正确性 / 功能缺口
| id | 项 | 状态 |
|----|----|------|
| B4 | ~~biweekly 没算业绩~~ ✅ DONE 2026-06-24 — _performance() 用 NAV 历史算区间收益 vs SPY + alpha（近似净值收益，非入金调整 TWR）。 |
| B5 | 反思记忆**平仓自动写入**未接（memory.py docstring 还谎称会自动增长）。 | ✅ DONE 2026-06-23 — 平仓离场→自动追加反思教训(daily_brief) |
| B6 | ~~新日报对 live IBKR 再核对~~ ✅ DONE 2026-06-23 — 见下方「B6 最终核对结果」，去重/止损/热度/风控/稀释/平仓逐项对 live 验证通过。 |
| B7 | **死配置键**：vol_window_days/corr_window_days(且与1年EWMA矛盾)/news_max_age_days/dilution_atm_disqualifier/skip_us_holidays — 删除或接通。 | ✅ DONE 2026-06-23 — 删 vol/corr_window_days 死键, 加 hist_window_days, 接通 news_max_age/dilution 旗标 |
| B8 | **README 过时**（0 提及 EWMA/选股雷达/position_size/9段）——重写。 | ✅ DONE 2026-06-23 — README 重写 |
| B9 | 配置宪法 §3 持仓快照旧股数 + §5 风控描述非EWMA/兜底——标"以 IBKR 为准"并更新。 | ✅ DONE 2026-06-23 — 宪法 §3 标'以IBKR为准' §5 写EWMA/兜底 |
| B24 | 风控表 current_usd 用 Flex 前一日市值，与组合快照(FMP 现价)同一封邮件口径不一致：07-10 邮件 ORCL 快照 $17,861(@141.47) vs 风控表 $18,208(=前日收价 144.22)，TRIM 金额按旧价算。修法：daily/biweekly 给 position_caps 的 cur_mv 改为 股数×FMP现价（同 B20 口径）。 | 🔴 OPEN |
| B28 | **全市场新主线扫描器（地图外发现，用户 2026-07-11 拍板要做）**：雷达现在只扫 config 手画的 8 个子板块，地图外的下一个 SNDK 不可见。规格：宇宙=美股普通股 mcap≥$2B、价≥$10、日均成交额≥$50M；信号=距 52 周新高 ≤5% + RS vs SPY(63/126日) 前十分位；按 industry 聚类，同业 ≥3 只贴近新高 = 疑似新主线簇；输出为雷达新增小节「地图外新主线候选」（代码直出，沿 B22 教训不经 LLM），每票标 posture（ext描述仍适用：发现≠追高）；同一簇持续 ≥5 交易日 → 提示把该 industry 加进 config.subthemes。成本：1 次 FMP screener + 批量 quote，挂在 daily_brief 内。 | 🔴 OPEN — 排期在 B23/B25/B27 重跑验证收尾之后，与 B29 定先后 |
| B29 | **依从性记分板（我建议的下一开发项，与 B28 先后待用户确认）**：biweekly 自动统计过去两周每个系统信号（TRIM/破位/等回调/超限/漂移）× 用户实际动作（对照 IBKR 持仓变动）× 事后盈亏，直接回答「听系统 vs 不听系统各赚亏多少」。依赖：B27 state 持久化修复生效（last_positions/nav 连续）。这是回答「系统值不值得继续」的唯一数据化途径。 | 🔴 OPEN |

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
| B26 | 盘中警报单次运行内不去重：07-10 警报邮件同一条 ROSEN/MSFT 新闻标题出现两次（同标题两条源，(票·条件·日)去重只防跨次运行）。修法：build_alerts 返回前按 (ticker,cond_key) 去重。 |
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

## 07-10/11 邮件验收纪要（2026-07-11，对 live IBKR 连接器逐项）
本轮验收：daily brief 2026-07-10、盘中警报 2026-07-10、biweekly review 2026-07-11 三封真实邮件。
- 股数/成本与 live 完全一致（NVDA 50.4177/198.34、ORCL 126.2544/198.01、MSFT 32.8368/395.89），去重正常。
- B11/B19 生效：风控表出现 有效相关性(eff_corr) 与 同板块同伴数；NVDA n_theme_peers=7、ORCL/MSFT=2，与 config 子板块成分数学一致。
- B12 生效：18:04 UTC(=14:04 ET) 判 intraday，标注"盘中快照"正确（夏令时口径）。
- B20/B21 生效：快照盈亏用 FMP 现价（NVDA +6.3% 对应 210.745）；顶部有"持仓数据截至 20260709"标注。
- B17 生效：EDGAR 段有真实流通股 YoY（NVDA -0.8% 回购/ORCL +2.6%/MSFT -0.1%）、拆股否、备案清单供人工核。
- B22 生效：雷达表在 daily 与 biweekly 都由代码直出；biweekly LLM 失败时雷达表照发（fail-open 按设计工作）。
- 组合热度 2.0% 公式复算一致（(210.745-191.53)×50.4177+(383.615-316.72)×32.8368)/158,889≈2.0%；ORCL 无有效止损已如实标注低估风险。
- 平仓检测生效：META 已从 live 消失，日报正确标"已平仓"并引用反思记忆。
- 发现并登记：B23（temperature 400 杀掉双周 LLM 段）、B24（风控表用前日市值）、B25（config 持仓漂移，15 vs 4，两票超硬顶无感知）、B26（警报单次运行内重复标题）。
- 本轮未检查：GitHub↔本地除 config.yaml/llm.py 外未逐文件比对（llm.py 已抓 raw 确认一致）；SKHY/CCXI/SPCX 标的身份未验证（待验证）；biweekly 业绩段(NAV vs SPY)因 LLM 失败未能验收，待重跑后补。
- 补充(同日稍后)：又发现 B27（daily state 回写断链，nav 冻结 06-27）；workflow 三份中 intraday 本地=GitHub 一致，biweekly 本地=GitHub 一致，daily 双向漂移已出合并版。SKHY/CCXI/SPCX 身份仍待验证。nav 断档 06-30~07-10 无法自动回填（可选：用 IBKR get_pa_performance 手工补）。
