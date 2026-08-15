# Codex `CODEX_HOME` 机制调研

本文调研 OpenAI Codex 的用户级状态根目录机制：默认 `~/.codex`，可由环境变量 `CODEX_HOME` 覆盖。重点不是罗列每个文件名，而是回答：

- `CODEX_HOME` 如何被解析
- 它与仓库内 `<repo>/.codex/` 的本质区别
- 配置、凭证、会话、SQLite、记忆等子系统如何挂在这棵根上
- 历史上为何「把项目 `.codex` 当成 `CODEX_HOME`」被拒绝
- 集成方如何安全地隔离、迁移、排查

参考来源：官方 Environment Variables / Advanced Configuration / Config basics、`openai/codex` 中 `codex-rs/utils/home-dir` 等公开实现，以及相关 GitHub issue/PR。上游版本演进可能导致文件名与字段变化。

---

## 1. 结论摘要

1. **`CODEX_HOME` 是用户级状态根**，不是「当前仓库的配置目录」。默认 `~/.codex`；设置了环境变量时，目录**必须已存在且为目录**，否则启动失败。
2. **仓库内 `<repo>/.codex/` 是另一层**：仅在项目被 trust 时作为 project layer 加载（config / hooks / rules 等），**不会**自动变成 `CODEX_HOME`，也不会默认接管 auth、sessions、SQLite。
3. **解析实现集中在 `find_codex_home()`**（`codex-rs/utils/home-dir`）：有 `CODEX_HOME` → 校验存在 + `canonicalize`；无 → `$HOME/.codex`（不强制该默认路径事先存在）。
4. **配置优先级（高 → 低）大致为**：CLI / `-c` 覆盖 → 项目 `.codex/config.toml`（trusted，近者优先）→ `--profile` 文件 → 用户 `$CODEX_HOME/config.toml` → 系统 `/etc/codex/config.toml` → 内置默认。
5. **敏感键禁止项目层覆盖**（如 provider、notify、otel、profile 选择等），防止仓库配置劫持凭证与遥测路由。
6. **`CODEX_SQLITE_HOME` / `sqlite_home` 可把 SQLite 状态与 `CODEX_HOME` 拆开**；相对路径相对 CWD 解析，`sqlite_home` 配置优先于环境变量。
7. **社区曾提案「cwd 下 `.codex` 优先作 `CODEX_HOME`」（PR #4007），因沙箱安全风险被关闭**；当前正式模型仍是「用户家目录根 + 项目 overlay」。
8. **从家目录启动时，曾把 `~/.codex` 误当成未信任的项目层**（issue #9932）；后续用「跳过等于 `CODEX_HOME` 的 project layer」修复（PR #10207）。

---

## 2. 它是什么：一张根上的职责图

```text
                    ┌─────────────────────────────────────┐
                    │           CODEX_HOME                │
                    │     (默认 ~/.codex，可环境变量覆盖)   │
                    └─────────────────────────────────────┘
           ┌────────────┬────────────┬────────────┬────────────┐
           ▼            ▼            ▼            ▼            ▼
        配置/凭证     会话/历史     SQLite状态*   记忆产物      技能/规则/包
     config.toml    sessions/     state_*.sqlite  memories/    skills/
     *.config.toml  history.jsonl memories_*.sqlite            rules/
     auth.json      logs/         logs_*.sqlite                packages/
     AGENTS.md                    goals_*.sqlite
                                  *也可迁到 CODEX_SQLITE_HOME
```

对照另一套路径：

| 路径 | 角色 | 是否等于 `CODEX_HOME` |
|---|---|---|
| `$CODEX_HOME`（默认 `~/.codex`） | 用户级根：配置、auth、sessions、sqlite、memories… | 是 |
| `<repo>/.codex/` | 项目 overlay：config / hooks / rules 等 | **否** |
| `workingDirectory` / CLI `-C` | agent 工作根，决定相对路径与项目发现 | **否** |

一句话：**`CODEX_HOME` 管「Codex 自己的家」；`<repo>/.codex/` 管「这个仓库允许覆盖什么」；`workingDirectory` 管「agent 在哪干活」。**

---

## 3. 解析机制：`find_codex_home()`

公开实现位于 `codex-rs/utils/home-dir`（历史上也曾在 `codex-core` 的 config 模块中）。逻辑可概括为：

```text
find_codex_home()
    │
    ├─ 读环境变量 CODEX_HOME
    │     ├─ 未设置或空串
    │     │     └─ 返回 $HOME/.codex
    │     │        （不强制该路径已存在）
    │     └─ 已设置非空
    │           ├─ 路径不存在 → Err(NotFound)
    │           ├─ 存在但不是目录 → Err(InvalidInput)
    │           └─ 是目录 → canonicalize → AbsolutePathBuf
    │
    └─ 结果写入运行时 Config.codex_home，供各子系统使用
```

### 3.1 为何「手动设置必须先存在」

官方 Environment Variables 写明：若设置 `CODEX_HOME`，目录必须已经存在。源码侧会先 `metadata`，再 `canonicalize`，并给出明确错误文案，例如：

- `CODEX_HOME points to "...", but that path does not exist`
- `... but that path is not a directory`
- `failed to canonicalize CODEX_HOME ...`

设计意图：

- **避免静默建错路径**（拼写错误、未挂载卷、CI 未 mkdir）
- **强制显式准备隔离目录**（多用户 / 多租户 / 测试夹具）
- **canonicalize 消除符号链接歧义**，方便后续「是否等于某个 project `.codex`」的路径比较

默认 `~/.codex` 路径则更宽松：未设置 env 时不必事先存在，由后续写操作按需创建。

### 3.2 Windows / 跨平台注意

- macOS / Linux / WSL：默认 `~/.codex`
- Windows 原生：通常 `%USERPROFILE%\.codex`
- 环境变量覆盖后语义一致：都是「用户级状态根」

### 3.3 相关环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | 用户级状态根（config、auth、logs、sessions、skills、packages…） |
| `CODEX_SQLITE_HOME` | 同 `CODEX_HOME` | SQLite 状态目录；配置项 `sqlite_home` 优先；相对路径相对 **CWD** |
| `CODEX_INSTALL_DIR` | 平台相关 bin 目录 | 只影响可见的 `codex` 可执行文件安装位置；standalone 包缓存仍在 `$CODEX_HOME/packages/standalone` |

---

## 4. 关键边界：不要把项目 `.codex` 当成 `CODEX_HOME`

### 4.1 当前正式模型

```text
用户层（始终可读）
  $CODEX_HOME/config.toml
  $CODEX_HOME/<name>.config.toml   # --profile
  $CODEX_HOME/hooks.json / rules / …
  系统层 /etc/codex/…（若存在）

项目层（仅 trusted）
  <project_root>/.codex/config.toml
  … 沿路径走到 CWD 的每一层 .codex/config.toml
  同层 hooks / rules
```

项目层特点：

- 从 **project root → CWD** 路径上合并多个 `.codex/config.toml`
- **离 CWD 更近的覆盖更远的**
- 相对路径（如 `model_instructions_file`）相对**包含该 config 的 `.codex/` 目录**解析
- **未信任项目**：忽略整个项目 `.codex/` 层（config、hooks、rules）；用户层与系统层仍生效

### 4.2 项目层禁止覆盖的敏感键

官方列出（出现在项目 `config.toml` 会被忽略并告警），包括但不限于：

- `openai_base_url` / `chatgpt_base_url`
- `model_provider` / `model_providers`
- `notify` / `otel`
- `profile` / `profiles`
- `apps_mcp_product_sku` / `experimental_realtime_ws_base_url`

原因：这些键会改 credential 路由、provider、通知命令、遥测出口或 profile 选择，属于**主机侧安全面**，不能被仓库内容静默劫持。

### 4.3 历史岔路：PR #4007（已关闭）

社区 PR 曾提议把解析顺序改成：

1. CWD 下存在 `.codex/` → 直接当作 `CODEX_HOME`
2. 否则看环境变量
3. 否则 `~/.codex`

动机是「进仓库自动隔离 config/auth/history」。维护者关闭该 PR 的核心理由是**沙箱安全**：

- 默认 `workspace-write` 下，agent 可能改写仓库内 `.codex/config.toml`
- 恶意或被诱导的写入可把 `sandbox_mode` 等调到更危险策略
- macOS Seatbelt 能对子目录做更严只读；Linux Landlock/Seccomp 当时难以对「可写仓库内的 `.codex` 子目录」单独加严

因此当前产品选择是：

> **项目 `.codex` = 受限 overlay（需 trust + 敏感键黑名单）**  
> **不是**完整替换用户级 `CODEX_HOME`

若需要真正的隔离根，应显式：

```bash
mkdir -p /tmp/codex-home-acme
export CODEX_HOME=/tmp/codex-home-acme
codex …
```

### 4.4 另一类 bug：把 `CODEX_HOME` 当成项目层（#9932 / #10207）

从 `$HOME` 启动且无 `.git` 时，project root 可能退化成 CWD（家目录），遍历会发现 `~/.codex`，并把它当成**未信任的项目配置层**，弹出 trust 警告。

修复：`load_project_layers()` 在发现 `.codex` 时，若路径（含 canonicalize 后）等于 `CODEX_HOME`，则**跳过**。这也解释了：即便有人把 `CODEX_HOME` 指到某个项目的 `.codex`，实现仍要避免双重身份冲突。

---

## 5. 配置加载与信任模型

### 5.1 优先级（高 → 低）

按 Config basics 公开说明：

1. CLI flags 与 `--config` / `-c` 覆盖
2. 项目 `.codex/config.toml`（root→CWD，近者优先；仅 trusted）
3. `--profile <name>` 对应的 `$CODEX_HOME/<name>.config.toml`
4. 用户 `$CODEX_HOME/config.toml`
5. 系统 `/etc/codex/config.toml`（Unix，若存在）
6. 内置默认

Profile 说明（0.134.0+）：

- 使用独立文件 `$CODEX_HOME/<profile>.config.toml`
- 不再依赖 `config.toml` 内 `[profiles.*]` / 顶层 `profile =` 选择器（旧写法需迁移）

### 5.2 Project root 发现

- 默认：向上找到含 `.git` 的目录作为 project root
- 可配 `project_root_markers = [".git", ".hg", ".sl"]`
- `project_root_markers = []`：不向上搜索，**把 CWD 当作 root**

这直接影响：能看到哪些项目 `.codex/`、能拼出怎样的 `AGENTS.md` 链、Desktop 如何按项目归类线程。

### 5.3 Trust

用户级 `config.toml` 中可标记：

```toml
[projects."/absolute/path/to/repo"]
trust_level = "trusted"   # 或 "untrusted"
```

- `untrusted`：跳过项目 `.codex/` 层
- trust 不改变 `CODEX_HOME` 本身，只改变「是否吃仓库 overlay」

---

## 6. `$CODEX_HOME` 目录布局与子系统归属

实际机器不一定出现全部文件；版本后缀（如 `state_5.sqlite`）会随 migrator 演进。

```text
$CODEX_HOME/
├── config.toml                 # 用户主配置
├── <profile>.config.toml       # --profile
├── auth.json                   # 文件型凭证（也可改用 OS keychain）
├── AGENTS.md / AGENTS.override.md
├── history.jsonl               # 可关闭的本地历史
├── sessions/YYYY/MM/DD/        # rollout-*.jsonl 会话 transcript
├── archived_sessions/          # 归档会话（若有）
├── state_N.sqlite              # 线程/可恢复运行时状态
├── memories_N.sqlite           # 记忆流水线状态
├── logs_N.sqlite               # 结构化日志状态
├── goals_N.sqlite              # goals 状态
├── memories/                   # 记忆文件系统产物
├── log/ 或 logs/               # 运行日志；显式 log_dir 可开 plaintext TUI log
├── skills/                     # 用户级 skills
├── rules/                      # 用户级 rules
├── hooks.json                  # 用户级 hooks（也可写在 config.toml）
└── packages/                   # standalone / release 缓存等
```

### 6.1 配置与凭证

| 路径 | 作用 |
|---|---|
| `config.toml` | 模型、features、memories、MCP、sandbox、history、trust… |
| `<name>.config.toml` | 命名 profile |
| `auth.json` | 文件型登录态；也可走系统钥匙串 / Credential Manager |
| `AGENTS.md` / `AGENTS.override.md` | 全局指令；override 非空时优先 |

### 6.2 会话与历史

| 路径 | 作用 |
|---|---|
| `history.jsonl` | 本地命令/会话历史（可关） |
| `sessions/**/rollout-*.jsonl` | 会话 transcript；resume 主数据源；也是记忆 Phase 1 输入 |
| `archived_sessions/` | 归档形态，机制类似 |

Rollout 命名与 resume 细节见 [codex-session-rollout-resume.md](./codex-session-rollout-resume.md)。要点：

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-{timestamp}-{threadId}.jsonl
```

CLI/SDK 按 UUID resume 时，多数情况**只需 rollout 文件**；SQLite 更偏 Desktop 列表与索引加速。

### 6.3 SQLite 运行时状态

| 库 | 典型职责 |
|---|---|
| `state_N.sqlite` | 线程元数据、可恢复运行时、job 协调 |
| `memories_N.sqlite` | stage-1、Phase 1/2 租约、usage、watermark |
| `logs_N.sqlite` | 结构化日志状态 |
| `goals_N.sqlite` | goals 状态 |

拆库原因：降低锁竞争；记忆表可独立清理/重建，不必与 durable thread records 共 schema。

`StateRuntime` 负责初始化、migration，并暴露 memories / jobs / logs 等入口。

**与 `CODEX_HOME` 的关系：**

```text
默认：SQLite 文件落在 CODEX_HOME
可覆盖：
  1) config 中 sqlite_home（优先）
  2) 环境变量 CODEX_SQLITE_HOME
相对路径：相对当前工作目录解析（不是相对 CODEX_HOME）
```

适用场景：把大体积 DB 放到更快磁盘、或让多个 `CODEX_HOME` 共享/分离索引策略。

### 6.4 记忆产物 `memories/`

| 路径 | 作用 |
|---|---|
| `raw_memories.md` | Phase 2 同步的合并 raw memories |
| `rollout_summaries/` | 选中 rollout 的摘要 |
| `memory_summary.md` / `MEMORY.md` | 巩固后高层记忆，供读路径注入 |
| `phase2_workspace_diff.md` | 相对上次成功 Phase 2 baseline 的 diff |
| `.git/` | memories 工作区 baseline（`codex-git-utils`） |
| `extensions/` 等 | 扩展资源；过期会被 prune |

官方倾向：这些是**生成态状态**，排查用，不宜当主要手改入口。记忆流水线细节见下文第 8 节。

### 6.5 日志、skills、packages

| 路径 | 作用 |
|---|---|
| `log/` / `logs/` | 运行日志；`log_dir` 显式设置时可生成 `codex-tui.log` |
| `skills/` | 用户级 skills（还会从 `~/.agents/skills`、仓库 `.agents/skills` 等发现） |
| `rules/` | 用户级 rules |
| `packages/` | standalone package / release 缓存元数据 |

安装器注意：改 `CODEX_INSTALL_DIR` 只改 bin 落点，**不**改 `packages/standalone` 所在的 `CODEX_HOME`。

---

## 7. 指令链（`AGENTS.md`）如何挂在 `CODEX_HOME` 上

会话启动时构建 instruction chain（每个 run / TUI session 一次）：

1. **全局**：`$CODEX_HOME` 下优先 `AGENTS.override.md`，否则 `AGENTS.md`
2. **项目**：从仓库根走到 CWD；每一层最多取一个文件（override → `AGENTS.md` → fallback 名）
3. **合并**：自上而下拼接；越靠近 CWD 越靠后，语义上可覆盖上层
4. **限制**：空文件跳过；总大小受 `project_doc_max_bytes`（默认约 32KiB）约束

`/resume` 旧会话时，可能仍沿用创建时指令上下文，而不立刻重读磁盘最新 `AGENTS.md`。

---

## 8. 记忆系统如何依赖 `CODEX_HOME`

记忆默认关闭。开启示例：

```toml
[features]
memories = true

[memories]
generate_memories = true
use_memories = true
```

依赖关系：

| 组件 | 落点 |
|---|---|
| 原始输入 | `$CODEX_HOME/sessions/**/*.jsonl` |
| job / stage-1 | `memories_N.sqlite`（或 `CODEX_SQLITE_HOME`） |
| 磁盘产物 | `$CODEX_HOME/memories/` |
| 读路径注入 | 巩固后的 summary → developer instructions（如 `## Memories`） |

两阶段：

```text
Phase 1（可并行）：eligible rollout → 抽取 → memories_*.sqlite
Phase 2（全局锁）：top-N stage-1 → 同步 memories/ →（若 dirty）consolidation agent
```

触发条件（根会话启动、后台异步）：非 ephemeral、feature 开、非 sub-agent、state DB 可用；另受信 idle、扫描上限、rate-limit 等约束。

已知张力：写路径会尽量按 cwd/项目区分，读路径历史上可能整文件注入全局 summary，存在跨项目串味风险——这与「单用户级 `CODEX_HOME` 共享 memories 根」直接相关。真隔离请用独立 `CODEX_HOME`。

---

## 9. 各入口如何消费 `CODEX_HOME`

```text
codex CLI / TUI
  └─ find_codex_home() → 读写 config / sessions / sqlite / memories

TypeScript SDK
  └─ 底层常拉起 `codex exec …`
        └─ 同一套 CODEX_HOME（继承进程环境变量）

Desktop / app-server
  └─ 常驻进程 + 同一状态根
        └─ 侧边栏/索引更依赖 sqlite；导入文件后常需重启

Installer (install.sh / install.ps1)
  └─ 可见 bin → CODEX_INSTALL_DIR
  └─ standalone 缓存 → $CODEX_HOME/packages/standalone
```

集成建议：

- SDK/自动化进程若要隔离，在**启动该进程前**设置 `CODEX_HOME`（并 `mkdir`）
- Desktop 与 CLI 共用默认家目录时，改文件后注意常驻缓存
- 跨机迁移：CLI/SDK resume 最小集是 rollout；完整体验再拷 sqlite / 整个 `CODEX_HOME`（auth 单独处理）

---

## 10. 常见使用模式

### 10.1 默认个人开发机

```bash
# 不设置 CODEX_HOME
# 使用 ~/.codex
codex
```

仓库内可提交**无密钥**的 `.codex/config.toml` / rules 模板；敏感项放用户级。

### 10.2 CI / 并行任务隔离

```bash
CODEX_HOME="$(mktemp -d)"
mkdir -p "$CODEX_HOME"   # 若 mktemp -d 已创建则可省略；关键是路径存在
export CODEX_HOME
codex exec "…"
```

每个 job 独立 auth/session/memory，避免互相抢 `state_*.sqlite` 锁或串会话。

### 10.3 只拆开 SQLite

```toml
# $CODEX_HOME/config.toml
sqlite_home = "/var/lib/codex-sqlite"
```

或：

```bash
export CODEX_SQLITE_HOME=/var/lib/codex-sqlite
```

配置优先于环境变量；相对路径相对 **CWD**。

### 10.4 不要做的事

- 以为「仓库有 `.codex/` 就会自动换家目录」
- 把 `CODEX_HOME` 指到尚不存在的路径
- 在项目 `config.toml` 里塞 `model_providers` / `notify` 指望生效
- 多个并发 agent 共用同一 `CODEX_HOME` 却期望完全隔离记忆与线程锁

---

## 11. 排查速查

| 现象 | 优先检查 |
|---|---|
| 启动报 `CODEX_HOME … does not exist` | 先 `mkdir -p` 再导出；检查拼写与挂载 |
| 项目配置不生效 | 项目是否 trusted；键是否在敏感黑名单；是否改错层 |
| 从 `~` 启动出现 trust 警告 | 旧版本 #9932；升级含 #10207 的版本，或避免把家目录当项目根 |
| resume 找不到会话 | `$CODEX_HOME/sessions/**/rollout-*-<uuid>.jsonl` 是否在**当前** home 下 |
| Desktop 列表缺会话 | 可能还需 sqlite / 重启；不只看 jsonl |
| 新会话无 `## Memories` | `features.memories`、`use_memories`、Phase 2 产物、是否共用错 home |
| Profile 不生效 | 是否使用 `$CODEX_HOME/<name>.config.toml` + `--profile`（新模型） |

本机快速查看：

```bash
echo "CODEX_HOME=${CODEX_HOME:-$HOME/.codex}"
ls -la "${CODEX_HOME:-$HOME/.codex}"
ls -la "${CODEX_HOME:-$HOME/.codex}/sessions" 2>/dev/null | head
ls -la "${CODEX_HOME:-$HOME/.codex}/memories" 2>/dev/null | head
```

---

## 12. 机制演进时间线（与家目录相关）

| 事件 | 含义 |
|---|---|
| 长期默认 `~/.codex` + `CODEX_HOME` 覆盖 | 用户级状态根模型确立 |
| 项目 `.codex/config.toml` + trust | overlay 而非替换 home |
| PR #4007 关闭 | 拒绝「cwd `.codex` = CODEX_HOME」，因沙箱可自改配置 |
| issue #9932 / PR #10207 | 跳过把 `CODEX_HOME` 当作 project layer |
| PR #10249 | 显式校验：env 指向的 home 必须存在且为目录 |
| Profile 文件化（0.134.0+） | `$CODEX_HOME/<name>.config.toml` |
| SQLite 可外置 | `CODEX_SQLITE_HOME` / `sqlite_home` |

---

## 13. 与本仓库其他文档的关系

- 本文：`CODEX_HOME` **解析、边界、分层、隔离与演进**
- [codex-session-rollout-resume.md](./codex-session-rollout-resume.md)：`$CODEX_HOME/sessions` 上的 **thread id / rollout / resume / 跨机导入**

---

## 14. 参考来源

- [Environment variables](https://developers.openai.com/codex/environment-variables)
- [Config basics](https://developers.openai.com/codex/config-basic)
- [Advanced Configuration](https://developers.openai.com/codex/config-advanced)（Config and state locations、project config、hooks、project root）
- [Configuration Reference](https://developers.openai.com/codex/config-reference)（`sqlite_home`、`projects.*.trust_level` 等）
- [Memories](https://developers.openai.com/codex/memories)
- [AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- `openai/codex` → `codex-rs/utils/home-dir`（`find_codex_home`）
- `openai/codex` → `codex-rs/memories/README.md`
- GitHub：PR #4007（关闭）、#10207、#10249；issue #9932

---

*文档整理自 2026-08 公开资料与源码说明；若升级 Codex 大版本，请复核 `find_codex_home` 校验语义、项目层敏感键列表，以及 `state_N.sqlite` / `memories_N.sqlite` 版本后缀。*
