# 架构

## 分层

```text
agent/cli/             终端界面：输入、渲染、按键、审批与提问面板
   │  RunEvent（不含终端格式）
   ▼
agent/runner.py        AgentRunner：一次运行的生命周期、中断与恢复、模型/权限切换
agent/session_runtime.py  会话租约、新会话延迟持久化、会话切换
   │
   ▼
agent/factory.py       AgentSpec → create_deep_agent()，装配工具与中间件
   ├── agent/llm.py        模型客户端（Chat Completions / Responses）
   ├── agent/tools/        execute、request_human_input、web_search
   ├── agent/middleware/   暂停、steering、取消、恢复说明、附件等
   ├── agent/sandbox.py    Bubblewrap / 宿主执行 backend 与路径路由
   ├── agent/sandbox_pool.py      按网络模式懒启动、复用的持久沙箱
   └── agent/sandbox_worker.py    沙箱内的常驻命令 Worker（仅标准库）
   │
   ▼
agent/session.py       SQLite：LangGraph checkpoint + session_catalog
agent/session_lock.py  每个会话一个 flock 锁
```

`agent/cli/` 只依赖 `AgentRunner` 的公开方法和 `RunEvent`。Runtime 不依赖终端，可以接到其他前端，见 [library.md](library.md)。

## 构图

`factory.build_agent()` 是唯一的装配入口，调用 Deep Agents 的 `create_deep_agent()`，不修改上游代码。

传入的工具：

- `request_human_input`（始终注册）
- `execute`（backend 支持命令执行时注册；`allow` 模式下使用默认开网的版本）
- `web_search`（有 Tavily 密钥时注册）
- 调用方通过 `extra_tools` 传入的工具

文件工具（`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`delete`）由 Deep Agents 的 `FilesystemMiddleware` 提供，本项目用 `WorkspaceFilesystemMiddleware` 替换它，以便在组合 backend 下仍然暴露 `execute`。`write_todos` 由 LangChain 的 `TodoListMiddleware` 提供。

Deep Agents 默认的 general-purpose 子 Agent 被禁用（通过 harness profile），因此没有 `task` 工具。

附加到 Deep Agents 默认中间件栈的中间件（按传入顺序）：

| 中间件 | 作用 |
|---|---|
| `RuntimeConfigGateMiddleware` | 有排队的模型 / 权限切换时，在模型调用前中断，切换后继续 |
| `PauseGateMiddleware` | `/pause` 请求后在模型调用前中断 |
| `RecoveryContextMiddleware` | 恢复异常结束的会话后，为第一次模型请求附加状态说明（不写入 checkpoint） |
| `SteeringMiddleware` | 注入 steering；模型返回时若有 steering，丢弃尚未开始的工具调用并重新推理 |
| `ToolCancelMiddleware` | 把取消信号传递给正在运行的工具 |
| `ToolArgHintMiddleware` | 工具参数校验失败时返回可操作的提示 |
| `WriteOperationMiddleware` | 在 `write_file` 结果中标记是新建还是覆盖 |
| `AttachmentMaterializationMiddleware` | 请求模型时把图片引用替换为实际内容 |
| `TodoListMiddleware` | `write_todos` |
| `WorkspaceFilesystemMiddleware` | 文件工具 |
| `SummarizationToolMiddleware` | `/compact` 使用的 `compact_conversation` |

审批由 Deep Agents 的 `interrupt_on` 实现；`ask` 模式的规则见 `agent/permission.py`。

## Backend 与路径

`WorkspaceCompositeBackend` 把 Agent 看到的路径路由到不同 backend：

- `/workspace/` → `BubblewrapBackend`（或 `UnsandboxedShellBackend`）。文件操作由其 `FilesystemBackend` 父类在宿主进程中完成（`virtual_mode`，根目录为工作区）；`execute` 交给 `SandboxPool`：按 `network` 取（或懒启动）对应的持久沙箱，由沙箱内的 Worker 为每条命令启动新的 shell。Pool 随 backend 在模型、权限切换的重建之间复用（切换不影响沙箱），由 `AgentRunner` 在会话切换和关闭时销毁。
- `/skills/` → 只读的 `FilesystemBackend`，根目录为 `~/.deep-agent/skills/`。
- 其他路径 → 一律返回权限错误。

`artifacts_root` 设为 `/workspace/.deepagents`，Deep Agents 的压缩历史等产物写在这里。

## 模型适配

模型接入的验收边界：用户配置只声明 API 协议、端点地址/凭据、模型 ID 和模型能力（输入模态、上下文窗口、可选推理强度）。Thinking、工具调用、图片、usage 和请求参数的格式转换由模型适配层完成，Agent 核心只消费统一的 LangChain 消息和既有运行事件。

新增符合 Responses 或 Chat Completions 协议的模型，原则上只增加配置，不修改 Agent、会话、沙箱、审批和工具执行代码。新发现的非标准端点字段应局限在适配层做可选字段兼容，不向用户增加厂商开关，也不在核心代码加入按模型名/厂商分支。

`llm.build_chat_model()` 按 `api` 构造客户端：

- `responses` → `ResponsesChatOpenAI`：使用 Responses API，在转换前把 `response.reasoning_text.*` 事件改名为 LangChain 1.6 能识别的 `response.reasoning_summary_text.*`。
- `chat_completions` → `ChatCompletionsChatOpenAI`：使用 Chat Completions，读取 `reasoning_content` 或 `reasoning` 字符串，正文统一保存，下一轮请求按实际收到的字段名回传。

两者继承 `ProtocolChatOpenAI`，统一同步/异步 HTTP 客户端、超时、重试和请求前附件解析。附件解析只生成请求消息，checkpoint 保存引用。Responses 在 SDK stream 入口包装已知非标准事件，继续复用 LangChain 的完整解析循环、工具调用拼接和异常映射；标准事件保持原样。所有端点统一采用 SDK 标准请求序列化，不按主机、端口或厂商嵌套 extra_body。结构化输出的 SDK parse 路径与 create 共用响应 Usage 规范化。Chat Completions 的思考正文统一保存为 reasoning_content，并在消息元数据中保留来源字段名，历史回传时按原字段发送。

`QwenChatOpenAI`、`TokenPlanChatOpenAI`、`ReasoningChatOpenAI` 及旧事件规范化函数暂时保留为弃用的 Python 别名。内部不使用厂商类名选择协议。

`UsageIdentityMiddleware` 在模型结果进入 checkpoint 前标记模型来源，不改变工具或图的执行决定。流式回调将 usage-only chunk 转为既有 usage 事件；TUI 仅读取 API 的 input_tokens。缺失、取消、切换和压缩时清除旧值，恢复只读取当前来源对应的有效 usage。自动压缩仍使用 Deep Agents 原有机制。

配置了 `context_window` 时，写入模型 profile 的 `max_input_tokens`，Deep Agents 据此计算压缩阈值。

思考内容在 `agent/stream.py` 中拆分：除 `reasoning_content` 等字段外，也处理正文中的 `<think>…</think>` 标签。

## 上下文压缩

自动压缩使用 Deep Agents 默认加入的 `SummarizationMiddleware`：

- 配置了 `context_window`：约 85% 时触发，保留最近约 10% 的上下文。
- 未配置且模型 profile 也没有 `max_input_tokens`：约 170,000 tokens 触发，保留最近 6 条消息。
- 被摘要掉的消息以 Markdown 写入 `/workspace/.deepagents/conversation_history/<id>.md`（即工作区下的 `.deepagents/`），Agent 之后仍可读取。

`/compact` 调用 `AgentRunner.compact_context()`，在图内执行 `compact_conversation` 工具，要求 usage 达到自动阈值的一半。

具体阈值由 Deep Agents 决定，升级依赖后可能变化。

## 会话存储

每个工作区一个 SQLite 文件（WAL 模式，权限 `0600`），包含：

- LangGraph `SqliteSaver` 的 checkpoint：消息、中断、图状态（含 `todos`）。序列化器禁用 pickle 回退。
- `session_catalog` 表：标题、时间、模型、推理强度、权限模式、待生效的切换、上一轮的结束原因（`pending` / `stop` / `error` / `aborted` / `deferred`）。旧表结构在打开时迁移。

数据库旁边：

- `<db>.locks/`：每个会话一个锁文件，`flock` 独占；进程退出时由内核释放，锁文件不删除；文件描述符设置 close-on-exec，子进程不继承。
- `<db>.attachments/`：按内容哈希存储的图片。

恢复时按上一轮的结束原因决定行为：`pending`（且已有 checkpoint）、`aborted`、`error` 会在下一次用户输入时附加恢复说明；明确的审批或暂停中断按 checkpoint 恢复，不重新执行工具。会话切换在全部读取完成后才提交，中途失败回滚到原会话。

## 依赖

主要依赖及版本范围见 [`pyproject.toml`](../pyproject.toml)。其中 `deepagents` 固定在 `>=0.7.17,<0.8.0`，`langchain-openai` 固定在 `>=1.6.2,<1.7.0`，因为模型适配和压缩逻辑依赖这两个包的内部接口。
