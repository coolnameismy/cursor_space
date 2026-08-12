# Codex `.codex` 目录：文件、作用与实现机制

本文记录 OpenAI Codex 本地主目录（默认 `~/.codex`，可由 `CODEX_HOME` 覆盖）与项目级 `.codex/` 中的常见文件、各自职责，以及关键实现机制。内容基于官方文档与 `openai/codex` 仓库公开实现说明，实际机器上不一定全部出现。

> 区分两套路径：
>
> - **用户级** `$CODEX_HOME`（默认 `~/.codex`）：配置、凭证、会话、SQLite 状态、记忆产物等
> - **项目级** `<repo>/.codex/`：仅项目覆盖（如 `config.toml`、hooks、rules）；需项目被 trust 才会加载

---

## 1. 总览

```text
~/.codex/                          # CODEX_HOME（用户级）
├── config.toml                    # 全局配置
├── <profile>.config.toml          # 可选配置 profile
├── auth.json                      # 文件型凭证（也可能用系统钥匙串）
├── AGENTS.md                      # 全局自定义指令
├── AGENTS.override.md             # 全局指令临时覆盖
├── history.jsonl                  # 本地历史（可关闭）
├── sessions/YYYY/MM/DD/           # 会话 transcript（rollout-*.jsonl）
├── state_5.sqlite                 # 线程/通用运行时状态
├── memories_1.sqlite              # 记忆流水线状态
├── logs_2.sqlite                  # 结构化日志状态
├── goals_1.sqlite                 # goals 状态
├── memories/                      # 记忆文件系统产物
├── log/ 或 logs/                  # 运行日志
├── skills/                        # 用户级 skills（也可能在 ~/.agents/skills）
├── rules/                         # 用户级 rules
└── packages/                      # 独立包 / release 缓存等

<repo>/.codex/                     # 项目级（trusted 时生效）
├── config.toml
├── hooks/ …
└── rules/ …
```

环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | 用户级状态根目录；若手动设置，目录必须已存在 |
| `CODEX_SQLITE_HOME` | 同 `CODEX_HOME` | SQLite 状态存放位置；配置项 `sqlite_home` 优先 |

---

## 2. 用户级文件与作用

### 2.1 配置与凭证

| 路径 | 作用 |
|---|---|
| `config.toml` | 主配置：模型、features、memories、MCP、sandbox、历史持久化等 |
| `<name>.config.toml` | 配置 profile；通过 `--profile <name>` 选用 |
| `auth.json` | 文件型登录凭证；也可改用 OS keychain / Credential Manager |
| `AGENTS.md` | 全局 agent 指令，会话启动时注入 instruction chain |
| `AGENTS.override.md` | 同级覆盖：存在且非空时优先于 `AGENTS.md` |

**配置加载要点**

- 用户级配置始终可读
- 项目级 `<repo>/.codex/config.toml` 仅在项目 trusted 时加载
- Codex 从仓库根走到当前工作目录，合并沿途所有项目 `.codex/config.toml`；离 CWD 更近的覆盖更远的
- 项目配置**不能**覆盖敏感项（会被忽略并告警），例如：`openai_base_url`、`chatgpt_base_url`、`model_provider`、`model_providers`、`notify`、`profile`、`profiles`、`otel` 等

### 2.2 会话与历史

| 路径 | 作用 |
|---|---|
| `history.jsonl` | 本地命令/会话历史记录（可用配置关闭） |
| `sessions/YYYY/MM/DD/rollout-*.jsonl` | 会话 transcript；用于 resume、调试，也是记忆 Phase 1 的原始输入 |

**实现要点**

- Rollout 是按会话持久化的执行日志（response items 等）
- 记忆系统不会立刻总结刚结束的会话，而是等会话足够 idle，再从 eligible rollouts 中抽取

### 2.3 SQLite 运行时状态

| 路径 | 作用 |
|---|---|
| `state_5.sqlite` | 线程元数据、通用可恢复运行时状态、job 协调相关数据 |
| `memories_1.sqlite` | 记忆专用：stage-1 输出、Phase 1/2 job 租约、usage、watermark 等 |
| `logs_2.sqlite` | 结构化日志相关状态 |
| `goals_1.sqlite` | goals 相关状态 |

**为何拆库**

- 降低锁竞争（会话状态 vs 记忆流水线 vs 日志）
- 记忆表可独立清理/重建，不必与 durable thread records 绑死同一 schema

`StateRuntime` 负责初始化这些 DB、跑 migrator，并提供 memories / jobs / logs 等访问入口。

### 2.4 记忆产物目录 `memories/`

| 路径 | 作用 |
|---|---|
| `raw_memories.md` | Phase 2 同步的合并 raw memories（按 thread-id 稳定升序） |
| `rollout_summaries/` | 每个选中 rollout 的摘要文件 |
| `memory_summary.md` / `MEMORY.md` | 巩固后的高层记忆，供读路径注入 |
| `phase2_workspace_diff.md` | 相对上次成功 Phase 2 baseline 的 git-style diff，供 consolidation agent 使用 |
| `.git/` | memories 工作区 baseline（由 `codex-git-utils` 维护；成功后会重置） |
| `extensions/`（如 chronicle） | 扩展资源；过期文件会被 prune，并体现在 workspace diff 中 |
| `skills/`（若存在） | consolidation 产物之一，通常由 Phase 2 agent 维护 |

官方建议：把这些文件当作**生成态状态**排查用，不要当作主要手改入口。

### 2.5 日志、skills、缓存

| 路径 | 作用 |
|---|---|
| `log/` 或 `logs/` | 运行日志；配置 `log_dir` 时还可能生成明文 `codex-tui.log` |
| `skills/` | 用户级 skills（skills 还会从 `~/.agents/skills`、仓库 `.agents/skills`、系统路径等发现） |
| `rules/` | 用户级 rules |
| `packages/` | standalone package / release 缓存等元数据 |

---

## 3. 项目级 `.codex/`（仓库内）

| 路径 | 作用 |
|---|---|
| `.codex/config.toml` | 项目覆盖配置（trusted 才加载） |
| `.codex/hooks/` 等 | 项目 hooks |
| `.codex/rules/` 等 | 项目 rules |

相对路径（例如 `model_instructions_file`）相对于包含该 `config.toml` 的 `.codex/` 目录解析。

未信任项目时：忽略项目 `.codex/` 层（含 config、hooks、rules）；用户级与系统级仍生效。

---

## 4. 指令链（AGENTS.md）实现机制

会话启动时构建 instruction chain（每个 run / TUI session 一次）：

1. **全局**：`$CODEX_HOME` 下优先 `AGENTS.override.md`，否则 `AGENTS.md`
2. **项目**：从仓库根走到 CWD；每一层最多取一个文件（先 override，再 `AGENTS.md`，再 fallback 名）
3. **合并**：自上而下拼接；越靠近 CWD 的内容越靠后，语义上可覆盖更上层
4. **限制**：空文件跳过；总大小受 `project_doc_max_bytes`（默认约 32KiB）约束

注意：`/resume` 恢复旧会话时，可能仍沿用会话创建时的指令上下文，而不是立刻重读磁盘上的最新 `AGENTS.md`。

---

## 5. 记忆系统实现机制

记忆默认关闭。开启方式示例：

```toml
[features]
memories = true

[memories]
generate_memories = true   # 新会话可否作为抽取输入
use_memories = true        # 新会话是否注入已有记忆
```

相关 crate / 模块：

| 组件 | 职责 |
|---|---|
| `codex-memories-write` | Phase 1/2 提示词、产物同步、workspace diff、扩展清理 |
| `codex-memories-read` | 注入 developer instructions、citation 解析、read-usage 遥测 |
| `codex-core` 的 `memories/` | 启动触发、租约心跳、consolidation 子 agent 编排 |
| `StateRuntime` + `memories_1.sqlite` | job claim、stage-1 存储、usage / watermark |

### 5.1 何时运行

根会话启动时后台异步执行，条件包括：

- 非 ephemeral
- memories feature 开启
- 非 sub-agent
- state DB 可用

然后按顺序跑：**Phase 1 → Phase 2**。

还会受 idle 时间、扫描上限、rate-limit 剩余百分比等约束；会话级可用 `/memories` 控制「是否用记忆 / 是否写入记忆」。

### 5.2 Phase 1：按会话抽取（Rollout Extraction）

目标：把 eligible rollout 变成 DB 中的 stage-1 记录。

```text
sessions/*.jsonl (rollouts)
        │
        ▼
  claim jobs (SQLite lease)
        │
        ▼
  filter memory-relevant items
        │
        ▼
  model extract (并行，有并发上限)
        │
        ├─ raw_memory
        ├─ rollout_summary
        └─ rollout_slug?
        │
        ▼
  redact secrets → memories_1.sqlite (stage-1)
```

Eligible 大致要求：允许的交互来源、年龄窗口内、空闲足够久、未被其他 worker 占用、落在 startup claim 限额内。

Job 结果：`succeeded` / `succeeded_no_output` / `failed`（失败带 retry backoff）。

### 5.3 Phase 2：全局合并（Global Consolidation）

目标：把 top-N stage-1 同步到磁盘，再用 consolidation 子 agent 更新高层记忆。

```text
stage-1 rows (usage 排序 / 过期过滤)
        │
        ▼
  global lock (仅一个 Phase 2)
        │
        ▼
  sync raw_memories.md + rollout_summaries/
  prune stale summaries / extension resources
        │
        ▼
  git diff vs last baseline → phase2_workspace_diff.md
        │
        ├─ clean? → 直接标记成功
        └─ dirty? → spawn consolidation agent
                      (无审批 / 无网络 / 仅本地写 / 禁 collab)
                      │
                      ▼
                 更新 MEMORY.md / memory_summary.md 等
                      │
                      ▼
                 重置 memories/.git baseline
                 更新 selected_for_phase2 + watermark
```

选择策略摘要：

- 忽略超出 `max_unused_days` 且长期未用的记忆
- 无 `last_usage` 时回退 `generated_at`
- 排序：`usage_count` 优先，再比最近 `last_usage` / `generated_at`

脏检查以 **git workspace dirty** 为准；DB watermark 主要用于记账，避免完成水位回退，不单独决定是否要跑 agent。

### 5.4 读路径：如何注入新会话

当 `memories.use_memories = true` 时，新会话构建初始上下文会走读路径：读取巩固后的记忆（如 `memory_summary.md`），渲染 `read_path` 模板，注入 developer instructions（常见为 `## Memories` 段）。

已知设计张力：写/合并路径会尽量按 cwd/项目区分，但读路径历史上可能整文件注入全局 summary，存在跨项目串味风险。

### 5.5 两阶段拆分原因

- **Phase 1**：横向扩展，规范化「每会话一条」记忆记录
- **Phase 2**：串行加锁，保证共享磁盘记忆一致、可差分更新

---

## 6. 常用配置键（记忆相关）

| 键 | 作用 |
|---|---|
| `features.memories` | 总开关 |
| `memories.generate_memories` | 是否允许新会话进入生成输入 |
| `memories.use_memories` | 是否向未来会话注入记忆 |
| `memories.disable_on_external_context` | 使用 MCP / web search 等外部上下文的会话是否排除出生成 |
| `memories.min_rate_limit_remaining_percent` | 配额不足时跳过生成 |
| `memories.extract_model` | Phase 1 抽取模型覆盖 |
| `memories.consolidation_model` | Phase 2 合并模型覆盖 |

---

## 7. 排查速查

| 现象 | 优先检查 |
|---|---|
| 新会话没有 `## Memories` | `features.memories`、`use_memories`、会话 `/memories`、Phase 2 是否成功写出 summary |
| 记忆不更新 | 会话是否够 idle、rate-limit、Phase 1 job 是否 failed/backoff、全局 Phase 2 lock 是否卡住 |
| 配置未生效 | 是否改在了项目 `.codex/config.toml` 且项目未 trust；敏感键是否被忽略 |
| 指令像旧的 | 是否 `/resume` 了旧会话；改 `AGENTS.md` 后是否开了新 session |
| 跨项目串味 | 查看 `memory_summary.md` 是否含多 cwd；读路径当前过滤能力 |

本机快速查看：

```bash
ls -la "${CODEX_HOME:-$HOME/.codex}"
ls -la "${CODEX_HOME:-$HOME/.codex}/memories"
```

---

## 8. 参考来源

- [Config and state locations / Advanced Configuration](https://developers.openai.com/codex/config-advanced)
- [Environment variables](https://developers.openai.com/codex/environment-variables)
- [Memories](https://developers.openai.com/codex/memories)
- [AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- [`openai/codex` → `codex-rs/memories/README.md`](https://github.com/openai/codex/blob/main/codex-rs/memories/README.md)

文档版本：根据 2026 年公开文档与源码说明整理；上游结构或文件名可能随版本演进（例如 `state_N.sqlite` / `memories_N.sqlite` 的版本后缀）。
