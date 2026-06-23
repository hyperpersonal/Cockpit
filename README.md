# Cockpit — 个人美股投研自动化（GitHub Actions + 邮件）

云端每个美股交易日发**每日简报**、每两周发**双周复盘**到你邮箱——电脑关机/出差也照跑。策略=**动量主线 ⊕ Serenity 供应链卡点**；只给提示，**绝不自动下单**。

## 它做什么
- **每日简报**（中国周二–周六 00:00 = 美股盘中）：组合快照、持仓新闻、大盘宏观、技术位/支撑阻力、风控触发、操作提示（含检查清单+待验证）。
- **双周复盘**（中国周六，每两周）：业绩vs基准、主线/板块轮动、逐票逻辑、风险敞口、反思记忆、下阶段打法。
- **风控**：波动率×相关性动态仓位上限（移植 ai-hedge-fund）+ 不追高 + 稀释否决。
- **反思记忆**：BM25 记忆库（移植 TradingAgents）——当前为**种子库 + 检索**（相似情形自动调出教训）；写入接口 `add()/save()` 已就绪，待接入平仓 postmortem 后自动追加教训（v1 暂为人工追加，因目前全为开仓、无已平仓样本）。
- **交叉验证**：关键价格对 Yahoo 核，差异大标「待验证」（SEC/10-K 为深度人工步骤）。

## 部署（约 10 分钟）
1. 在 GitHub 新建私有仓库，把本 `cockpit/` 目录内容 push 上去。
2. `Settings → Secrets and variables → Actions` 配置（见 `.env.example`）：`FMP_API_KEY`、`ANTHROPIC_API_KEY`、`EMAIL_SENDER`、`EMAIL_PASSWORD`、`EMAIL_RECEIVERS`、`SMTP_HOST/PORT`，以及 IBKR（`IBKR_FLEX_TOKEN`+`IBKR_FLEX_QUERY_ID`）。
   - Gmail 需用**应用专用密码**（非登录密码）。
3. **接 IBKR**：在 `cockpit/ibkr.py` 里实现 Flex（或 Client Portal / ib_insync）取持仓+净值——你说"好办"，这里留了 TODO 和接口契约。**未接也能跑**：持仓段会标"数据缺口"，行情/选股/复盘照常。
4. `Actions` 标签 → 启用 → 可先手动 `Run workflow`（`force_run=true` 可在非交易日测试）。
5. 验证收到邮件后即自动按表运行。

## 重要约定（红线，硬编码在 LLM system prompt + 脚本里）
- **绝不下单/转账**，只给提示，你手动执行。
- 非投资建议；未交叉验证的关键数字标「待验证」、不编造。
- **fail-open**：任一数据源失败 → 降级+标数据缺口，绝不阻塞、绝不臆造。
- 盘中简报标"日线未完成"，不当收盘复盘。

## 结构
```
config.yaml            # 配置宪法（持仓/子板块地图/风控/排期/模型）
cockpit/
  daily_brief.py       # 每日简报编排
  biweekly_review.py   # 双周复盘编排
  fmp.py / ibkr.py / crossval.py   # 数据（fail-open）；ibkr 待你接
  risk.py              # 波动率×相关性上限（已验证）
  memory.py            # BM25 反思记忆（已验证）
  screener.py          # 子板块相对强度 + 不追高
  calendars.py         # exchange-calendars 跳节假日 + 盘段
  llm.py / notify.py   # Claude API / SMTP
state/reflection_memory.json   # 反思记忆库（种子已含本期教训）
.github/workflows/     # 两个定时任务
```

## 成本
Claude API 按量 ~$3–15/月（Sonnet 日报 / Opus 复盘）；GHA 免费额度足够；FMP 你已付。

## 状态：v1 脚手架
逻辑均经本项目 dry-run 验证；这是**可运行的 v1**，按真实邮件反馈迭代格式/阈值。`ibkr.py` 的 Flex 解析、SEC/10-K 深度对账、历史回放、**反思记忆的平仓自动写入**为后续增强。本仓库经"独立子智能体评审"过一轮（确认无下单/转账路径、密钥处理正确、风控公式正确；已修复 fail-open 漏洞与占位市值问题）。

## v1.1（2026-06-22 复盘后增强）
- **选股引擎代码化**：子板块相对强度改为对 SPY 的超额 + 板块宽度(成分站上200日比例) + 生命周期(emerging→exhausting，过热不追)；新增 `rank_candidates` 扫描全 universe、排出 top 新候选(排除已持仓，含 no-chase 罚分)。Serenity 14 点 / VCP 收缩仍标 LLM/manual(需基本面+盘中结构)。
- **硬顶基数修正**：单票绝对硬顶 = 12%×总资产 ≈ **$30k**(config `total_assets_usd` × `single_name_hard_cap_pct_of_total`)，与配置宪法一致。
- **每日简报补第④段 财报/事件日历**：拉 FMP 最近一次/下次 earnings(已实测端点)。
- 文档对齐：验收标准旧口径(单票≤15%/主题≤35%)已废弃为 v2 动量口径；宪法波动率档位措辞与 risk.py 对齐。
- 实测(2026-06-22 live)：Layer1 正确把半导体/存储/光模块标 exhausting-过热、ai_software 标落后(应轮出)；ORCL/MSFT 相对 SPY 明显走弱。历史回放(ORCL 6/10 财报→6/11 −18.5%)与事实对账(FMP EPS 2.11/营收19.2B 对 SEC 8-K)均通过。
