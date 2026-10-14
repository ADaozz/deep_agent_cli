# HTTP 服务与前端接入

本文涵盖 HTTP 服务的安装配置、接口及业务行为，可作为聊天、工具审批和人工输入界面的开发契约。接口对应当前 `v0.1.5` 已实现的能力，复用 CLI 的执行逻辑，CLI 继续使用原来的启动方式。接口实现位于 [`agent/server/app.py`](../agent/server/app.py)，任务行为位于 [`agent/server/runtime.py`](../agent/server/runtime.py)。

## 1. 安装与启动

需要 Linux / WSL2、Python 3.12+ 和可用的 Bubblewrap。服务始终要求沙箱，不接受宿主执行回退。

```bash
pip install "deep-agent-cli[server]"
# 从源码安装：pip install -e ".[dev,server]"
```

先准备原有格式的模型配置，如 `~/.deep-agent/config.yaml`。创建服务器配置 `~/.deep-agent/service.yaml`：

```yaml
model_config: config.yaml
state_dir: server-state
# 从环境变量读取服务令牌，不写在 YAML 中。
token_env: DEEP_AGENT_SERVICE_TOKEN
max_runs: 2
projects:
  app: /srv/projects/app
  worker: /srv/projects/worker
```

路径相对于服务器配置文件解析，项目目录必须已经存在，项目目录之间不能重叠。模型配置、服务器配置、Agent 配置目录和服务状态目录必须位于所有项目工作区之外。服务状态目录应固定使用，项目 ID 与目录的映射在持久化会话存在期间应保持稳定。

```bash
export DEEP_AGENT_SERVICE_TOKEN='<设置一个随机服务令牌>'
deep-agent serve --config ~/.deep-agent/service.yaml --host 127.0.0.1 --port 8080
```

服务启动时检查模型配置和各项目沙箱。`sandbox.allow_unsandboxed` 在服务模式下强制关闭，权限固定使用 `ask`。

配置在启动时加载，修改模型或项目配置后需要重启服务。

第一版使用一个进程和本地 SQLite。状态目录的文件锁会拒绝第二个服务进程；请勿使用多个 Uvicorn worker，也不要为同一批项目启动使用不同状态目录的服务。调用方只提交项目 ID，不能传宿主工作区路径。

### MCP 工具环境

服务默认禁用 MCP。在服务 YAML 中设置 `mcp_config: /home/user/.deep-agent/mcp.yaml` 可加载配置启动快照，配置文件必须在工作区外。TUI 后续修改不会影响服务；重启才重新读取。没有 MCP 管理接口，远程工具沿用现有 `ask` 审批和 Cancel 接口。审批调用附带 `mcp.profile` 与脱敏 `mcp.url`，Bubblewrap 不隔离远程 Server。详见 [MCP 接入](mcp.md)。

### 运行与隔离边界

工作区文件工具仍可修改工作区内的 `.git`、`.env` 等文件，沙箱沿用现有项目的隔离边界。服务未增加 CPU、内存或磁盘配额。任务与事件暂不自动清理，部署时监控状态目录磁盘用量。

`network=false` 通过独立网络命名空间限制命令及子进程的直接 IP 网络访问，不承诺阻断所有间接通信。工作区或其他挂载路径中可见的 Unix socket 仍可能连接宿主进程或有网沙箱的服务，由它们代为联网；共享文件和 FIFO 也可能形成通信路径。这是当前隔离的已知边界，不是已修复的问题或沙箱逃逸。如果部署要求禁止一切间接联网，当前配置不满足该承诺，需要另行定义并验证更严格的隔离策略。

## 2. 接入范围

HTTP 服务复用 CLI 的 `AgentRunner`、模型调用、工具、审批、人工输入和 checkpoint。它是 CLI 执行能力的一个子集，前端应按以下范围实现。

| 前端业务 | 当前 HTTP 能力 | 实现方式 |
| --- | --- | --- |
| 选择项目 | 支持 | 查询服务器预配置的项目 ID |
| 新建会话、继续对话 | 支持 | 创建会话后，按顺序提交文字任务 |
| 展示回答、思考、工具执行、用量 | 支持 | SSE 事件；最终回答以任务结果为准 |
| 批量工具审批 | 支持 | 展示整批待审批调用，逐项选择后一次提交 |
| 补充回答、选择、确认、评审 | 支持 | 按 Agent 声明的字段生成表单 |
| 停止当前任务 | 支持 | 取消接口；关闭页面或 SSE 不会取消 |
| 继续已有暂停或配置边界中断 | 支持 | 对相应状态调用 continue |
| 服务重启后恢复中断会话 | 支持 | 在原会话创建 `resume=true` 的新任务 |
| 会话标题、搜索、删除、消息列表、任务列表 | 未提供 | 会话接口只有 ID、项目和创建时间；调用方保存展示记录和任务 ID |
| 模型/思考强度切换、权限模式切换 | 未提供 | 服务启动时读配置，默认模型固定，权限为 `ask` |
| 主动暂停、手动压缩、运行中追加/插入消息 | 未提供 | 不显示相应 HTTP 操作按钮 |
| 图片/附件上传、文件下载、剪贴板、技能选择器 | 未提供 | 创建任务仅接受文字，不把 CLI 的附件或选择器当作已有 HTTP 接口 |

向 `text` 发送 `/model`、`/compact` 等文字不会触发 CLI 斜杠命令分发。前端不能用这种方式模拟尚未暴露的操作。

### 推荐连接方式

```text
浏览器界面 → 现有系统后端/同源代理 → Deep Agent Service
                  登录和项目权限       服务 Bearer 令牌
```

服务令牌代表整个内部系统，不区分最终用户，也不判断会话属于哪个用户。令牌保留在业务后端；业务后端校验登录、项目权限，以及 session/run 的业务归属，然后转发请求。前端本地存储不是权限依据。

当前服务没有配置 CORS 或 Cookie 登录。原生 `EventSource` 不能设置此服务要求的 `Authorization` 请求头。建议通过同源业务后端代理 SSE，并由后端附加令牌；也可以在适合的受信任客户端中使用带 Bearer 请求头的流式 `fetch`。不要通过 URL 查询参数传令牌，服务也不支持这种鉴权。

## 3. 通用约定

示例直连地址为 `http://127.0.0.1:8080`。除健康检查外，所有接口需要：

```http
Authorization: Bearer <DEEP_AGENT_SERVICE_TOKEN>
Content-Type: application/json
```

`Content-Type` 适用于 JSON 请求体；GET 和无请求体的 cancel 不需要它。普通响应为 JSON，事件接口为 `text/event-stream`。

- 请求体只接受文档列出的字段；额外顶层字段和审批项额外字段返回 `422`。
- 路径 ID 从接口响应取得，不在前端生成 session/run/interrupt ID。`project_id` 从项目列表选择。
- 时间为带 UTC 偏移的 ISO 8601 字符串，如 `2026-10-13T02:00:00+00:00`；前端按用户时区显示。
- `201` 表示会话创建成功；`202` 表示动作已受理，不表示 Agent 已完成。
- 所有写请求均没有客户端幂等键。发送超时不表示服务器没有执行，不自动重发文字任务。
- 服务不自动排队。等待审批、等待人工输入和暂停都算占用项目和运行容量。
- 服务状态目录中的会话与 CLI 默认数据库分开，不把 CLI 会话 ID 直接当成服务会话。

### 数据结构

以下 TypeScript 类型描述响应字段。运行初期和交互受理后的 `result` 可以是 `{}`；某些失败只包含 `status/error`，不能假设其他结果字段一定存在。

```ts
type RunStatus =
  | 'running' | 'waiting_confirmation' | 'waiting_human'
  | 'paused' | 'runtime_config_boundary' | 'cancelling'
  | 'completed' | 'failed' | 'cancelled' | 'interrupted';

interface Session {
  id: string;
  project_id: string;
  created_at: string;
}

interface PendingTool {
  toolCallId: string;                 // 响应使用 camelCase
  name: string;
  args: Record<string, unknown>;
}

interface InputOption {
  value: string;
  label: string;
  description: string;
  allowsCustomText?: boolean;
}

interface InputField {
  id: string;
  type: 'text' | 'textarea' | 'single_select' | 'multi_select' | 'boolean';
  label: string;
  required: boolean;
  placeholder: string;
  options: InputOption[];
}

interface HumanInput {
  type: 'human_input';
  interactionType: 'clarification' | 'decision' | 'confirmation' | 'review';
  title: string;
  reason: string;
  question: string;
  fields: InputField[];
  blocking: boolean;
  impact: string[];
  interactionId?: string;             // Agent 声明的标识，不用于 HTTP 交互校验
  recommendation?: { value: string; reason: string };
}

interface RunResult {
  status: RunStatus;
  output: string;
  pending_tool_calls: PendingTool[];
  human_input: HumanInput | Record<string, never>;
  error: string;
}

interface Run {
  id: string;
  session_id: string;
  project_id: string;
  status: RunStatus;                  // 任务状态的权威字段
  created_at: string;
  updated_at: string;
  result: Partial<RunResult>;
  interrupt_id: string | null;        // 服务生成的交互标识
}
```

`GET /v1/sessions/{id}` 只返回 `Session`，不会包含聊天记录、模型、标题、运行状态或最近的 run ID。业务系统需保存 session 与用户的关联，以及每次提交的文字、run ID、事件记录/游标。已有会话的聊天展示从业务系统记录恢复，不能从这个会话接口还原完整聊天。

### 任务状态与页面行为

| `status` | 业务含义 | 推荐页面操作 |
| --- | --- | --- |
| `running` | worker 正在执行或即将开始 | 展示进度；允许取消；禁用同项目新任务提交 |
| `waiting_confirmation` | 工具尚待用户决定 | 展示整批工具审批；允许取消 |
| `waiting_human` | Agent 需要补充信息 | 展示声明字段表单；允许取消 |
| `paused` | checkpoint 已暂停 | 显示继续/取消；当前 API 没有主动暂停入口 |
| `runtime_config_boundary` | 等待恢复已有配置边界 | 显示继续/取消；不显示模型配置编辑器 |
| `cancelling` | 取消已请求，worker 尚未完成清理 | 显示“正在停止”；继续查询；保持新任务提交禁用 |
| `completed` | 本次任务正常结束 | 展示 `result.output`，可以提交下一条文字 |
| `failed` | 执行或清理失败 | 展示 `result.error`；按错误和 checkpoint 情况决定后续动作 |
| `cancelled` | 取消结束 | 保留已经显示的输出；取消不回滚已执行的文件修改 |
| `interrupted` | 服务重启前任务未结束 | 保留旧记录；检查工作区，按恢复流程创建新任务 |

只有 `completed/failed/cancelled/interrupted` 是终态。业务上所有等待状态都不是任务结束。`failed` 且错误为 `Runner cleanup failed; restart service` 时，服务仍保留该项目占用，需要运维重启，不能立即再次写入。

正常交互路径：`running → waiting_* → running → completed`，可重复多次。受理交互会清空当前 `result` 和 `interrupt_id`，因此界面必须在 POST 前保存当前交互 ID；后续只使用新的任务快照更新页面。

## 4. 接口明细

### 4.1 健康与契约

| 方法与路径 | 成功响应 | 业务行为 |
| --- | --- | --- |
| `GET /health` | `200 {"status":"ok"}` | 不需要令牌，仅代表 HTTP 进程可响应；不保证模型端点可用 |
| `GET /openapi.json` | `200` OpenAPI JSON | 需要令牌；供后端查看请求约束，当前未开放 Swagger/Redoc 页面 |

OpenAPI 的请求模型由 Pydantic 声明；多数响应没有绑定专用 response model，生成的 OpenAPI 不会完整描述本文的响应类型。前端不能仅根据自动生成的响应类型推断业务状态。

### 4.2 项目列表

```http
GET /v1/projects
```

```json
{
  "projects": [
    { "id": "app" },
    { "id": "worker" }
  ]
}
```

返回 `200`。列表来自服务启动配置，只返回 ID，不返回宿主目录、模型配置或凭据。无创建、删除或修改项目的接口。前端可用业务系统维护的名称映射显示友好项目名；不要让用户提交任意工作区路径。

### 4.3 创建与查询会话

**新建会话**：`POST /v1/sessions`。

```json
{ "project_id": "app" }
```

返回 `201`：

```json
{
  "id": "e2483b2a-5b7f-458b-8024-006e98e27673",
  "project_id": "app",
  "created_at": "2026-10-13T02:00:00+00:00"
}
```

仅创建服务会话目录项；不调用模型，不占用运行容量，也不创建一个已完成的 Agent 任务。项目不存在返回 `404`。会话项目不能更改。

**列表**：`GET /v1/sessions` 或 `GET /v1/sessions?project_id=app`。

返回 `200 {"sessions":[Session, ...]}`。按创建时间倒序，最多 100 条，没有分页参数；不按最后聊天时间排序。过滤条件不存在返回 `404`，只返回目前配置中仍存在的项目会话。页面不能据此判断项目是否空闲。

**详情**：`GET /v1/sessions/{session_id}`，返回 `200 Session`。会话不存在，或其项目已经移出当前配置，返回 `404`。

### 4.4 提交文字任务 / 恢复中断

```http
POST /v1/sessions/{session_id}/runs
```

两种请求互斥：

```json
{ "text": "检查项目中的测试失败原因，并给出修复建议。" }
```

```json
{ "resume": true }
```

| 字段 | 类型与默认值 | 约束 |
| --- | --- | --- |
| `text` | string 或 null；默认 null | 提交新输入时必填，长度 1–100000 个字符，不能全部为空白 |
| `resume` | boolean；默认 false | true 时不能同时提供非 null 的 text；用于恢复 checkpoint 中已有中断 |

不要传 `model`、`workspace`、`permission`、`attachments` 等额外字段。`{}`、`{"text":" "}`、`{"text":"你好","resume":true}` 返回 `422`。`{"text":null,"resume":true}` 与只传 resume 等效。

返回 `202 Run`，典型受理快照为：

```json
{
  "id": "4e362c75-6d43-4e48-a00e-998af74b622b",
  "session_id": "e2483b2a-5b7f-458b-8024-006e98e27673",
  "project_id": "app",
  "status": "running",
  "created_at": "2026-10-13T02:01:00+00:00",
  "updated_at": "2026-10-13T02:01:00+00:00",
  "result": {},
  "interrupt_id": null
}
```

收到后立即保存 run ID 并订阅事件。worker 可能很快结束，不把 POST 返回快照当成后续持续状态。

**业务规则**：

- 同一项目只允许一个未结束任务，跨会话也互斥；占用时返回 `409 Project already has an unfinished run`。
- 不同项目可以并行，达到 `max_runs` 返回 `429 Run capacity reached`；默认容量为 2。
- 服务关闭期间提交返回 `503 Service is shutting down`。项目占用检查先于总容量检查。
- 新任务继续使用此会话的 checkpoint；每个任务结束后关闭 Runner 和沙箱，`/tmp`、后台进程不跨 HTTP 任务保留。工作区文件修改会保留。
- `resume=true` 不是重新发送旧文字，也不是重跑已完成任务。worker 读取现有中断，进入等待交互，再由前端提交决定。
- 没有可恢复中断时，resume 请求仍可能先返回 `202`，随后任务变为 `failed`。同样，在已有中断上直接提交文字会先受理，随后以 `Session is interrupted; submit resume=true` 失败。这些不是同步的 HTTP `409`。

### 4.5 查询任务

```http
GET /v1/runs/{run_id}
```

返回 `200 Run`；不存在或项目不再可访问时返回 `404`。轮询和 SSE 最终查询都使用此接口。

完成结果示例（外层 Run 字段省略）：

```json
{
  "status": "completed",
  "interrupt_id": null,
  "result": {
    "status": "completed",
    "output": "已检查测试失败原因，建议修复配置读取路径。",
    "pending_tool_calls": [],
    "human_input": {},
    "error": ""
  }
}
```

`output` 是当前任务最终回答，不是整个会话历史；工具调用、思考过程和中间回答从事件取得。服务存储会替换已知服务令牌/模型凭据字符串，但前端仍应把模型输出、工具参数和日志当作非可信内容进行安全渲染。

### 4.6 工具审批

前提为最新 `Run.status === 'waiting_confirmation'`。待审批列表位于 `result.pending_tool_calls`：

```json
{
  "status": "waiting_confirmation",
  "interrupt_id": "13be37b8-2852-49e0-b43a-c338d7dfd1a3",
  "result": {
    "status": "waiting_confirmation",
    "output": "",
    "pending_tool_calls": [
      {
        "toolCallId": "call-write",
        "name": "write_file",
        "args": { "file_path": "/workspace/notes.md", "content": "测试检查记录" }
      },
      {
        "toolCallId": "call-shell",
        "name": "execute",
        "args": { "command": "npm test", "network": false }
      }
    ],
    "human_input": {},
    "error": ""
  }
}
```

服务沿用 CLI 的 ask 策略：execute、web_search，以及写入、编辑、删除文件需要审批；普通只读文件工具不会因为 ask 就全部弹窗。展示工具名称、参数、文件路径，以及 execute 的命令和网络权限；`network=true` 可访问宿主网络，包括 localhost 和局域网。工具审批参数来自 Agent，审批接口不能替换参数或编辑命令。

```http
POST /v1/runs/{run_id}/approvals
```

```json
{
  "interrupt_id": "13be37b8-2852-49e0-b43a-c338d7dfd1a3",
  "decisions": [
    { "tool_call_id": "call-write", "decision": "approve" },
    {
      "tool_call_id": "call-shell",
      "decision": "reject",
      "message": "先给出检查结论，暂不运行测试。"
    }
  ]
}
```

| 字段 | 规则 |
| --- | --- |
| `interrupt_id` | 必填，使用最新等待快照的服务交互 ID |
| `decisions` | 非空数组，覆盖本批所有调用，每个调用恰好一次 |
| `tool_call_id` | 对应响应中的 `toolCallId`；请求使用 snake_case |
| `decision` | 仅 `approve` 或 `reject` |
| `message` | 可选 string/null；拒绝理由交给 Agent，approve 时不改变工具参数 |

返回 `202 Run`。审批不会同步执行工具，也不保证这次运行最终成功。服务先持久化 `interaction_submitted` 事件，再唤醒 worker。

遗漏、重复、包含不属于当前批次的调用，或交互已过期，返回 `409`；格式/枚举错误返回 `422`。前端逐项收集决定再一次提交，默认选择“拒绝”或未选择，避免隐式批准。重复点击通过按钮禁用控制，遇到 `409` 重新查询，不盲目重发旧批次。

`agent_event.type === 'tool_started'` 表示图中产生了工具调用，可能尚未获批，不能据此显示“已执行成功”。真正结果以 `tool_completed.is_error` 和结构化 artifact 为准。

### 4.7 人工输入

前提为 `waiting_human`。问题在 `result.human_input`，例如：

```json
{
  "type": "human_input",
  "interactionType": "clarification",
  "title": "确认修改范围",
  "reason": "需要限定本次修改目录。",
  "question": "允许修改哪些目录？",
  "fields": [
    {
      "id": "scope",
      "type": "multi_select",
      "label": "修改范围",
      "required": true,
      "placeholder": "",
      "options": [
        { "value": "tests", "label": "测试目录", "description": "" },
        { "value": "src", "label": "源代码", "description": "" },
        { "value": "__other__", "label": "Other:", "description": "", "allowsCustomText": true }
      ]
    },
    {
      "id": "confirmed",
      "type": "boolean",
      "label": "是否已经备份",
      "required": true,
      "placeholder": "",
      "options": []
    }
  ],
  "blocking": true,
  "impact": ["可能修改选定目录的文件。"]
}
```

| 字段类型 | 页面控件 | `values[field.id]` |
| --- | --- | --- |
| `text` | 单行输入 | string |
| `textarea` | 多行输入 | string |
| `single_select` | 单选 | 选项 value 字符串；自定义项用用户实际输入文本 |
| `multi_select` | 多选 | value 字符串数组；自定义文本作为数组成员 |
| `boolean` | 是/否或明确确认控件 | boolean，`false` 是有效回答 |

`interactionType` 描述问题意图，不替代字段定义；`recommendation` 是建议，不表示用户已作决定。自定义选项按 `allowsCustomText` 识别，不能硬编码 `__other__`，其标识可能为了避免冲突而改变。自定义答案提交实际文字，不提交 Other 标记或额外的 schema。

```http
POST /v1/runs/{run_id}/human-input
```

```json
{
  "interrupt_id": "当前 Run 的 interrupt_id",
  "values": { "scope": ["tests", "docs"], "confirmed": false }
}
```

返回 `202 Run`。`values` 必须是 JSON 对象，字段名对应当前表单 id；未声明字段时标准 Runner 会提供必填 `text`，可提交 `{"values":{"text":"我的回复"},"interrupt_id":"..."}`。

服务以当前中断的 fields 检查必填字段：缺失、null、空字符串、纯空白字符串、空数组均返回 `422`。此时不入队、不产生提交事件，等待状态、interrupt ID 和 checkpoint 不变，可保留表单更正后重试。可选字段允许省略或留空，必填 boolean 的 false 有效。

当前服务只校验对象和必填非空，不完整校验每种字段的数据类型、枚举成员或 values 的额外键。前端按上表限制类型和选项；这项客户端校验不能替代未来需要的服务端校验。不要把 Agent 给出的字段当成 HTML 或脚本执行。

HTTP 受理后若 Runner 再次拒绝回答，会发出 `interaction_rejected`，随后重新进入等待并生成新的服务 interrupt ID。保留填写内容，但以新的 `interaction_requested` 或 GET 快照重建提交目标。

### 4.8 继续已有暂停

```http
POST /v1/runs/{run_id}/continue
```

```json
{ "interrupt_id": "当前 Run 的 interrupt_id" }
```

仅用于 `paused` 或 `runtime_config_boundary`，返回 `202 Run`。对 `waiting_human`、`waiting_confirmation`、运行中或终态调用，返回 `409`；不能用它跳过审批或必填输入。当前 API 没有请求暂停或更改模型配置的端点，前端仅在实际收到相应状态时提供继续按钮。

### 4.9 取消任务

```http
POST /v1/runs/{run_id}/cancel
```

无请求体，返回 `202 Run`，一般为 `cancelling`。运行中、等待审批、等待回答和暂停的活动任务都可取消。服务请求 Runner 停止，解除等待，并等待 worker 清理；后续通常到达 `cancelled`，清理失败可能变为 `failed`。

终态只裁决一次，以 Runner 实际到达的执行边界为准：

- 本轮已经结束（最终回答已写入 checkpoint）后才生效的取消不覆盖结果，`cancelling` 之后仍可能到达 `completed`。
- 取消在本轮结束前生效时到达 `cancelled`。此前已完成的工具调用（包括文件写入）保留，其结果已记录在 checkpoint 中，下一轮可见。
- 取消已生效但无法确认模型请求已关闭时到达 `failed`，`result.error` 说明原因；若 worker 清理时仍无法确认，则按 `Runner cleanup failed; restart service` 保留项目占用。
- 等待审批时取消、且审批尚未被处理，工具不执行，审批中断保留，该会话需用 `resume=true` 恢复。

已终止或无活动 worker 返回 `409 Run has already ended`。取消处理中重复请求可能再次返回 `202`，前端只发送一次并禁用按钮。断开事件流、离开页面、AbortController 中止浏览器请求，都不是服务取消操作。取消不回滚工作区修改，也不撤销已经完成的外部副作用。

## 5. SSE 事件协议

```http
GET /v1/runs/{run_id}/events?after=0
Accept: text/event-stream
Authorization: Bearer <服务令牌>
```

成功返回 `200`，响应含 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。部署代理也需关闭响应缓冲和流压缩缓冲，不能先把整个流收集完再交给浏览器。

### 帧与续接

```text
id: 101
event: delta
data: {"kind":"assistant","text":"正在检查测试"}

id: 103
event: delta
data: {"kind":"assistant","text":"正在检查测试失败原因。"}

: keepalive

```

事件 ID 在整个服务数据库中递增，所以同一个 run 的 ID 不连续是正常现象。前端把 ID 当作游标字符串保存，并按 run 隔离游标和展示记录。

- 从头读取：`after=0`，默认值也是 0。
- 续接：`?after=<最后已处理ID>`，或 `Last-Event-ID: <ID>`。均为非负整数；请求头优先于 query。
- 只返回严格大于游标的事件。游标应在本地处理/保存该事件成功后推进；不要使用其他 run 的游标。
- 非法 `Last-Event-ID` 返回 `422`。`after` 负数/非整数同样返回 `422`。
- 每约 15 秒发送注释心跳 `: keepalive`，心跳没有 ID，不改变业务状态。
- 终态任务的存量事件发完后正常关闭；即使先收到 `run_finished`，也允许读到 EOF。
- 对终态任务使用已经读完的游标，接口正常返回空流。不能把 EOF 一概当作任务失败。
- 网络断开后重新 GET 任务；仍非终态时从已处理 ID 续接。重放可能重复送到应用，按 ID 去重。
- 只存游标而丢失此前 UI 状态时，从 0 重放以恢复展示。没有单独的消息/工具历史查询接口。

### 服务级事件

| `event` | `data` | 前端行为 |
| --- | --- | --- |
| `run_started` | Run 受理快照 | 建立当前任务记录；不表示已调用模型 |
| `delta` | `{kind: 'assistant'|'reasoning', text: string}` | 替换当前模型调用的回答/思考文本，不能逐帧追加 |
| `agent_event` | 下节的 RunEvent | 展示回答块、工具、输出、用量等 |
| `interaction_requested` | Run 等待快照，含服务 interrupt_id | 以这个事件/GET 快照打开审批或表单 |
| `interaction_submitted` | `{interrupt_id: string}` | 本轮交互已入队；禁用旧表单，等待新状态 |
| `interaction_rejected` | `{error: 'Invalid interaction response'}` | 显示提交失败；随后按新快照更新交互 ID |
| `run_finished` | Run 终态快照 | 关闭交互、同步最终状态及回答，释放页面提交限制 |

**流式文本是累计快照**。上述两帧的正确结果是“正在检查测试失败原因。”，不是“正在检查测试正在检查测试失败原因。”。一个任务可能含多个模型调用；`agent_event.assistant_started` 用于创建新回答块，并重置这个调用的思考/回答缓存。

同一回答既可能通过 `delta` 发送，也可能通过 `agent_event.assistant_delta/thinking_delta` 发送。前端选一条文本数据通道处理，避免重复展示；推荐使用 agent_event 管理块边界，delta 填充当前块。`assistant_completed` 可补齐完整文本。

Runner 的 `agent_event` 也可能含 `interaction_requested` 或 `run_completed`。服务级 `interaction_requested` 才有用于 HTTP 提交的 interrupt ID，服务级 `run_finished` 才表示任务和清理已经结束；不要拿原始 Runner 事件提前结束任务。

### Agent 事件

每个 `agent_event.data` 为下列结构，字段无内容时仍可能出现空字符串、空对象或 null：

```ts
interface AgentEvent {
  type: string;
  content: string;
  message_id: string;
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  result: unknown;
  is_error: boolean;
  stream: string;
  artifact: unknown;
}
```

| `data.type` | 主要载荷与处理 |
| --- | --- |
| `assistant_started` | 新模型调用开始，创建新回答块；与服务 run_started 不同 |
| `assistant_delta` / `thinking_delta` | `content` 为本调用累计回答/思考；若已处理 delta，不重复填充 |
| `assistant_completed` | `content` 为回答，`result.thinking` 为思考文本 |
| `tool_started` | `tool_call_id/name/arguments`，创建待执行工具卡片；可能仍需审批 |
| `tool_output_delta` | `tool_call_id/content/stream`；普通 merged 输出按增量追加，tail_snapshot 替换当前有界输出并标记截断 |
| `tool_completed` | `content/result/is_error/artifact`，结束对应工具卡片；不把 artifact 任意字符串当下载地址 |
| `usage` | `result` 可能含 input_tokens/output_tokens/total_tokens，也可能为 {}；不是会话累计费用 |
| `todos_updated` | `result` 为任务项数组，替换当前待办展示 |
| `compaction_started` / `compaction_finished` | 自动压缩进度；不表示当前 run 已结束 |
| `turn_completed` | `content` 为该轮回答，`result` 含 elapsed_seconds/finished_at；可显示耗时 |
| 其他事件 | 忽略不识别的展示事件，保留原始记录；任务状态仍从服务快照读取 |

工具卡片按 `tool_call_id` 关联，回答块按调用顺序组织；不要假设每个事件都有非空 message_id。execute 的 artifact 可能包含 `exit_code`、`truncated`、`termination_reason`、`host_log_path`、`agent_log_path`、`log_error`、`max_output_bytes`。工具失败以 `is_error` 和 exit_code 判断，不能因为 Agent 最终说“完成”就认为命令成功。日志路径是工作区路径提示，当前没有通过 HTTP 下载日志的接口。

长命令输出超限后，stream 为 `tail_snapshot` 的事件携带最新尾部快照，必须替换而不是追加，否则会重复展示且持续增长内存。最终 tool_completed 的 content/结构化结果用于对齐工具结束状态；界面可保持尾部预览和日志路径提示，不承诺浏览器持有完整命令输出。

`usage` 展示可沿用 CLI：只在出现有效非负整数 input_tokens 时更新读数，空 usage 保留上次读数；没有读数时显示 Unknown。百分比还需已知当前模型的 context_window，现有 HTTP API 不提供模型目录或窗口值，业务系统没有可靠配置时仅展示输入 token 数，不猜窗口或价格。

## 6. 错误与重试

业务/鉴权错误的 JSON 示例：

```json
{ "detail": "Interaction is no longer pending" }
```

FastAPI/Pydantic 的参数校验错误使用数组型 detail，例如：

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "text"],
      "msg": "String should have at least 1 character",
      "input": "",
      "ctx": { "min_length": 1 }
    }
  ]
}
```

不要假设所有 `422.detail` 都是字符串，也不要按英文报错文本做唯一业务判断。以 HTTP 状态、请求动作及最新 Run 状态驱动界面，英文 detail 作为诊断信息保留。

| 状态码 | 触发场景 | 页面处理 |
| --- | --- | --- |
| `401` | 无令牌、错误令牌或鉴权类型不正确 | 由业务后端检查服务配置；不要让用户输入服务令牌 |
| `404` | 未知 project/session/run，或项目已移出配置 | 停止当前订阅，提示资源不可用并刷新业务列表 |
| `409` | 同项目占用；旧交互、错误交互状态；审批列表不完整；终态取消 | 占用时保留用户文字；交互冲突时 GET 最新任务再更新界面 |
| `422` | 请求字段、长度、枚举、游标或必填回答不符合约束 | 保留输入，展示对应字段错误，更正后重新提交 |
| `429` | 活动任务达到服务 max_runs | 保留输入，等待用户重试或在业务后端安排提交；服务自身不排队 |
| `503` | 服务正在关闭时提交新任务 | 提示稍后重试；先查询已知任务，避免重复创建 |
| `5xx` / 网络错误 | 服务/代理异常 | 先核对已知 run；未知是否受理的 POST 进入“不确定”状态 |

若创建任务的 POST 在响应前断线，前端可能没有 run ID，而当前服务又没有任务列表或幂等键。业务后端应保存获得的响应并提供自己的操作记录；若连后端也无法确认是否受理，提示用户检查任务/工作区，由运维核对服务记录，不能自动提交同一修改任务。这里描述的是调用方策略，不代表 DeepAgent 已提供相应查询端点。

## 7. 重启与恢复业务流程

正常关闭会请求取消并等待 worker 清理。清理失败时保留项目占用，拒绝新写入，需重启服务。

1. 浏览器刷新：从业务系统取得 session/run ID 和已有展示记录，GET 当前任务，从游标续接；若展示记录丢失，从 after=0 重放该任务事件。
2. 服务重启：服务将此前未终止的任务标记为 interrupted，并追加 run_finished。旧 run 不会重新执行，旧 interrupt_id 不能再提交。
3. 用户确认继续后，在原 session POST `{"resume":true}`，获得新的 run ID，重新订阅其事件。
4. 新 run 读取 checkpoint 中的中断，重新发出等待快照和新的 interrupt_id，前端据此生成审批/回答/继续控件。
5. 若 checkpoint 没有中断，resume 的 worker 会失败；按任务错误和实际记录判断能否提交新的 text。不能承诺取消/报错后总能 resume。

示例序列：

```text
旧 run: running → waiting_confirmation → 服务重启 → interrupted
原 session POST {"resume":true}
新 run: running → waiting_confirmation（新的 interrupt_id）
用户提交 approvals → running → completed
```

恢复不保证工具恰好执行一次。取消、崩溃或请求超时后，之前的文件修改和外部副作用可能已经发生，恢复前检查工作区。服务的项目互斥也不约束 CLI 或其他程序，对同目录的写操作由业务系统协调。

## 8. 调用示例

### Python 后端调用

以下 Python 示例可作为现有系统后端的接入参考，需要 `httpx`。`session_id` 和 `run_id` 都应由业务系统保存。

```python
import os
import httpx

client = httpx.Client(
    base_url="http://127.0.0.1:8080",
    headers={"Authorization": f"Bearer {os.environ['DEEP_AGENT_SERVICE_TOKEN']}"},
    timeout=30,
)

response = client.post("/v1/sessions", json={"project_id": "app"})
response.raise_for_status()
session_id = response.json()["id"]

response = client.post(
    f"/v1/sessions/{session_id}/runs",
    json={"text": "检查项目中的测试失败原因，并修复相关代码。"},
)
response.raise_for_status()
run_id = response.json()["id"]

# 在独立连接中订阅进度；审批请求由业务系统界面另行提交。
with client.stream("GET", f"/v1/runs/{run_id}/events", timeout=None) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        print(line)

response = client.get(f"/v1/runs/{run_id}")
response.raise_for_status()
print(response.json()["result"])
client.close()
```

### TypeScript 前端接入

下面代码使用同源业务后端代理 `/api/deep-agent`。**这个前缀是调用方自建的代理路径，不是 DeepAgent 新增接口**；代理将后续路径原样转发至服务，并加入服务 Bearer 令牌。示例不包含用户登录、项目权限或业务记录存储，这些由已有系统完成。

#### JSON 请求

```ts
const base = '/api/deep-agent';

class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
}

async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(base + path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, data.detail ?? data);
  return data as T;
}

const projects = () => api<{ projects: { id: string }[] }>('/v1/projects');
const createSession = (project_id: string) =>
  api<Session>('/v1/sessions', 'POST', { project_id });
const listSessions = (project_id?: string) =>
  api<{ sessions: Session[] }>('/v1/sessions' +
    (project_id ? `?project_id=${encodeURIComponent(project_id)}` : ''));
const getSession = (id: string) => api<Session>(`/v1/sessions/${encodeURIComponent(id)}`);
const sendText = (sid: string, text: string) =>
  api<Run>(`/v1/sessions/${encodeURIComponent(sid)}/runs`, 'POST', { text });
const resume = (sid: string) =>
  api<Run>(`/v1/sessions/${encodeURIComponent(sid)}/runs`, 'POST', { resume: true });
const getRun = (rid: string) => api<Run>(`/v1/runs/${encodeURIComponent(rid)}`);
const cancel = (rid: string) => api<Run>(`/v1/runs/${encodeURIComponent(rid)}/cancel`, 'POST');
const continueRun = (rid: string, interrupt_id: string) =>
  api<Run>(`/v1/runs/${encodeURIComponent(rid)}/continue`, 'POST', { interrupt_id });
const submitHuman = (rid: string, interrupt_id: string, values: Record<string, unknown>) =>
  api<Run>(`/v1/runs/${encodeURIComponent(rid)}/human-input`, 'POST', { interrupt_id, values });
const submitApprovals = (rid: string, interrupt_id: string,
  decisions: { tool_call_id: string; decision: 'approve' | 'reject'; message?: string }[]) =>
  api<Run>(`/v1/runs/${encodeURIComponent(rid)}/approvals`, 'POST', { interrupt_id, decisions });
```

#### Fetch SSE 读取器

TCP/HTTP 分块不等于 SSE 帧；可能一块包含多个事件，也可能一个中文字符或一个 JSON 跨块。以下实现使用增量解码、缓冲到完整空行分隔帧，并忽略心跳。

```ts
interface SseMessage { id: string; event: string; data: unknown }

function parseFrame(frame: string): SseMessage | null {
  let id = '', event = 'message';
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const key = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (key === 'id') id = value;
    else if (key === 'event') event = value;
    else if (key === 'data') data.push(value);
  }
  if (data.length === 0) return null;
  if (!/^\d+$/.test(id)) throw new Error('Invalid event ID');
  return { id, event, data: JSON.parse(data.join('\n')) };
}

async function* events(rid: string, after = '0', signal?: AbortSignal): AsyncGenerator<SseMessage> {
  const response = await fetch(
    `${base}/v1/runs/${encodeURIComponent(rid)}/events?after=${encodeURIComponent(after)}`,
    { headers: { Accept: 'text/event-stream' }, signal },
  );
  if (!response.ok) {
    const error = await response.json();
    throw new ApiError(response.status, error.detail ?? error);
  }
  if (!response.body) throw new Error('No event stream');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let separator: RegExpExecArray | null;
      while ((separator = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        const message = parseFrame(frame);
        if (message) yield message;
      }
      if (done) return;               // 不处理未完整结束的尾帧，续接时重新取
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
```

使用时，业务系统为每个 run 保留独立的 `cursor`，并提供自己的 `persistAndApply`：

```ts
let cursor = '0';                    // 恢复页面时从业务记录读取
const controller = new AbortController();

for await (const message of events(run.id, cursor, controller.signal)) {
  if (BigInt(message.id) <= BigInt(cursor)) continue;
  await persistAndApply(run.id, message); // 先持久化并应用，再保存游标
  cursor = message.id;
  await saveCursor(run.id, cursor);
}

const latest = await getRun(run.id);  // EOF/断线后的状态以 GET 为准
// 仍非终态时，从已保存 cursor 重连；异常时保留记录并退避，不取消服务任务。
```

`persistAndApply`、`saveCursor`、run 的加载和重试循环由业务系统实现；这段是 transport 示例，不是直接可用的完整聊天组件。处理服务级 interaction_requested 时替换当前 Run，处理 agent_event.assistant_started 时创建回答块，处理 delta 时替换当前块文本，处理 run_finished 时应用完整终态快照。

#### 页面按钮规则

- “发送”：无本页活动任务时可点击，但同项目可能在其他页面忙碌，仍需处理服务 `409/429`。
- “提交审批/回答/继续”：状态匹配且有最新 interrupt_id 才可点击；请求中禁用，`202` 后关闭旧交互。
- “停止”：活动 run 且未进入 cancelling 时可点击；等 run_finished/GET 终态后恢复发送。
- 关闭审批面板仅隐藏 UI，可从当前 Run 快照重开；不是拒绝、取消或释放项目。
- 在 UI 中区分“任务运行”“本地重连”“正在停止”“等待用户”；网络断线不要伪造 cancelled/failed。

## 9. 联调验收清单

| 场景 | 应观察到的业务结果 |
| --- | --- |
| 无令牌查询项目 | 401；健康检查仍可正常访问 |
| 空文字/未知请求字段 | 422；不创建任务 |
| 普通回答 | 202 后读取 SSE；累计文本不重复；run_finished/GET completed 后可发下一条 |
| 同项目不同会话并发 | 第二次提交 409；不同项目在容量内可以并行 |
| 容量满 | 429；用户输入保留，不显示“已排队” |
| 同批 approve/reject 混合 | 完整提交被受理，工具按各自决定处理 |
| 审批遗漏/重复/旧 interrupt ID | 409；仍用最新 Run 驱动界面 |
| 必填回答为空、false 有效 | 空值 422 且交互 ID 不变；false 可受理 |
| Runner 后续拒绝交互 | interaction_rejected 后显示新等待快照，使用新 interrupt ID |
| 等待审批时断开 SSE | 任务保持等待；续接能重放交互，断开不取消 |
| run_finished 后事件连接关闭 | 正常 EOF；不重复重连已经完成的 run |
| 取消 | 202/cancelling 后等待终态；已经修改的文件不回滚 |
| 服务重启恢复 | 旧 run interrupted；resume 创建新 run 和新 interrupt ID |
| 工具执行失败、Agent 回答正常 | 工具卡片仍按 is_error/exit_code 显示失败，不被最终回答覆盖 |

现有自动验证见 [`tests/test_server.py`](../tests/test_server.py)、[`tests/test_server_terminal.py`](../tests/test_server_terminal.py)、[`tests/test_server_cli.py`](../tests/test_server_cli.py)。模型实时调用和浏览器窗口显示不由这些模拟接口测试替代。

## 后续任务：长输出持久化放大

`OUTPUT-PERSIST-01`（P3）：调查长输出事件/任务快照重复持久化及 SQLite 增长，评估输出存储、事件保留和清理策略。用户真机报告记录数据库从 4.25 MB 增至 15.7 MB；本轮未独立复现该体积测量。验收应对固定大小输出测量事件数、写入量和数据库增长，并确认 SSE 重放、完整日志和恢复行为保持正确。作为独立后续任务记录，本轮不修改持久化协议或扩大第一版服务范围。
