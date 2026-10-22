# MCP / Host Dev 修复验收（2026-10-22）

目标分支：`feat/mcp-and-host-dev`；审查基线：`6683b529`。

## 修复范围

- `/mcp add/test/remove` 经现有 I/O 线程池执行；操作开始即占用忙碌标志，结果和 Graph 更新事件回到 UI 线程。网络等待期间保留输入和刷新；Esc 明确提示后台操作仍在运行，取消等待不会假称已终止连接。
- `load()` 只读取、校验配置；TUI 开始运行后后台连接非 OAuth 服务。OAuth 仅在显式添加或测试时触发。未连接服务不发布工具，已连接工具在安全边界更新。
- 每个 Server 的单调操作版本及配置对象检查阻止晚到结果恢复已删除、重载或关闭的服务。过期连接会被关闭，网络和 Client 关闭均不持有 Manager 锁。
- 工具快照以现有 Revision 固定关联 Client；构建失败释放快照，过期 Revision 拒绝提交，旧 Graph 的 Client 保留至安全重建成功。没有将外部工具加入 `AgentSpec.tools`。
- HTTP 禁止 userinfo 和 Token 查询参数；stdio 拒绝明确的明文凭据选项，保留普通参数及 `env_passthrough`。配置展示、MCP 审批卡片和错误复用脱敏函数；工具文本及结构化结果过滤已知凭据。
- 连接建立或工具包装异常均清理 Client。Runner 关闭超时或模型请求尚未退出时保留 MCP Bridge、Client 和会话锁，待退出后重试关闭。
- Sandbox 产品代码、原生 ASK/ALLOW、Runner 推理调度和 Run Epoch 均未修改。

## 测试

本地环境：Python 3.12.14、LangChain 1.4.7、DeepAgents 0.7.30、FastMCP 4.1.3、MCP SDK 2.3.0。Bubblewrap 探测结果为 `SUPPORTED / isolated`。

| 范围 | 结果 |
| --- | --- |
| 六个指定 MCP 测试文件（含最后追加的损坏 UTF-8 配置边界） | 62 passed |
| Runner、权限、模型、会话、取消、CLI、Sandbox 指定回归 | 564 passed |
| 正常终端下已有渲染测试 | 32 passed |
| 真实 Prompt Toolkit 和新增 PTY 非阻塞验收 | passed |
| wheel / sdist 构建及 twine 元数据检查 | passed |
| 本地完整 pytest | 1409 passed, 5 skipped（运行启动后追加的 UTF-8 边界另由上述 MCP 回归覆盖） |
| GitHub Actions | 最终分支提交的 Python 3.12 / 3.13 及 Ubuntu 默认策略结果以 PR Checks 为准 |

覆盖交错连接/删除/重载、两次并发测试连接、过期 Graph 构建提交、构建失败重试、待审批和执行中移除服务、正常/超时/异常清理，以及 stdio 握手期间关闭后的进程回收。

真实 Bubblewrap 回归确认宿主 HOME 与 `/tmp` 写入数据一致，整目录 `/dev` 仍须显式配置，单设备可独立配置；安装目录和配置目录自保护以及 restricted 原有边界由现有回归覆盖。

跳过项为当前环境缺少动态链接系统 rg、Playwright CLI / 浏览器的已有测试；未通过跳过掩盖 MCP 或 Sandbox 测试失败。

首轮全量运行继承了环境的 `TERM=dumb`，使 Rich 忽略窄屏宽度，触发一个既有渲染断言。渲染源码未改；正常终端类型 `TERM=xterm-256color` 下渲染测试全部通过，并以此终端类型重新运行完整测试。

## 实际服务验收

| 服务 | 实际验证 |
| --- | --- |
| DeepWiki | 官方 MCPAdapter HTTP 连接，发现 3 个工具，调用 `read_wiki_structure(repoName="langchain-ai/deepagents")` 返回真实文档目录 |
| 本地 FastMCP | stdio 工具执行、调用超时、执行中取消、正常关闭与连接握手中关闭后的子进程回收 |
| 本地 Bearer MCP | 缺失、错误、过期与有效凭据；凭据回显及日志、列表、工具结果脱敏 |
| 本地 OAuth MCP | 官方 InMemoryOAuthProvider + 实际 HTTP 动态注册、授权跳转、回调、工具发现与鉴权调用；短期 Token 到期后由官方客户端自动刷新，不重新打开浏览器 |

DeepWiki 使用本执行环境要求的出站 HTTP 代理，通过测试侧替换 HTTP Client 工厂完成；产品默认直接连接策略未改。默认直接连接在本环境中失败，因此这里不宣称默认网络路径可用。外部 DeepWiki 验收未加入 CI 必需测试。

OAuth 测试只在本地测试提供者上模拟用户访问授权链接；产品的浏览器授权仍由用户完成。

## 已知限制

- FastMCP 4.1.3 默认 OAuth Token 存储为内存，官方客户端负责刷新；重启进程后需重新授权。本轮沿用官方机制，没有新增 Token 管理或落盘系统。跨重启持久化需另行选择官方支持的安全存储后端。
- Manager 的 `snapshot()` 固定 Client；调用者必须以 `commit()` 或 `discard()` 结束构建。生产 Graph 构建路径在成功和异常路径均已覆盖。
- 当前 Graph 和待审批 Checkpoint 保留原工具及连接，移除服务在下一安全边界生效；不会通过提前关闭 Client 强行破坏当前调用。
