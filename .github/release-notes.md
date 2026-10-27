## 未发布 · 原生 SubAgent（PR #30）

- 接入 Deep Agents 原生同步 `task` 和隔离 `CompiledSubAgent`。仅当问题可拆成多个独立、互不干扰的子问题时才应派生；默认最多同时运行 2 个子任务。
- 子任务共享已验证的沙箱 Backend/Pool，使用 `allow`，不装载 MCP、人工输入或递归委派工具；父任务取消传播到子任务，任务结束不保留后台 SubAgent。
- 模型 Profile 可继承主模型，思考强度 `default/null` 使用子模型原生默认；支持显式能力校验和独立客户端回收。
- 输入框上方仅显示运行数量，task 卡片及内部流在实时与历史恢复时隐藏；独立 JSONL 日志仅精确替换已知模型和 Tavily Key，支持有界、按父会话隔离的查询。
- 现有用户配置不会自动覆盖；配置项和派生条件见 [配置参考](../docs/configuration.md#subagent-委派)。

## v0.2.0 更新内容

### CLI 运行与退出

- `Ctrl+C`、`Esc`、`Ctrl+D` 和 `/exit` 的判定集中到统一的输入动作路由；退出拆成请求退出、界面收尾和停止三个阶段。退出开始后不再登记新的 Run、会话等待和 MCP 操作，也不消费 steering / follow-up 队列。
- MCP 操作的忙闲状态与工具表发布集中到 `MCPController`。退出中的新 MCP 操作被拒绝，晚到的完成回调不发布工具表、不写转录；`Esc` 取消会话等待会回到选择器，CLI 退出只取消等待。
- 会话快照只由 `SessionController` 应用，空闲配置只由 `AgentRunner.apply_pending_config_if_idle()` 应用；运行事件与结果分类移到 `agent/run_events.py`，`RunResult` / `RunEvent` 仍从 `agent.runner` 导入。
- 修复退出后仍可能启动 Run、登记界面辅助任务或把失败任务记错的问题。任务失败按对象身份只记录一次。

### 配置与显示

- 内容折叠统一为 `ui.head_lines` 和 `ui.tail_lines`，默认各 4 行。`thinking_tail_lines`、`execute_tail_lines`、`tool_tail_lines`、`expanded_tool_lines`、`edit_preview_changed_lines`、`write_preview_lines`、`failure_preview_lines`、`command_failure_tail_lines` 已移除，已有配置需改成新的两个字段。
- 新增 `llm.timeout`：所有模型共用的 HTTP 首字节超时，默认 60 秒，限制建立连接和等待首批响应数据。
- 新增 `stream_chunk_timeout`：模型流连续无新 chunk 的超时，默认 120 秒；超时后中断并重试当前请求，持续输出不受该上限约束。
- 新模板默认 `sandbox.mode: restricted`，宿主 `/dev` 只在 `modes: [full-access]` 时做 `dev_bind`；省略 `modes` 的旧挂载仍在两种模式生效。`/sandbox mounts` 展示下一次创建 Worker 的有效启动策略，切换失败时不发布新路由与保护。
- 统一终端消息间距、工具状态圆点和命令标识（`/help`、`/status`、`/session` 输出以命令名开头）。回合耗时写入会话数据库，恢复会话时重建历史耗时行。

### 提示词与工具

- 移除中文化中间件：上游内置文件工具及参数的说明原样提供（英文），不再在模型请求阶段改写工具 Schema；本地自定义工具仍可使用中文说明。系统提示词保持中文。
- Skills 目录改由 `create_deep_agent(skills=...)` 装配的上游原生 `SkillsMiddleware` 渲染，项目不再替换技能目录文本。
- 收紧人工输入提示：仅在缺少无法合理确定的信息或授权时调用 `request_human_input`；任务完成后可直接说明可选后续操作，不必为此中断。
- 系统提示词补充任务执行、代码修改、验证和沟通约束，身份说明改为运行在 Deep Agent CLI 中的编程智能体。

### 测试与文档

- 新增 `docs/refactor-baseline.md`，固定退出、取消、会话租约和运行事件契约，供后续改动对照。
- CLI 测试按渲染、交互、布局和运行控制拆分，共用测试辅助函数；新增退出准入、生命周期收尾、运行事件和模型切换等契约测试。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

已有配置不会被自动覆盖：`ui` 中已移除的折叠字段会在启动时报错，请先改为 `head_lines` / `tail_lines`；需要开放宿主 `/dev` 的挂载项请标为 `modes: [full-access]`。

GitHub Release 与 PyPI 由同一个发布工作流构建并发布。更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.0/docs/tui.md)和[架构设计](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.0/docs/architecture.md)。
