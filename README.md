# Cockpit — 个人美股投研自动化（GitHub Actions + 邮件）

云端每个美股交易日发**每日简报**、每两周发**双周复盘**到邮箱——电脑关机/出差也照跑。
策略 = **动量主线 ⊕ Serenity 供应链卡点**；只给提示，**绝不自动下单**。

## 它做什么
- **每日简报**（中国周二–周六 00:23 = 美股盘中）——例外报告制（B42），结构：
  ① **今日行动清单**置顶（市场位置分 → ⛔纪律违规点名 → 先卖/减$金额 → 热度闸门 → 等回调候选带三档等待价，每条附规则名）
  ② **三盏灯**（组合热度 / 现金·保证金 / 财报窗口≤14天）
  ③ **例外区**（仅列超限/破位/距止损<5%/财报临近/稀释的票，健康票折叠一行）
  ④ LLM 附录（重大消息≤3 + 异常点评 + 待验证≤5）
  ⑤ 紧凑快照 + 选股雷达/观察池（代码直出，不经 LLM）。
- **双周复盘**（中国周六 10:23，每两周）：业绩 vs 基准（自跟踪 NAV）、板块轮动、逐票逻辑、风险敞口、反思记忆、下阶段打法 + **依从性记分板**（B29：每个信号 执行/无视 × 无视的代价$，正负如实）。
- **持仓名单 IBKR 驱动**（B33）：买卖任何股票次日自动进快照/风控/行动清单，不用改 config（config.holdings 只是角色注释+掉线兜底）。
- **风控**：EWMA(λ0.98,~1年,winsorize) 波动率 × 长窗相关性（同板块 0.60 兜底；eff_corr=max(avg,0.85×max)；peer 扩到同板块成分股）动态上限 + 单名 $30k 硬顶 + 不追高 + 真实止损位 + 组合热度(<6-8%) + 稀释代理。风控表市值与快照同用 FMP 现价（B24）。
- **行为留痕**：平仓/开仓自动写反思记忆（B5/B34）；**亏损摊平自动点名**（B37 利弗莫尔铁律）；每日信号写 state/signal_history.json 喂记分板。
- **逐笔定股**：position_size（Fixed Fractional，账户×风险%÷(入场−止损)），候选给 1% 风险示例股数（与硬顶取 min）。
- **交叉验证**：关键价格对 Yahoo 核；**SEC EDGAR 深核**（自动）——真实流通股趋势(稀释，拆股不误报)、近180天 S-3/424B5/FWP 增发类备案、最新 10-K/Q/8-K，差异/稀释标「待验证」。
- **fail-open**：任一数据源失败 → 降级+标数据缺口，绝不阻塞/编造。
- ~~盘中警报~~：2026-07-19 退役（用户时区下信息时效≈0；夜间保护由 IBKR GTC 止损单承担）。代码/workflow 保留，可一键复用。

## 部署
1. 私有仓库，push 本 `cockpit/` 内容。
2. `Settings → Secrets and variables → Actions` 配置（见 `.env.example`）：`FMP_API_KEY`、`ANTHROPIC_API_KEY`、`EMAIL_SENDER/PASSWORD/RECEIVERS`、`IBKR_FLEX_TOKEN`、`IBKR_FLEX_QUERY_ID`。Gmail 用应用专用密码。
3. `Actions` 启用 → 可手动 `Run workflow`（`force_run=true` 非交易日也测）。

## 质量护栏（每次改动前必跑）
- **`python3 selfcheck.py`** —— 机械闸门 7 项：①全模块编译 ②行为配置键被引用 ③biweekly 与 daily 对齐 ④扫 TODO ⑤死配置键 ⑥BACKLOG 卫生（同行不得既 OPEN 又 DONE） ⑦完成指纹（标 DONE 的项必须在代码里找到指纹）。必须 PASS 才算"完成"。
- **`BACKLOG.md`** —— 唯一权威待办册（P0/P1/P2 + 决策）。所有 caveat 登记于此，不靠记忆。
- **完成的硬定义**：selfcheck PASS + 真实邮件输出对照 live IBKR 逐项核 + 配置键全引用 + 文档与代码一致 + 更新 BACKLOG。

## 结构
```
config.yaml            # 配置宪法（持仓注释/子板块/风控/排期/模型）
selfcheck.py BACKLOG.md
cockpit/
  daily_brief.py biweekly_review.py     # 编排（biweekly 复用 daily 的快照/风控；B42 例外报告制）
  risk.py        # EWMA 波动率 × 相关性(同板块兜底/eff_corr) 上限 + position_size
  screener.py    # 子板块相对强度 + 候选排名 + 等待价(B38) + market_position(B39) + 不追高
  fmp.py ibkr.py crossval.py            # 数据（fail-open）；fmp 含 batch→单票 fallback(B31)；ibkr=Flex 解析(去重)
  memory.py calendars.py llm.py notify.py
  intraday_alert.py                     # 已退役(2026-07-19)，保留可复用
state/  reflection_memory.json nav_history.json last_positions.json signal_history.json alert_state.json   # 运行态，工作流自动提交
.github/workflows/     # daily-brief / biweekly-review（均带 timeout-minutes 护栏 B43）+ intraday-alert(Disabled)
```

## 成本
Claude API 按量 ~$3–15/月（Sonnet 日报 / Opus 复盘）；GHA 免费额度足够；FMP Starter 年付（2026-07-18 从 Ultimate 降级，省 $960/年）。

## 状态：v1.x，LIVE
核心 LIVE 自动运行（2026-06-23 起；07-20→08-21 连续无断档，唯一一次 GHA 平台取消已加超时护栏 B43）。决策层（行动清单/等待价/市场位置）与度量层（记分板/开仓留痕/摊平点名）均已实弹验证。待办见 `BACKLOG.md`（开发大件仅剩 B28+B41 全市场扫描器；小项 B36 主题敞口阈值、B39v2 CAPE、B18 HTML 长期停车；决策项 D2 RAM 杠杆ETF 处置）。
