## v0.1.9 更新内容

### 宿主 MCP

- 在宿主进程接入 MCP（Streamable HTTP 与 stdio），独立于 Bubblewrap 和原生文件工具；工具名形如 `mcp__<服务器>__<远程工具>`。
- `mcp.json` 增加根级 `enabled`（默认 `true`，旧配置兼容）。`/mcp enable` / `/mcp disable` 在运行中排队到安全点切换工具表，不中断当前 Agent；关闭时不连接、不启动 stdio、不触发 OAuth，配置和加密令牌缓存保留。
- 启动仅在全局开启时自动连接各服务 `enabled: true` 的项。OAuth 优先复用加密缓存并刷新；无法刷新时打开浏览器。退出会取消未完成的连接与授权等待。
- `/mcp` 显示 `MCP: Enabled` / `MCP: Disabled`，开启时列出服务名、连接状态和工具数；`connected` 为绿色，其余为红色。MCP / OAuth 日志只写后台文件，不进入终端。
- 工具审批与原生工具共用 `/permission`：`mcp_list` 免审批，`mcp_add` / `mcp_remove` 和外部 MCP 工具在 `ask` 下需批准。审批卡片只展示脱敏后的参数。

### 沙箱与执行

- 文件系统收敛为 `restricted` 与 `full-access`；额外挂载支持 `dev_bind`。文件工具按挂载的只读或读写属性授权。
- 每次模型调用附加当前沙箱环境快照（不写入 checkpoint）。会话切换时复用沙箱，不因换对话而销毁 Worker。
- 模型工具表不再提供 `grep` / `glob`；代码搜索通过沙箱内 `rg` 完成。已移除独立 HTTP 服务入口，只保留 CLI。

### 网络、模型与终端

- 主配置顶层 `trust_env` 统一模型、MCP、Tavily 与子进程的代理 / CA / SOCKS 行为，默认忽略环境代理。
- 模型请求增加自动重试；可用 `tools.hidden` 从模型请求中隐藏指定工具。
- 会话 TPS 统计持久化，并补齐推理摘要识别口径。精简 MCP 向导、开关提示和 ALLOW 确认文案。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

GitHub Release 与 PyPI 由同一个发布工作流构建并发布。更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.1.9/docs/tui.md)和[架构设计](https://github.com/ADaozz/deep_agent_cli/blob/v0.1.9/docs/architecture.md)。
