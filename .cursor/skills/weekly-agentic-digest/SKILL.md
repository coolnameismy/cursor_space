---
name: weekly-agentic-digest
description: >-
  抓取并整理一周内的 agentic / AI agent 相关文章：理念、产品、研究、协议与业界进展。
  Use when the user asks for a weekly agent digest, 本周/每周 agent 文章, 抓取 agentic
  新闻, Claude/OpenAI/Google/Meta agent 周报, 业界 agent 进展, or /weekly-agentic-digest.
icon: book-open
color: blue
---

# Weekly Agentic Digest

把过去约 7 天（默认上一个完整 ISO 周，或用户指定窗口）里，关于 **AI agent / agentic** 的高信号文章抓齐，写成中文索引 + 简介，落入本仓库。

先读参考，再动手：

1. [`references/sources.md`](references/sources.md) — 检索词、一手源、人物、协议
2. [`references/template.md`](references/template.md) — 周报 Markdown 模板
3. 跑 `python3 scripts/week-window.py`（可选 `--week YYYY-Www` 或 `--days 7`）得到窗口、文件名、已有周报列表

## 触发与窗口

| 用户说法 | 窗口 |
|---|---|
| 「本周」「weekly」「抓一下这周」 | 当前 ISO 周周一 00:00 UTC → 今天（含） |
| 「上周」「上一周」 | 上一个完整 ISO 周（周一–周日） |
| 指定日期 / `2026-W37` | 按指定 ISO 周 |
| 未说明 | **上一个完整 ISO 周**（避免半周噪声） |

输出文件：`docs/agent-weekly/YYYY-Www.md`  
总索引：`docs/agent-weekly/README.md`（没有就按模板建）  
去重账本：`docs/agent-weekly/_seen.json`（URL + 故事指纹）

篇数默认 **10–15**。用户说「20 篇」或「月刊」时再扩到 18–22，文件改放到 `docs/agent-digest-YYYY-MM.md`。

## 工作流（必须按序）

### 1. 定窗口与去重基线

- 运行 `scripts/week-window.py`，记下 `start`、`end`、`iso_week`、`outfile`。
- 读 `docs/agent-weekly/_seen.json`（若存在）和最近 4 期周报标题/链接。
- 同一故事只留 **一篇一手源**；解读稿放「延伸阅读」，不占正选名额。

### 2. 并行检索（至少 8 路）

用 Web Search，查询里必须带 **年份 + 月份或 ISO 周**（如 `August 2026` / `September 2026`），避免旧闻。每路 3–6 条结果即可。

必开检索路（详见 sources.md）：

1. Anthropic / Claude agent、Claude Code、alignment researcher、MHS
2. OpenAI agent、Codex、ChatGPT Work、Agentic Commerce
3. Google / DeepMind / Gemini agent、Antigravity、ADK
4. Microsoft Foundry / Copilot / Agent Framework
5. Meta Hatch / Muse Code / personal superintelligence
6. MCP / A2A / ACP / AAIF 协议
7. arXiv：`multi-agent` / `LLM agent` / `agentic`，限定本周提交
8. 人物：Amodei、Altman、Hassabis、Karpathy、Zuckerberg、LeCun 等 + agent

缺稿时再补：Salesforce Agentforce、AWS Bedrock agents、开源框架（LangGraph、CrewAI）融资/大版本。

### 3. 打开一手页，核日期与相关性

对候选 **Fetch** 官方博客、论文摘要、高管原文。不要只靠搜索摘要写简介。

收录必须同时满足：

- **主题**：agent / agentic 工作流、多 agent、工具调用、computer use、coding agent、协议、对齐研究自动化，而非「又一个聊天模型发版」
- **时间**：发表或重大更新落在窗口内（持续更新文档仅当本周有实质变更）
- **信号**：实验室/云厂商/协议基金会/顶会论文/当事高管 > 二手聚合站
- **可核验**：有稳定 URL；日期写不准就标 `~` 并在简介里说明依据

丢弃：SEO 拼凑、无日期旧文、纯产品教程、与 agent 无关的模型跑分、重复转载。

### 4. 选题配比

尽量覆盖至少 4 类（缺类就在「本周主线」里写明缺口，不要硬凑）：

| 类型 | 目标 |
|---|---|
| 产品发布 / 平台 | 2–4 |
| 理念 / 高管 / 宣言 | 1–2 |
| 研究论文 / 官方研究 | 2–4 |
| 协议 / 标准 / 治理 | 1–2 |
| 高质量行业解读 | 0–2 |

### 5. 写文件

按 [`references/template.md`](references/template.md) 写 `docs/agent-weekly/YYYY-Www.md`：

- 索引表：`# | 日期 | 来源类型 | 机构/人物 | 标题 | 链接`
- 逐篇简介：3–6 句中文，先讲 **是什么 + 为何重要**，再补关键数字/边界
- 「本周主线」：3–6 条，是综合判断，不是第 N 篇的复述
- 「延伸阅读」：落选但有用的链接
- 文末 `*整理日期：YYYY-MM-DD*`

同步：

- 更新 `docs/agent-weekly/README.md` 顶部插入本周一行
- 把本周 URL 写入 `_seen.json`
- 若 `README.md`（仓库根）还没有周报入口，补上指向 `docs/agent-weekly/README.md` 的链接

简介用简体中文。专有名词保留原文（Claude Code、MCP、A2A、Personal AGI）。

### 6. 提交

分支名遵循仓库约定。提交说明示例：`Add agentic weekly digest YYYY-Www`。用户若在 Cloud Agent 流程中，按该流程 push / 开 PR。

## 定时（可选）

用户要「每周自动跑」时：

- 对话内循环：建议配合 `/loop`，间隔 7 天，prompt 写「按 weekly-agentic-digest skill 抓上一完整 ISO 周」
- 产品级调度：建议 `/automate`，cron 用周一 14:00 UTC（`0 14 * * 1`），同一句 prompt

不要在本 skill 里自己 sleep 空转等下周。

## 反模式

- 不要编造未打开过的文章摘要
- 不要把「OpenAPI」和 OpenAI 混为一谈；用户写 OpenAPI 时，默认仍覆盖 OpenAI + 开放协议
- 不要用营销稿语气（「颠覆」「必看」）；边界和失败案例要写
- 不要把窗口外爆款硬塞进正选（可放延伸阅读并标注日期）
