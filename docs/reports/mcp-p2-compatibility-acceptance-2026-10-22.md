# MCP P2 兼容性修复验收

仓库：ADaozz/deep_agent_cli。基线：`main @ aca05a3df025`。修复分支：`fix/mcp-p2-compatibility`。

## 异步调用

先验证了只保留 `func` 的 LangChain 异步回退：外层 Task 取消后 150ms，原始 MCP Coroutine 没有收到取消，Bridge 中仍有 1 个待执行请求。因此该回退不能满足 A05。

在现有 SyncAsyncBridge 上增加 `acall()`，与同步入口复用同一提交函数。MCP Coroutine 仍提交到原 Bridge Loop；调用方异步等待 Future，应用同一超时和取消事件。Task 取消时取消对应 Future，并在原 Loop 上有界等待该请求完成清理；没有建立第二套 Client 生命周期。

StructuredTool 的同步、异步入口共用错误转换及已有递归脱敏函数，不再暴露原始远程 Coroutine。异步 Task 的 CancelledError 保持传播。超时和取消事件继续返回与同步一致的工具错误。

| 编号 | 验收 |
| --- | --- |
| A01 / A02 | invoke / ainvoke 成功；同类型且同语义的输出，忽略服务端为内容块生成的随机 ID |
| A03 / A04 | 同步、异步超时均在期限及有界清理窗口内结束 |
| A05 | HTTP 和 stdio 的真实服务均收到异步 Task 取消，完成受保护的异步服务端清理 |
| A06 | 异步 Bearer 凭据回显被脱敏，错误转换与同步一致 |
| A07 | 官方 LangChain Graph + MCPMiddleware + HITL 的 astream 在批准后调用 MCP Tool |
| A08 | 取消后同一个 Client 可继续执行 echo |
| A09 | Manager 关闭后异步调用结束，Bridge 线程退出且没有遗留 Task |

A07 针对官方异步 Graph 中的 MCP 集成测试；项目原生 Runner 及其同步中间件保持原状，不宣称其全部原生中间件新增了异步能力。

## 工具名称

合法且不超长的旧名称保持不变。不兼容字符转换为下划线；发生替换或截断时，在可用长度内保留名称片段，并附加原始名称 SHA-256 的前 12 位。别名以 `mcp__{server}__` 开头，完整长度不超过 80。

原始 Server / Tool 名称保留在 `deep_agent_mcp` Metadata；包装原 MCPAdapter 调用对象，不改变远程调用目标、参数 Schema 或既有描述。别名计算不依赖发现顺序、进程随机 Hash 或连接状态。

实测服务提供带点、空格、中文及 120 字符的名称，包括 read.file / read_file 和相同长前缀的两种名称。验证了：

- 名称合法、稳定且互不覆盖；最大长度 Server 名称也在 80 字符预算内。
- 两个 Server 的同名工具各自返回正确的原始名称，证明路由目标未改变。
- 重新连接、Manager 重启后别名保持一致。
- 待审批的别名 Tool Checkpoint 在 Manager 重启后恢复并执行成功。
- 空名称、非法 UTF-8 或实际别名冲突有明确原因；连接结果、服务器列表和工具列表显示警告。

## 范围与回归

LangChain 官方 MCPAdapter、MCPManager、MCPMiddleware 和 SyncAsyncBridge 保留；MCP 工具仍独立于 AgentSpec.tools，外部工具保留宿主审批。Runner、原生权限、Sandbox 和 Checkpoint 产品代码未修改。P3 未实施，启动连接继续串行；本轮没有添加 Resources、Prompts、Sampling 或 Token 持久化。

| 检查 | 结果 |
| --- | --- |
| pytest -q tests/test_mcp_*.py | 80 passed |
| pytest -q（TERM=xterm-256color） | 最终提交的完整统计见 PR 正文和 Checks |
| wheel / sdist 构建及 twine | passed |
| Python 3.12 / 3.13、Ubuntu 默认策略 CI | 以 PR 最终提交的 Checks 为准 |

OAuth 成功、刷新、取消与失败，Bearer，stdio / HTTP，同步 Runner，独立审批，原有 Sandbox / full-access 回归由完整测试覆盖。外部网络服务没有加入 CI 必需测试。
