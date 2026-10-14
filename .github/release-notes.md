## v0.1.6 更新内容

### 模型流取消与执行状态一致性

- 修复内置模型在 SSE 静默、网络读取长期等待时无法被 `Esc` 或 HTTP Cancel 终止的问题。同步 LangGraph Runner 通过可取消的异步模型流关闭底层请求；继续保留现有 `SqliteSaver`、Session Checkpoint、同步 Python API 和沙箱架构。
- 强化取消与自然完成之间的竞态处理。模型最终回答已提交时，迟到取消不会覆盖 `completed` / `STOP`；取消、失败、完成由 Runner 的最终执行路径裁决，异常退出则由已有的 `PENDING` 恢复机制提示。
- 修复运行开始前取消可能丢失的问题；按 Run Generation 绑定模型流、预约与取消。上一轮结束清理不会删除下一轮预约；CLI 过滤上一轮迟到事件，HTTP 取消支持幂等请求并在清理完成后公布终态。
- 在真正确认模型请求关闭之前，不将取消视为完全完成；不能确认清理时明确报告错误并保留 Session Lease，避免新任务并发修改同一 Checkpoint。

### 验证

- 真实本地 HTTP SSE 故障注入覆盖两种 API 协议、TUI 与 HTTP 服务、静默取消、连接释放、反复取消、会话后续执行和并发隔离。
- 真实 TokenPlan `qwen3.8-flash` 验收覆盖 Chat Completions 与 Responses：流式 Reasoning、Tool Calls、API Usage、沙箱 Coding 任务、首包后取消和完成竞态。验证记录见 [PR #3](https://github.com/ADaozz/deep_agent_cli/pull/3)。
- GitHub Actions 在 Python 3.12 / 3.13 上的相关自动回归通过。

### 已知限制

- 非流式模型请求与外部自定义 `BaseChatModel` 不能保证硬取消。
- 取消不会撤销已执行的文件修改或外部工具副作用；恢复前应检查工作区。
- 本次未进行 `graph.astream()` 全面迁移，也不改变沙箱网络隔离边界。

### 安装与升级

要求 Python 3.12+，Linux 或 WSL2。默认沙箱需要 Bubblewrap 和可用的 user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
# 需要 HTTP 服务：
pipx install --force 'deep-agent-cli[server]'
```

GitHub Release 与 PyPI 使用同一次 CI 构建的 wheel 和源码包。更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[HTTP 服务](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/server.md)及[开发与发布](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/development.md)。
