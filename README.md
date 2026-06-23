# Cockpit — 个人美股投研自动化（GitHub Actions + 邮件）

云端每个美股交易日发**每日简报**、每两周发**双周复盘**到邮箱——电脑关机/出差也照跑。
策略 = **动量主线 ⊕ Serenity 供应链卡点**；只给提示，**绝不自动下单**。

## 它做什么
- **每日简报**（中国周二–周六 00:00 = 美股盘中），9 段：① 组合快照（IBKR 真实股数/成本/市值/浮盈亏）② 持仓新闻（≤3 天）③ 大盘/宏观 ④ 财报/事件日历 ⑤ 技术位+真实止损位 ⑥ 风控（EWMA 波动率×相关性上限 + $30k 硬顶 + 组合热度）⑦ 操作提示（检查清单）⑧ 待验证 ⑨ 选股雷达/观察池（候选 + 板块轮动）。
- **双周复盘**（中国周六，每两周）：业绩 vs 基准（自跟踪 NAV 历史）、板块轮动、逐票逻辑、风险敞口、反思记忆、下阶段打法。
- **风控**：EWMA(λ0.98,~1年,winsorize) 波动率 × 长窗相关性（同板块 0.60 兜底）动态上限 + 单名 $30k 硬顶 + 不追高 + 真实止损位 + 组合热度(<6-8%) + 稀释代理。
- **反思记忆（BM25）**：种子 6 条；**平仓自动写入**——某持仓离开账户时自动追加一条复盘教训（state/last_positions.json 检测）。
- **逐笔定股**：position_size（Fixed Fractional，账户×风险%÷(入场−止损)），候选给 1% 风险示例股数（与硬顶取 min）。
- **交叉验证**：关键价格对 Yahoo 核，差异大标「待验证」（SEC/10-K 为人工步骤）。
- **fail-open**：任一数据源失败 → 降级+标数据缺口，绝不阻塞/编造。

## 部署
1. 私有仓库，push 本 `cockpit/` 内容。
2. `Settings → Secrets and variables → Actions` 配置（见 `.env.example`）：`FMP_API_KEY`、`ANTHROPIC_API_KEY`、`EMAIL_SENDER/PASSWORD/RECEIVERS`、`IBKR_FLEX_TOKEN`、`IBKR_FLEX_QUERY_ID`。Gmail 用应用专用密码。
3. `Actions` 启用 → 可手动 `Run workflow`（`force_run=true` 非交易日也测）。

## 质量护栏（每次改动前必跑）
- **`python3 selfcheck.py`** —— 机械闸门：编译全模块、检查配置键是否被用、断言 biweekly 与 daily 对齐、扫 TODO。必须 PASS 才算"完成"。
- **`BACKLOG.md`** —— 唯一权威待办册（P0/P1/P2 + 决策）。所有 caveat 登记于此，不靠记忆。
- **完成的硬定义**：selfcheck PASS + 真实邮件输出对照 live IBKR 逐项核 + 配置键全引用 + 文档与代码一致 + 更新 BACKLOG。

## 结构
```
config.yaml            # 配置宪法（持仓/子板块/风控/排期/模型）
selfcheck.py BACKLOG.md
cockpit/
  daily_brief.py biweekly_review.py     # 编排（biweekly 复用 daily 的快照/风控）
  risk.py        # EWMA 波动率 × 相关性(同板块兜底) 上限 + position_size
  screener.py    # 子板块相对强度 + 候选排名 + 不追高
  fmp.py ibkr.py crossval.py            # 数据（fail-open）；ibkr=Flex 解析(去重 Summary/Lot)
  memory.py calendars.py llm.py notify.py
state/reflection_memory.json nav_history.json last_positions.json   # 运行态，工作流自动提交
.github/workflows/     # 两个定时任务
```

## 成本
Claude API 按量 ~$3–15/月（Sonnet 日报 / Opus 复盘）；GHA 免费额度足够；FMP 已付。

## 状态：v1.x，LIVE
核心 LIVE 自动运行。已修：IBKR 持仓去重、$30k 硬顶、组合快照只列持仓、EWMA 风控、选股雷达、真实止损位、组合热度、稀释代理、平仓反思自动写入、双周业绩(NAV)。待办见 `BACKLOG.md`（P2 小项：生命周期阈值校准、avg/max 相关性、邮件 HTML、盘中警报等）。
