# Codex Session 恢复与 Rollout 文件机制调研

本文基于近期对 OpenAI Codex（CLI / TypeScript SDK / Desktop·app-server）会话恢复行为的调研与实践结论，梳理：

- rollout 文件如何命名与存储
- `thread.id` 与业务 `session.id` 的关系
- `startThread` / `resumeThread` 的行为差异
- 跨机器导入恢复的可行路径
- `workingDirectory` 机制与路径不一致的影响
- CLI/SDK 与 Desktop/app-server 的差异

参考来源：`openai/codex` 公开源码（尤其 `sdk/typescript`、`codex-rs/rollout`、`codex-rs/exec`）、官方文档与相关 GitHub issue。上游版本演进可能导致细节变化。

---

## 1. 结论摘要

1. **可恢复身份是 Codex `thread.id`（UUID）**，对应 rollout 文件名末尾 UUID，以及文件首行 `session_meta.id`。业务库自己的 `session.id` 与之无关，需自行映射。
2. **`startThread` 不能指定 session/thread id**；刚返回时 `thread.id` 多为 `null`，要等第一次 `run()` 收到 `thread.started` 后才有值。
3. **`resumeThread(不存在的 id)` 会失败**（常见 `no rollout found` / `thread not found`），不会用该 id 新建会话。
4. **CLI/SDK 按明确 UUID resume**：多数情况下**只导入对应 rollout 文件即可**，不必迁移 SQLite；查找路径会先查 DB，再回退扫 `sessions/**/rollout-*-{uuid}.jsonl`。
5. **Desktop/app-server 常驻**，导入后更依赖退出再开；侧边栏列表更依赖 sqlite / 索引。
6. **`workingDirectory` 是 agent 工作根目录**，不是 rollout 存放目录。恢复时新旧路径不同：对话上下文通常仍可续，但后续读写/命令跟**新 cwd**走。

---

## 2. 存储位置与文件形态

### 2.1 根目录

| 变量 | 默认 | 含义 |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | 用户级状态根 |
| `CODEX_SQLITE_HOME` | 同 `CODEX_HOME` | SQLite 状态位置（可另置） |

### 2.2 Rollout 路径与命名

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-{YYYY-MM-DDThh-mm-ss}-{threadId}.jsonl
```

示例：

```text
~/.codex/sessions/2026/08/12/rollout-2026-08-12T10-30-00-019d5fe8-3730-7dc0-b126-bd5bd37446bd.jsonl
```

| 部分 | 含义 |
|---|---|
| `YYYY/MM/DD` | 按创建日期分区 |
| 时间戳 | 创建时刻；`:` 换成 `-`，便于跨文件系统 |
| **末尾 UUID** | `ThreadId` / `conversation_id` |

源码中文件名由 `conversation_id`（ThreadId）拼出，例如：

```text
rollout-{date_str}-{conversation_id}.jsonl
```

### 2.3 文件内容（JSONL）

- **第一行**：`session_meta`（或带 `payload` 包装）
  - `id`：**与文件名 UUID 一致**（thread id）
  - `cwd`：创建时工作目录
  - `session_id`：另一字段，**不要拿它去对文件名**
  - `model_provider`、`originator`、`source`、git 信息等
- **后续行**：消息、tool call、`turn_context`（可含更新后的 `cwd` / model）、用量等

归档会话可能在 `archived_sessions/` 下，机制类似。

### 2.4 相关但非必须的状态

| 路径 | 作用 | CLI/SDK 按 uuid resume |
|---|---|---|
| `sessions/**/*.jsonl` | 会话正文 | **必需** |
| `state_5.sqlite` | 线程元数据/索引 | 可选（加速/Desktop） |
| `session_index.jsonl` | 列表索引 | Desktop 更相关 |
| `memories*` / `auth.json` | 记忆/登录 | 与单会话 resume 无直接关系 |

---

## 3. 身份模型：谁才是 “session id”

| 标识 | 来源 | 用途 |
|---|---|---|
| 业务 `session.id` | 自有 DB | 产品会话主键 |
| `codexThreadId` / `thread.id` | Codex | resume、对 rollout |
| 文件名末尾 UUID | 磁盘 | 与 `thread.id`、`session_meta.id` 相同 |
| `session_meta.session_id` | meta 内字段 | **不等于**文件名 UUID，勿混用 |

推荐映射：

```text
your_session.id  ↔  codexThreadId (= thread.id = rollout UUID)
```

从文件反推 id：

```ts
// 稳妥：读第一行 session_meta.id / payload.id
// 或：文件名 rollout-...-<uuid>.jsonl 取末尾 uuid 段
```

从 id 找文件：

```ts
// $CODEX_HOME/sessions/**/rollout-*-${threadId}.jsonl
```

---

## 4. SDK API 行为

### 4.1 `startThread`

```ts
thread = codex.startThread({ workingDirectory: session.cwd });
```

要点：

- **不能传入自定义 sessionId / threadId**
- `ThreadOptions` 仅有 model、sandbox、workingDirectory、skipGitRepoCheck 等
- 返回时 `thread.id` 通常为 **`null`**
- 官方注释：`Populated after the first turn starts`
- 第一次 `run()` / `runStreamed()` 收到 `thread.started` 后：`this._id = parsed.thread_id`
- 持久化应在 **首轮 `run` 之后** 写入 `codexThreadId: thread.id`

时间线：

```text
startThread()     → thread.id = null，磁盘通常尚无 rollout
run() 开始        → thread.started → thread.id = uuid
                  → 创建 rollout-...-{uuid}.jsonl
run() 结束        → 可安全持久化 thread.id
```

### 4.2 `resumeThread`

```ts
thread = codex.resumeThread(existingThreadId, {
  workingDirectory: session.cwd,
});
await thread.run(followUp);
```

要点：

- 参数是 **已有 thread UUID**，不是完整文件名
- SDK 底层大致执行：`codex exec ... resume <threadId>`
- 构造时即可带上 id；续写同一条 thread / 同一份（同 id）rollout

### 4.3 推荐业务伪代码

```ts
if (await canResume(session.codexThreadId, sandbox)) {
  thread = codex.resumeThread(session.codexThreadId, {
    workingDirectory: session.cwd,
  });
} else {
  thread = codex.startThread({ workingDirectory: session.cwd });
  // 首条 message 可带上 DB 重建的上下文
}

const turn = await thread.run(userMessageOrRebuiltPrompt);

await sessionApi.appendTurn(session.id, {
  userMessage,
  turn,
  codexThreadId: thread.id, // start 路径下此处才非 null
});
```

`canResume` 应以 **目标环境磁盘上是否存在对应 rollout**（及可选线程库）为准，不能只看业务 DB 是否存过 id。

### 4.4 id 不存在时

`resumeThread(假 uuid)`：

- 典型错误：`no rollout found for thread id ...` / `thread ... not found`
- **不会**用该 id 新建 rollout
- 正确降级：`startThread` → 拿新 id

历史版本曾出现 “resume 缺失 id 却静默新开线程” 的问题，新版本多已改为明确失败或可控回退；集成方仍应自己做 `canResume` 分支。

---

## 5. 查找与恢复链路（CLI/SDK）

按明确 UUID resume 时，定位 rollout 大致逻辑：

```text
resume(threadId)
    │
    ├─ 校验 UUID
    ├─ 优先查 state DB 中的 rollout 路径并校验文件
    └─ 回退：扫描 $CODEX_HOME/sessions/**/rollout-*-{threadId}.jsonl
            │
            ▼
      找到 → 从 rollout 重载历史并继续 turn
      找不到 → 报错（不会盗用该 id 建新会话）
```

因此对 **CLI/SDK 一次性 resume**：

> **多数情况只导入 rollout 文件即可，不必操作 SQLite。**

SQLite 更重要的场景：

- Desktop 侧边栏列出会话
- `resume --last`、按标题搜索
- 需要稳定索引、归档状态的运维场景

---

## 6. 跨机器导入恢复

### 6.1 可行性

**可以。** 把旧机 rollout 放到新机同一 `CODEX_HOME` 布局下，再用原 UUID 调 `resumeThread`。

最小集合（CLI/SDK）：

```text
旧机: .../sessions/YYYY/MM/DD/rollout-...-<uuid>.jsonl
          ↓ 拷贝
新机: ~/.codex/sessions/YYYY/MM/DD/rollout-...-<uuid>.jsonl

resumeThread("<uuid>", { workingDirectory: "/new/path/to/project" })
```

更稳妥（尤其还要用 Desktop）：额外拷 `state_5.sqlite`（及 wal/shm）、`session_index.jsonl`，或备份整个 `~/.codex`（auth 可另处理）。

### 6.2 是否必须重启

| 入口 | 导入后 |
|---|---|
| **CLI / SDK 一次性调用** | 文件放对后通常立刻可 resume，**不必**专门重启 Codex 服务 |
| **Desktop / app-server** | 常驻进程有缓存/索引，**建议退出再开** |

操作建议：两边先退出 Codex → 拷文件 → 再启新机客户端；纯 SDK 脚本则可在文件到位后直接 resume。

### 6.3 路径与项目代码

- 迁 rollout **不会**自动迁项目仓库
- 新机需有对应代码目录，并用 `workingDirectory` 指向新路径
- 历史消息里的旧绝对路径可能误导模型，首条 follow-up 建议声明路径已变更

---

## 7. `workingDirectory` 机制

### 7.1 含义

`workingDirectory`（CLI `-C` / `--cd`）是 **agent 工作根目录**，决定：

- 相对路径读写、命令执行位置
- Git / 项目根发现
- 项目级 `.codex/config.toml`、`AGENTS.md` 加载
- 沙箱可写根 / 信任边界
- Desktop 按项目路径归类线程

它**不是** `$CODEX_HOME`，也不是 rollout 存放目录。

### 7.2 在 rollout 中的记录

- `session_meta.cwd`：创建时 cwd
- 后续 `turn_context.cwd`：可能随轮次变化；resume 元数据常取**最新** turn_context

### 7.3 恢复前后 cwd 不同

| 现象 | 说明 |
|---|---|
| 对话历史 | 通常仍可加载 |
| 新 turn 落盘/命令 | 跟**本次** `workingDirectory` / 进程 cwd |
| 不传新路径且旧路径不存在 | 可能失败或行为异常 |
| Desktop | 更易出现 “cwd missing / project moved”，列表绑旧绝对路径 |

SDK 实务：

```ts
codex.resumeThread(oldId, {
  workingDirectory: "/new/machine/path/to/repo",
});
```

交互式 CLI 在 cwd 不一致时可能询问用 current 还是 session；显式 `--cd` 优先。

---

## 8. Desktop / app-server vs CLI / SDK

```text
业务 App
  └─ Codex SDK ──► codex exec [resume <id>]
                       └─ 读写 $CODEX_HOME/sessions ...

Desktop UI
  └─ JSON-RPC ──► app-server（常驻）
                       └─ 同一套 thread / rollout 存储
```

| 维度 | CLI | SDK | Desktop / app-server |
|---|---|---|---|
| 入口 | 终端命令 | 代码库 | GUI + 本机服务 |
| 进程 | 短生命周期为主 | 每次 run 常拉起 CLI | **常驻** |
| 恢复 | `codex exec resume <id>` | `resumeThread(id)` | UI → `thread/resume` |
| 导入后 | 新进程读盘，较快生效 | 同左 | 更需重启 |
| 索引依赖 | 按 uuid 可扫文件 | 同左 | 列表更依赖 sqlite |

共同点：thread id、rollout 命名、`$CODEX_HOME` 语义一致。

---

## 9. 风险与踩坑清单

1. **把业务 `session.id` 当成 Codex thread id** → resume 失败或对错文件  
2. **`startThread` 后立刻读 `thread.id`** → 得到 `null`  
3. **`resumeThread` 传入完整文件名** → 应为 UUID  
4. **只信 DB 有 id、不查磁盘** → 换机/删文件后假阳性  
5. **换机只拷文件、不改 `workingDirectory`** → 工具仍指向旧绝对路径  
6. **期望用自定义 id 建会话** → 不支持；只能映射  
7. **Desktop 侧指望只拷 jsonl 就出现在侧边栏** → 可能不够，需索引/重启  
8. **混淆 `session_meta.session_id` 与文件名 UUID**

---

## 10. 验证清单（换机恢复）

```bash
# 1. 确认文件在新机 CODEX_HOME 下
ls "${CODEX_HOME:-$HOME/.codex}/sessions"/**/rollout-*-<uuid>.jsonl

# 2. 确认首行 id 一致
head -n1 .../rollout-...-<uuid>.jsonl

# 3. SDK/CLI resume
# resumeThread("<uuid>", { workingDirectory: "/new/project" })

# 4. 跑一轮后确认仍在写同一 uuid 的文件（或同 id 续写）
```

业务侧断言：

- [ ] `canResume` 基于文件（或等价探测）  
- [ ] start 路径在 `run` 后持久化 `thread.id`  
- [ ] resume 失败降级 start，并更新映射  
- [ ] 显式传入新机 `workingDirectory`  
- [ ] 不把假 uuid 强行当作可 resume

---

## 11. 与本仓库其他文档的关系

- `$CODEX_HOME` 解析、用户级根与项目 `.codex/` 边界、隔离与演进：见 [codex-home.md](./codex-home.md)
- 本文聚焦：**Session / Rollout / Resume** 路径，服务自建编排（DB 会话 ↔ Codex thread）集成

---

## 12. 参考

- `openai/codex` → `sdk/typescript/src/{codex,thread,threadOptions,exec}.ts`
- `openai/codex` → `codex-rs/rollout`（命名、`find_thread_path_by_id_str`）
- `openai/codex` → `codex-rs/exec`（`resume` 解析）
- [Codex SDK 文档](https://developers.openai.com/codex/sdk)（若入口变更以官网为准）
- 相关 issue：`no rollout found`、resume 缺失 id 行为、项目路径移动后 Desktop 不可用等

---

*文档整理自 2026-08 会话调研；若升级 Codex 大版本，请复核 `thread.id` 赋值时机与 resume 失败语义。*
