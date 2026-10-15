# MCP Client 验收

基线：v0.1.6 / `d6ce087`；开发分支：`feat/mcp-client`。保留同步 `graph.stream()`、原有 Harness、SQLite、HITL、模型流取消和沙箱，不提供 MCP Server 或 stdio 接入。

## 技术验证

通过官方 `langchain.mcp.MCPAdapter` 和显式 FastMCP HTTP/SSE Transport 连接 `tests/mcp_test_server.py` 的真实本地 HTTP 服务。工具为异步 `StructuredTool`，`response_format=content_and_artifact`；直接同步 `invoke()` 抛出 `NotImplementedError`，`ainvoke()` 返回带 Tool Call ID 的 `ToolMessage`。同步桥完整委托该调用，真实 `LangGraph ToolNode` 经 `graph.stream()` 成功执行。

验证过的组合：现有 Python 3.12 环境为 Deep Agents 0.7.17 / LangChain 1.4.2 / FastMCP 4.1.0；Python 3.12 和 3.13 干净环境均解析为 Deep Agents 0.7.25 / LangChain 1.4.5 / FastMCP 4.1.0。依赖检查通过，没有升级 Deep Agents 主版本。

HTTP 与 Legacy SSE 都验证了参数 Schema、文本、图片、文件、结构化 artifact 和错误 status；发现后关闭 Adapter context，工具仍可以按官方可重入 Client 行为执行。配置为 `none` 时不创建 Client 或事件循环。

官方 API 与生命周期依据：[MCPAdapter](https://reference.langchain.com/python/langchain/mcp/adapter/MCPAdapter)、[官方 MCP 接入说明](https://github.com/langchain-ai/langchain/blob/master/openwiki/mcp-integration.md)。当前 `langchain.mcp` 带 beta 提示；同步桥保留官方结果，不补实现其未支持的音频转换。

## 本地验证证据

| 环境 | 结果 | 证据 |
| --- | --- | --- |
| Python 3.12.3，干净安装，全量回归 | 1010 passed, 1 skipped | [完整日志](evidence/mcp-client-python312-full.txt) |
| Python 3.13.16，干净安装，全量回归 | 1009 passed, 1 skipped | [完整日志](evidence/mcp-client-python313-full.txt) |
| Python 3.12，最终变更涉及的 MCP/CLI/启动/权限回归 | 114 passed | [最终回归](evidence/mcp-client-python312-followup.txt) |
| Python 3.13，相同最终回归 | 114 passed | [最终回归](evidence/mcp-client-python313-followup.txt) |
| 生产 HTTP Runner 初始化、MCP 快照及 Cancel | HTTP / SSE 两种传输通过 | `test_http_service_cancel_uses_same_runner` |
| wheel / sdist 构建、twine check、双版本 wheel 安装 | 通过 | 安装后从 `/tmp` 运行版本/帮助，且 MCP 模块来自 site-packages |

全量测试之后补充了取消收尾租约测试、输入框编辑恢复和生产 HTTP 初始化路径，已分别追加覆盖最终修改的回归。模型使用本地脚本，不调用付费模型 API；MCP 测试使用真实协议与本地服务，不用 Mock 代替工具发现或 ToolNode。

审批测试检查远程调用日志：批准只调用一次，拒绝不会再次调用。并发取消测试验证每个调用的 Future 独立、Task finally 收尾完成后才返回。取消收尾被延迟时，`Runner.close()` 超时并保留 Session Lease；允许收尾后可正常关闭，桥接线程退出。HTTP Cancel 经生产 RunManager 和同一 Runner 取消链路，最终无残留桥接线程。

SQLite 恢复测试重新发现工具后继续原审批，不依赖序列化 Client；新建/恢复会话、模型与 ask/allow 切换复用已发现工具。配置切换连接、冲突和保存失败保持原 Agent，禁用保留 Profile 和历史。CLI 测试覆盖 `(current)`、Esc、补全、保存失败保留输入、编辑时保留 SSE 与 URL，并验证配置操作不进入 Agent 历史。

## 边界

首版仅支持无需认证的 HTTP(S) MCP Tools，不实现 OAuth、Resources、Prompts、Sampling 或 Elicitation。Elicitation 返回 decline，不产生另一种 LangGraph 中断。远端 Server 不受 Bubblewrap 隔离；取消只确认本地收尾，不承诺远端副作用回滚。服务默认 MCP none，显式 `mcp_config` 读取启动快照，后续 TUI 改配置不影响服务。完整使用说明见 [MCP 接入](../mcp.md)。
