# 作为 Python 库使用

`agent` 包可以脱离终端界面使用。公开 API 在第一个稳定版之前可能变化。

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
result = runner.invoke("列出 /workspace 下的文件")
print(result.status, result.output)
runner.close()
```

注意：

- `create_agent()` 不会自动读取配置文件。不传 `settings` 时使用代码内默认值（`http://localhost:8000/v1`，模型 `qwen3.5-plus`）。需要与 CLI 一致时显式传入 `Settings.load()`。
- 不传 `backend` 时与 CLI 一样做沙箱预检，失败时抛出 `SandboxUnavailableError`，除非 `settings.sandbox.allow_unsandboxed` 为真。
- 不传 `session_store` 时，会话只保存在内存中。持久化可参考 `agent/cli/main.py` 中的 `create_runner()`。
- `AgentRunner` 同一时间只允许一个操作；并发调用会抛出 `RuntimeError`。

## 处理中断

`invoke()` 返回的 `RunResult.status` 可能是：

| 状态 | 含义与继续方式 |
|---|---|
| `completed` | 正常结束，回答在 `result.output` |
| `failed` | 出错，原因在 `result.error` |
| `cancelled` | 被 `request_cancel()` 取消 |
| `waiting_confirmation` | 等待审批：`runner.approve_tool(tool_call_id)` 或 `runner.reject_tool(tool_call_id, message)`；待审批调用在 `result.pending_tool_calls` |
| `waiting_human` | 等待回答：`runner.submit_human_input(values)`；问题在 `result.human_input`。只有自由文本框时传 `{"text": "..."}`，有字段时按字段 `id` 传值 |
| `paused` | 已暂停：`runner.continue_run()` |
| `runtime_config_boundary` | 停在排队的模型 / 权限切换处：`runner.resume_runtime_config()` |

当前中断也可以通过 `runner.current_interrupt()` 查询。

运行中可以从其他线程调用 `runner.steer(text)`、`runner.follow_up(text)`、`runner.request_pause()`、`runner.request_cancel()`。

## 事件

`on_delta(kind, text)` 接收流式的思考与回答增量。`on_event(RunEvent)` 接收结构化事件，不含 ANSI 序列或终端宽度信息。

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
- `request_human_input` 取代了已移除的 `handoff_to_human`；停在旧工具上的会话恢复时会报错，需要新建会话。
