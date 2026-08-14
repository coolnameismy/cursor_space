# Codex App Server 技术调研

本文梳理 OpenAI Codex **App Server**（`codex app-server`）的定位、内部架构、JSON-RPC 协议、核心原语、生命周期、审批流与客户端集成模式。内容基于官方工程博文、开发者文档与 `openai/codex` 仓库 `codex-rs/app-server` 公开实现；上游版本演进可能导致细节变化。

> **选型一句话**：需要富客户端（IDE / Desktop / 自建 UI）时用 App Server；一次性自动化 / CI 优先用 Codex SDK（`codex exec`）。两者共享同一套 Codex core、thread id 与 `$CODEX_HOME` 持久化语义。

---

## 1. 它是什么、解决什么问题

**App Server = 协议 + 常驻进程。**

- **协议**：面向客户端的双向 JSON-RPC 2.0 API（线上通常省略 `"jsonrpc":"2.0"` 字段）。
- **进程**：长期运行、托管 Codex core threads 的本地（或容器内）服务。

Agent 交互不是简单的「一次请求 → 一次回复」：用户一句话会展开成结构化动作序列（流式推理、命令执行、文件 diff、审批、工具调用等）。客户端需要稳定、可渲染的事件模型，而不是直接消费 core 内部事件。

OpenAI 用 App Server 把 **Codex core（agent loop / 工具 / 配置 / 鉴权）** 与 **各客户端表面（CLI TUI、VS Code、Desktop、Web、JetBrains、Xcode、第三方）** 解耦：core 只写一套，各表面通过同一稳定 API 驱动。

历史上团队曾尝试把 Codex 暴露成 MCP server 给 VS Code 用，但 IDE 需要的会话语义（流式 diff、审批、thread 持久化）与 MCP 的「工具导向」模型不契合，最终单独设计了 App Server。MCP 仍用于「给 agent 挂工具」；全保真客户端集成推荐 App Server。

---

## 2. 在 Codex 体系中的位置

```text
┌─────────────────────────────────────────────────────────┐
│  Clients: VS Code / Desktop / JetBrains / Xcode / Web /  │
│           自建产品 / Remote TUI                           │
└───────────────────────────┬─────────────────────────────┘
                            │ JSON-RPC（stdio / ws / unix）
                            ▼
┌─────────────────────────────────────────────────────────┐
│  codex app-server                                        │
│  ├─ Transport reader（stdio / ws / unix）                 │
│  ├─ Message processor（请求翻译 + 事件投影）               │
│  ├─ Thread manager                                       │
│  └─ Core threads（每 thread 一个 core session）           │
└───────────────────────────┬─────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Codex core + $CODEX_HOME（sessions/rollout、sqlite…）    │
└─────────────────────────────────────────────────────────┘
```

| 入口 | 典型用途 | 进程形态 |
|---|---|---|
| `codex` / TUI | 交互式终端 | 可连本地或 remote app-server |
| `codex exec` / TypeScript SDK | 脚本、CI、一次性任务 | 短生命周期为主 |
| **`codex app-server`** | IDE、Desktop、自建富 UI | **常驻**，双向 RPC |
| Codex as MCP | 简单工具式嵌入 | 能力子集，非全保真 |

与本仓库其他文档的关系：

- `$CODEX_HOME` 目录与记忆：见 [codex-home.md](./codex-home.md)
- Session / Rollout / Resume：见 [codex-session-rollout-resume.md](./codex-session-rollout-resume.md)
- 本文聚焦：**App Server 协议与进程本身**

---

## 3. 内部架构（四个主要部件）

官方工程博文给出的进程内结构：

1. **stdio / transport reader** — 读入客户端 JSON-RPC 消息  
2. **Codex message processor** — 把客户端请求翻译成 core 操作，并把 core 内部事件投影成稳定、UI 友好的通知  
3. **Thread manager** — 为每个 thread 拉起 / 管理一个 core session  
4. **Core threads** — 真正跑 agent loop、工具、沙箱的会话实例  

关键设计点：

- **一对多**：一次客户端请求（如 `turn/start`）会触发大量服务端通知（`item/*`、`turn/*`）。
- **翻译层**：客户端不直接接触 core 低层事件；processor 保证协议面相对稳定、可向后兼容。
- **背压**：ingress / 处理 / outbound 之间使用有界队列；饱和时新请求返回 JSON-RPC `-32001`（`"Server overloaded; retry later."`），客户端应指数退避 + jitter 重试。
- **卸载策略**：连接对 thread `unsubscribe` 后，若无订阅者且一段时间无活动（文档记载约 30 分钟），server 可卸载 thread、跑 `SessionEnd` hooks，并发 `thread/closed`。

---

## 4. 核心原语：Item / Turn / Thread

| 原语 | 含义 | 生命周期要点 |
|---|---|---|
| **Item** | 原子输入/输出单元 | `item/started` → 可选 `* /delta` → `item/completed` |
| **Turn** | 一次用户输入触发的一整段 agent 工作 | `turn/started` → 多个 items → `turn/completed` |
| **Thread** | 持久会话容器 | `start` / `resume` / `fork` / `archive` / `delete` |

常见 Item 类型（不完全列表）：

| type | 用途 |
|---|---|
| `userMessage` | 用户输入（text / image / localImage / audio…） |
| `agentMessage` | Agent 回复（可流式 delta） |
| `reasoning` | 推理摘要 / 原文 |
| `commandExecution` | 沙箱内命令执行 |
| `fileChange` | 文件修改 / patch |
| `mcpToolCall` | MCP 工具调用 |
| `collabToolCall` | 多 agent 协作工具（spawn / send_input / wait…） |
| `webSearch` / `imageGeneration` / `imageView` | 搜索与图像相关 |
| `enteredReviewMode` / `exitedReviewMode` | Review 模式进出 |
| `contextCompaction` | 上下文压缩 |
| `plan` / `sleep` | Plan 模式 / 等待 |

UI 渲染约定：

- **权威结果**看 `item/completed`，不要只信中间 delta  
- `turn/completed` 里的 agent message 只是摘要兜底；完整时间线以 `item/*` 为准  
- `turn/diff/updated` 提供本 turn 聚合 unified diff，便于「本轮改了什么」视图

---

## 5. 传输层与消息形态

### 5.1 Transports

| Transport | 启动方式 | 状态 | 成帧 |
|---|---|---|---|
| **stdio**（默认） | `codex app-server` 或 `--listen stdio://` | 稳定 | stdin/stdout 上 JSONL（一行一条消息） |
| **WebSocket** | `--listen ws://IP:PORT` | **experimental / unsupported** | 每帧一条 JSON；带 `/readyz`、`/healthz` |
| **Unix socket** | `--listen unix://` 或 `unix://PATH` | 本地控制面 | 默认 `$CODEX_HOME/app-server-control/app-server-control.sock`；HTTP Upgrade 后走 WS 帧 |
| **off** | `--listen off` | — | 不暴露本地监听 |

补充：

- `codex app-server proxy`：把 unix control socket 代理到本进程 stdin/stdout，便于控制面客户端。  
- Remote TUI：`codex app-server --listen ws://127.0.0.1:4500`，另一台机器 `codex --remote ws://...`。非本机应配 TLS + WS auth（`--ws-auth` + token file / sha256 / signed bearer）。  
- 日志：`RUST_LOG`；`LOG_FORMAT=json` 时 tracing 以 JSON 行写 stderr。

### 5.2 三种消息

```jsonc
// Client → Server 请求
{"id": 1, "method": "thread/start", "params": {...}}

// Server → Client 响应
{"id": 1, "result": {...}}
{"id": 1, "error": {"code": -32601, "message": "Method not found"}}

// 通知（任一方，无 id）
{"method": "item/agentMessage/delta", "params": {...}}

// Server → Client 请求（双向：审批等）
{"id": 42, "method": "item/commandExecution/requestApproval", "params": {...}}
```

线上通常**省略** `"jsonrpc":"2.0"`。客户端实现必须能处理「带 id 的入站 method」（服务端请求），不能只处理 response。

### 5.3 Schema 生成

与当前安装的 Codex 版本严格绑定：

```bash
codex app-server generate-ts --out DIR
codex app-server generate-json-schema --out DIR
```

升级 CLI 后应重新生成；否则类型/校验易漂移。

---

## 6. 连接生命周期（必做握手）

每个 transport 连接必须先完成：

1. 客户端 `initialize`（带 `clientInfo`，可选 `capabilities`）  
2. 客户端发 `initialized` 通知  
3. 之后才允许其他方法  

否则返回 `Not initialized`；重复 `initialize` 返回 `Already initialized`。

`initialize` 响应中常见字段：上游用的 user-agent、`codexHome`、`platformFamily` / `platformOs`。

`clientInfo.name` 会进入 OpenAI Compliance Logs 的客户端识别；企业集成需按官方要求登记已知客户端名。

常用 capabilities：

| 字段 | 作用 |
|---|---|
| `experimentalApi` | 打开实验 API（dynamic tools、部分 realtime / queue 等） |
| `optOutNotificationMethods` | 按**精确方法名**抑制通知（无通配）；适合 CI 关掉高频 delta |
| MCP 扩展声明 | 如 `openai/form`、`io.modelcontextprotocol/ui`；会话创建后固定，子 agent 继承 |
| `requestAttestation` | Desktop 等响应服务端 `attestation/generate` |

最小握手示例：

```json
{"method":"initialize","id":0,"params":{
  "clientInfo":{"name":"my_product","title":"My Product","version":"0.1.0"}
}}
{"method":"initialized","params":{}}
```

---

## 7. 典型对话流

```text
initialize → initialized
    → thread/start | thread/resume | thread/fork
        → turn/start
            ← turn/started
            ← item/started / deltas / item/completed  （多次）
            ← （可选）server→client 审批请求，客户端回 accept/decline…
            ← turn/completed
        → turn/steer（可选，追加输入到进行中的 turn）
        → turn/interrupt（可选，取消）
```

### 7.1 `thread/start` / `resume` / `fork`

```json
{
  "method": "thread/start",
  "id": 10,
  "params": {
    "model": "gpt-5.1-codex",
    "cwd": "/Users/me/project",
    "approvalPolicy": "never",
    "sandbox": "workspaceWrite",
    "personality": "friendly",
    "serviceName": "my_app_server_client"
  }
}
```

要点：

- 成功响应含 `thread`（含 `id`）；并常伴随 `thread/started` 通知；连接自动订阅该 thread 的 turn/item 事件。  
- `personality`：`friendly` | `pragmatic` | `none`。  
- 带 `cwd` 且沙箱为 workspace-write / 全权限时，app-server 可能把该项目标为 trusted（写入用户 `config.toml`）。  
- `ephemeral: true`：内存临时 thread，`thread.path` 为 `null`，不落盘。  
- **`thread/resume`**：用持久化的 `thread.id`（与 rollout UUID / `session_meta.id` 一致）重开；历史从 rollout / 投影存储恢复。冷 resume 时可能立即发 `thread/tokenUsage/updated`。  
- **`thread/fork`**：复制历史到新 id；可用 `lastTurnId` / `beforeTurnId` 截断；源 thread 若在跑，fork 会先按 interrupt 语义打标记。  
- 实验性 `historyMode: "paginated"`：投影式分页历史；同一 thread 同一时间只允许一个 app-server 进程写持有，否则 `-32600`。

权限覆盖：优先实验性 `permissions`（profile id）；旧式 `sandbox` / `sandboxPolicy` 仍可用，但**不可与 `permissions` 同传**。

### 7.2 `turn/start` / `steer` / `interrupt`

```json
{
  "method": "turn/start",
  "id": 30,
  "params": {
    "threadId": "thr_123",
    "input": [{ "type": "text", "text": "Summarize this repo." }]
  }
}
```

- 立即返回 turn 对象；真正开始跑时发 `turn/started`。  
- `turn/steer`：向**进行中的普通 turn**追加输入，不新建 turn，不发 `turn/started`；review / 手动 compact 等不可 steer。  
- `turn/interrupt`：按 `(threadId, turnId)` 取消；最终 `turn/completed` 且 `status: "interrupted"`。不自动杀掉 background terminals。

可选覆盖：`model`、`cwd`、沙箱/权限、`approvalPolicy`、`approvalsReviewer`、`output_schema`、`skill_inputs`、实验性 `dynamicTools` 等。

`approvalsReviewer`：

| 值 | 行为 |
|---|---|
| `user`（默认） | 审批打到客户端 UI |
| `auto_review` | 交给专门子 agent 做风险决策（旧名 `guardian_subagent` 仍兼容） |

### 7.3 Thread 管理常用方法（摘要）

| 方法 | 作用 |
|---|---|
| `thread/list` | 分页列出会话（过滤 cwd、archived、searchTerm、section…） |
| `thread/read` | 只读加载，不 resume |
| `thread/archive` / `unarchive` / `delete` | 归档 / 恢复 / 硬删（含派生子线程） |
| `thread/name/set` | 设置显示名 |
| `thread/unsubscribe` | 取消本连接订阅 |
| `thread/compact/start` | 触发上下文压缩（进度走普通 item 流） |
| `thread/rollback` | **已弃用**：丢弃最后 N 个 turn |
| `review/start` | 内置代码审查（inline 或 detached 新 thread） |

另有大量实验面：queue、goal、realtime voice、remote control、plugin marketplace、filesystem helpers、`command/exec` 等——生产客户端应通过 `generate-ts` / 官方文档核对当前版本，并注意 `experimentalApi` 门槛。

---

## 8. 事件流与 Item 生命周期

订阅方式：`thread/start|resume|fork` 后持续读通知；可用 `optOutNotificationMethods` 关掉噪声。

### 8.1 Turn 级

| 通知 | 含义 |
|---|---|
| `turn/started` | turn 开始，`status: "inProgress"`，items 为空 |
| `turn/completed` | `completed` / `interrupted` / `failed` |
| `turn/diff/updated` | 本 turn 聚合 diff 快照 |
| `turn/plan/updated` | agent 计划步骤更新 |
| `thread/tokenUsage/updated` | token 用量（可与 resume 回放） |
| `error` | turn 中途错误（常先于 failed 的 `turn/completed`） |

常见 `codexErrorInfo`：`ContextWindowExceeded`、`UsageLimitExceeded`、`HttpConnectionFailed`、`SandboxError`、`Unauthorized`、`ActiveTurnNotSteerable` 等。

### 8.2 Item 级

统一：`item/started` →（可选）类型专用 delta → `item/completed`。

高频 delta 示例：

- `item/agentMessage/delta`  
- `item/reasoning/summaryTextDelta` / `textDelta`  
- `item/commandExecution/outputDelta`  
- `item/plan/delta`（实验）

批量 / CI 建议保留 `turn/*` 与 `item/started|completed`，关掉上述高频 delta。

---

## 9. 双向审批与人机协同

协议是**全双工**：服务端可主动向客户端发 **request**，turn 会暂停直到客户端响应。

典型路径（命令执行）：

1. `item/started`（`commandExecution`，`status: inProgress`）  
2. `item/commandExecution/requestApproval`（**server → client request**）  
3. 客户端回 `accept` / `acceptForSession` / `decline` / `cancel`  
4. `serverRequest/resolved`  
5. `item/completed`（`completed` / `failed` / `declined`）

类似还有：

- 文件变更：`item/fileChange/requestApproval`  
- 权限提升：`item/permissions/requestApproval`  
- 工具向用户提问：`item/tool/requestUserInput`  
- MCP elicitation：`mcpServer/elicitation/request`  
- Desktop attestation：`attestation/generate`  

这是 App Server 相对「纯 MCP 工具调用」的核心差异之一：**会话级审批与暂停语义内建在协议里**。

---

## 10. 与磁盘状态的关系

App Server **不另起一套会话真相源**；仍落在 Codex core 的 `$CODEX_HOME`：

```text
$CODEX_HOME/
├── config.toml / auth.json / …
├── sessions/YYYY/MM/DD/rollout-*-{threadId}.jsonl
├── archived_sessions/…
├── state_*.sqlite          # 索引 / 元数据（列表、section 等更依赖）
└── app-server-control/     # unix control socket 等
```

实践含义（与 [session 调研](./codex-session-rollout-resume.md) 一致）：

- 可恢复身份是 **Codex `thread.id`**，不是业务库自己的 session id。  
- CLI/SDK 一次性进程「文件到位即可 resume」；**Desktop/app-server 常驻**，导入 rollout / 改索引后更建议重启客户端或进程。  
- `thread/list` 侧边栏体验更依赖 sqlite / session index；只拷 jsonl 可能列表不完整。

---

## 11. 客户端嵌入模式

官方归纳的三类部署：

### 11.1 本地子进程 + stdio（最常见）

VS Code 扩展、macOS Desktop 等：捆绑平台二进制 → `spawn("codex", ["app-server"])` → 双向 stdio JSONL。

适合：本机 IDE、本机桌面应用。

### 11.2 版本解耦的合作方客户端

如 Xcode：客户端发布节奏独立，可指向较新的 App Server 二进制，吃到服务端改进而无需同步发客户端。

### 11.3 Web / 云端容器

Worker 起容器 → 容器内跑 App Server → 浏览器经后端 HTTP/SSE（或等价通道）消费事件。长任务状态以服务端为准，关标签页不丢任务。

### 11.4 何时不要用 App Server

- 只要「跑完退出」：用 `codex exec` / TS SDK。  
- 不需要流式 UI、审批、多 turn 持久会话：App Server 偏重。

---

## 12. 最小集成骨架（Node）

```ts
import { spawn } from "node:child_process";
import readline from "node:readline";

const proc = spawn("codex", ["app-server"], {
  stdio: ["pipe", "pipe", "inherit"],
});
const rl = readline.createInterface({ input: proc.stdout! });

const send = (message: unknown) => {
  proc.stdin!.write(`${JSON.stringify(message)}\n`);
};

let threadId: string | null = null;

rl.on("line", (line) => {
  const msg = JSON.parse(line);

  // 处理 server→client 请求（审批等）：看 msg.id && msg.method
  if (msg.id != null && msg.method) {
    // TODO: 弹 UI，再 send({ id: msg.id, result: { decision: "accept" } })
    return;
  }

  if (msg.id === 1 && msg.result?.thread?.id && !threadId) {
    threadId = msg.result.thread.id;
    send({
      method: "turn/start",
      id: 2,
      params: {
        threadId,
        input: [{ type: "text", text: "Summarize this repo." }],
      },
    });
  }

  if (msg.method === "turn/completed") {
    // 本轮结束
  }
});

send({
  method: "initialize",
  id: 0,
  params: {
    clientInfo: { name: "my_product", title: "My Product", version: "0.1.0" },
  },
});
send({ method: "initialized", params: {} });
send({ method: "thread/start", id: 1, params: { model: "gpt-5.1-codex" } });
```

Python 侧可参考官方实验包 `openai-codex-app-server-sdk`（stdio、高阶 `thread_start` / `run`）；缺的方法可降到底层 `request(...)`。

---

## 13. 与 MCP、ACP 的边界

| 协议 | 角色 |
|---|---|
| **App Server** | Codex harness 专用全保真客户端协议（会话、流式、审批、持久化） |
| **MCP** | Agent 挂外部工具 / 资源；也可把 Codex 当简易 MCP server（能力子集） |
| **ACP**（Agent Client Protocol 等） | 编辑器 ↔ 任意 coding agent 的更通用标准；与 App Server 可并存 |

选型建议：做 **Codex 深度产品集成** → App Server；做 **给 Codex 加工具** → MCP；做 **编辑器通用 agent 插槽** → 关注 ACP 生态。

---

## 14. 实践建议与踩坑

1. **先握手再干活**；忘记 `initialized` 会全线 `Not initialized`。  
2. **必须处理服务端请求**（审批），否则 turn 会一直挂起。  
3. **业务 session id ≠ thread.id**；自行建映射表。  
4. **升级 Codex 后重新 `generate-ts` / JSON Schema**。  
5. **`-32001` 可重试**；不要当致命逻辑错误。  
6. **WebSocket 默认非生产级**；远程务必鉴权 + TLS。  
7. **实验 API 默认关闭**；`experimentalApi: true` 接受破坏性变更。  
8. **常驻进程有缓存**：换机拷贝 rollout / sqlite 后，Desktop/app-server 建议退出再开。  
9. **CI 关掉 delta**：`optOutNotificationMethods` 抑制 `item/agentMessage/delta` 等。  
10. **沙箱与审批策略**在 `thread/start` 与 `turn/start` 都可覆盖；冷 resume 有「请求覆盖 → 持久设置 → 配置默认」的优先级。

---

## 15. 参考

- [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/unlocking-the-codex-harness/)（OpenAI 工程博文）  
- [Codex App Server 官方文档](https://developers.openai.com/codex/app-server)  
- `openai/codex` → [`codex-rs/app-server/README.md`](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)  
- InfoQ 综述：[OpenAI Publishes Codex App Server Architecture…](https://www.infoq.com/news/2026/02/opanai-codex-app-server/)  
- 本仓库：[codex-home.md](./codex-home.md)、[codex-session-rollout-resume.md](./codex-session-rollout-resume.md)

---

*文档整理自 2026-08 公开资料与上游 README；若升级 Codex 大版本，请以 `codex app-server generate-ts` 产物与官方 App Server 页为准复核方法名与字段。*
