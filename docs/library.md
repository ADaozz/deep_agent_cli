# 作为 Python 库使用

`agent` 包可以脱离终端界面使用。这是在其他 Python 程序中复用运行核心的方式。公开 API 在第一个稳定版之前可能变化。

## 基本用法

```python
from agent import AgentRunner, create_agent
from agent.config import Settings

settings = Settings.load()                 # 读取 ~/.deep-agent/config.yaml 或 DEEP_AGENT_CONFIG
prepared = create_agent(settings=settings)
runner = AgentRunner(
    prepared=prepared,
    settings=settings,
    on_delta=lambda kind, text: print(text, end=""),
    on_event=print,
)
result = runner.invoke("列出项目工作区下的文件")
print(result.status, result.output)
runner.close()
```

注意：

- `create_agent()` 不会自动读取配置文件，也不会读取 `mcp.json`。不传 `settings` 时使用代码内默认值（`http://localhost:8000/v1`，模型 `qwen3.5-plus`）。需要与 CLI 一致时显式传入 `Settings.load()`。要使用 MCP，自行构造 `MCPManager`，`load()` 后传给 `create_agent(mcp=manager)` 或 `AgentRunner(mcp_manager=manager)`。根级 `enabled` 为 `false` 或存在配置错误时不注入 `MCPMiddleware`，工具表与未传入 manager 相同。MCP 连接在宿主进程中建立，不进入 Bubblewrap。
- 默认模板中的工具挂载来自配置文件；直接构造 `Settings()` 或 `SandboxConfig()` 不会自动添加这些条目，也不做工具链发现。NVM 版本、Cargo / Rustup、浏览器缓存及 PATH 由调用方配置，见 [默认工具挂载](configuration.md#默认工具挂载)。
- 不传 `backend` 时与 CLI 一样做沙箱预检，失败时抛出 `SandboxUnavailableError`。Bubblewrap 是必需的。
- 不传 `session_store` 时，会话只保存在内存中。持久化可参考 `agent/cli/main.py` 中的 `create_runner()`。
- `AgentRunner` 同一时间只允许一个操作；并发调用会抛出 `RuntimeError`。

## SubAgent 委派

与 CLI 使用同一套 `settings.subagents` 配置，默认提供原生同步 `task`。
只有问题能分解为多个独立、互不干扰的子问题时，主 Agent 才应派生 SubAgent。
共享工作区不会自动隔离文件修改；存在依赖或修改冲突的步骤由主 Agent 顺序处理。

子任务复用工厂图和已验证的 Backend/Pool，不创建新的 Runner；默认并发 2，超限立即返回错误。
`AgentRunner` 的单操作限制仍然有效，不能通过并发调用同一 Runner 的 `invoke()` 派生任务。
子任务使用独立上下文和 `allow`，不装载 MCP、调用方的 `extra_tools`、人工输入或递归委派工具；
显式注入的 `CUSTOM` Backend 无已验证沙箱身份时，委派会被拒绝。主任务取消及 Runner 关闭会传播到关联子任务。

默认模型 Profile 继承主模型，思考强度 `default/null` 使用子模型原生默认；独立客户端在任务结束后关闭。
直接注入的自定义 `BaseChatModel` 保留原对象，强度覆盖需显式指定子模型 Profile。
执行摘要独立持久化且按父会话隔离，只精确替换已知密钥；完整配置与查询工具见 [SubAgent 配置](configuration.md#subagent-委派)。

## 处理中断

`invoke()` 返回的 `RunResult.status` 可能是：

| 状态 | 含义与继续方式 |
|---|---|
| `completed` | 正常结束，回答在 `result.output` |
| `failed` | 出错，原因在 `result.error` |
| `cancelled` | 被 `request_cancel()` 取消，本轮在取消生效前未产生最终回答；若无法确认模型请求已关闭，`result.error` 非空。最终回答已写入 checkpoint 后才到达的取消不改变 `completed` |
| `waiting_confirmation` | 等待审批：`runner.approve_tool(tool_call_id)` 或 `runner.reject_tool(tool_call_id, message)`；待审批调用在 `result.pending_tool_calls` |
| `waiting_human` | 等待回答：`runner.submit_human_input(values)`；问题在 `result.human_input`。只有自由文本框时传 `{"text": "..."}`，有字段时按字段 `id` 传值 |
| `paused` | 已暂停：`runner.continue_run()` |
| `runtime_config_boundary` | 停在排队的模型 / 权限切换处：`runner.resume_runtime_config()` |

当前中断也可以通过 `runner.current_interrupt()` 查询。

一批工具需要分别批准或拒绝时，使用 `runner.decide_tools(decisions)`，每项包含 `tool_call_id`、`decision`（`approve` / `reject`）及可选的拒绝 `message`。必须覆盖每个待审批调用且不能重复；整个批次在一次恢复操作中提交。

运行中可以从其他线程调用 `runner.steer(text)`、`runner.follow_up(text)`、`runner.request_pause()`、`runner.request_cancel()`。

## 事件

`on_delta(kind, text)` 接收流式的思考与回答增量。`on_event(RunEvent)` 接收结构化事件，不含 ANSI 序列或终端宽度信息。

原生 `task` 的开始和完成事件可用于构建独立任务卡片，最终结果保持完整；TUI 与历史恢复共用子任务失败/取消状态解析，避免把正常返回的失败摘要标成成功。

`subagents_changed` 事件的 `result["count"]` 是实际运行数量。子任务模型增量、思考和原始工具输出不写入摘要，也不转发给主 Runner 的流式回调。摘要仅记录起止、工具操作与状态、耗时及有界最终结果；正常 task 最终结果返回保持完整。

`execute` 的 `tool_completed` 事件中，`result` 包含 `exit_code`、`truncated`、`host_log_path`、`agent_log_path`、`termination_reason`；`content` 是发给模型的文本。所有工具的 `ToolMessage.artifact` 原样放在 `RunEvent.artifact` 中。

## 自定义工具与审批

```python
from agent import create_agent
from agent.tools.examples import build_example_tools

prepared = create_agent(
    extra_tools=build_example_tools(),           # 示例：只读的 lookup_docs
    interrupt_on={"my_side_effect_tool": True},  # 有副作用的自定义工具需显式加入审批
)
```

- `interrupt_on` 与 `ask` 模式的内置规则合并；与内置规则冲突时抛出 `ValueError`。
- 工具抛出的异常不会自动重试，以免重复产生副作用。
- `instructions="..."` 覆盖配置中的 `agent.instructions`；`skills=[...]` 指定 Skills 来源。
- 自定义模型通过 `model=` 传入。只有 `ChatOpenAI` 子类或声明了 `materializes_attachment_refs = True` 的模型支持图片附件。

## 会话

- `runner.list_sessions()`、`runner.new_session()`、`runner.switch_session(id)`。
- 目标会话被其他进程持有时，`switch_session` 抛出 `SessionLockBusyError`。
- 切换会话前需要先用 `runner.take_unapplied_messages()` 取回未生效的 steering / follow-up，否则抛出 `RuntimeError`。
- 主 Agent 的人工输入仅使用 `request_human_input`；不提供旧工具或旧数据库格式的兼容恢复。
