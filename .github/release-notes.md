## v0.2.2 更新内容

### 会话中文摘要（PR #33）

- 新会话首次成功完成一轮后，后台额外请求一次中文标题（目标 12～25 个汉字），用于 `/resume`、`deep-agent resume` 列表和底栏；无摘要时显示 thread_id，选择项始终绑定真实会话 ID。
- 标题生成使用独立客户端，不调用工具、不写入聊天历史、不计入 TPS、不阻塞输入；超时 10 秒且不重试，请求和结果均先脱敏并限长。
- 历史会话不自动补全摘要；数据库初始化时自动补齐摘要列，已有记录保持不变。

### 上下文压缩与超大请求恢复

- 新增 `agent.compaction_threshold`，按模型上下文窗口比例触发自动压缩（取值大于 0 且不超过 1，省略时为 0.85，实际取其与 0.85 的较小值）。示例配置设为 0.60。
- 新增请求体恢复中间件：工具结果中的内嵌媒体在请求前限制在约 2 MiB，优先保留最近的媒体；收到 HTTP 413 时移除剩余内嵌媒体、截短超长工具文本并重试一次。原始 checkpoint、用户消息和附件均不改动，修复超大工具结果导致会话无法继续的问题。

### 追加消息队列与中断发送

- 排队的 steering / follow-up 改为在独立区域逐条显示，可滚动查看，不再挤在状态栏中。
- 有排队消息时 `Esc` 或 `Alt+Up` 将队列撤回到输入框，不中断当前任务；连续按两次 `Enter` 则中断当前任务并在停止后立即发送队列。
- `Ctrl+B` 只负责展开 / 收起已发送的长消息。

### TUI 与状态栏

- 长会话转录渲染改为增量复用未变化的单元，5000 块场景下 `render_document` P95 由约 220 ms 降至约 18 ms（PR #32）。
- 底栏显示模型原始名称；推理强度选单始终把 `default` 放在首位；选择类弹窗精简冗余提示；沙箱模式显示为 `sandbox`。
- `/status` 新增按模型统计的工具调用表（子智能体请求不计入主会话）。
- 收到 SIGTERM / SIGHUP 时走正常退出流程并恢复终端模式，第二次信号立即退出；Git 改动数不再统计未跟踪文件。
- 修复 `TERM=dumb` / `unknown` 终端（部分 IDE 内嵌终端、Emacs shell 等）下转录区固定按 80 列折行、忽略实际终端宽度的问题。
- 精简内置提示词与 `ls` / `read_file` / `request_human_input` 工具说明，减少多余调用轮次。

### 内部重构与依赖

- 按领域边界拆分核心模块：模型、会话、运行控制、执行环境与启动装配各自独立，CLI 控制器通过显式依赖协作；不保留旧模块路径转发，直接引用内部模块的 Python 代码需按 [架构文档](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.2/docs/architecture.md) 调整导入路径。
- 新增 `RunMetrics` 与 `examples/benchmark.py`，用于对照 agent、cli、sandbox 耗时。
- 升级依赖：`deepagents>=0.7.35`、`langchain-core>=1.7.7`、`langchain-openai>=1.7.4,<1.8.0`，同步提高 `langchain[mcp]`、`langgraph`、`langgraph-checkpoint-sqlite` 下限。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

已有用户配置不会自动覆盖；如需调整压缩阈值，请手动在 `agent` 段加入 `compaction_threshold`。

更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.2/docs/tui.md)和[配置参考](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.2/docs/configuration.md)。
