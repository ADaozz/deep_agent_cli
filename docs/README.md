# Deep Agent CLI 文档总览

本目录包含 Deep Agent CLI（v0.4.0）的用户指南、运行机制说明、安全边界参考与开发文档。所有内容均以当前代码实现为准。

---

## 按需求选择阅读路径

- **第一次使用**：先看 [项目首页](../README.md#快速开始) 完成安装，再按 [配置参考](configuration.md) 配置模型与沙箱挂载，配合 [终端交互指南](tui.md) 熟悉快捷键与斜杠命令。
- **排查环境与权限问题**：优先阅读 [沙箱与权限](sandbox.md)（含 Ubuntu / WSL2 排查），若涉及长时命令或交互式输入，查看 [交互式命令执行](execution.md)；若涉及凭据输入或脱敏，查看 [安全凭据管理](secrets.md)。
- **扩展工具与工作流**：接入外部工具看 [MCP 集成](mcp.md)，拆分并行子任务看 [SubAgent 委派](subagents.md)，配置右侧伴随面板看 [Lain Companion](companion.md)。
- **嵌入 Python 或参与开发**：嵌入运行核心看 [Python API](library.md)；理解内部模块边界与测试规范看 [系统架构](architecture.md)、[领域设计](domain-design.md) 与 [开发与测试](development.md)。

---

## 文档目录

### 用户指南

| 文档 | 内容摘要 |
|---|---|
| [配置参考](configuration.md) | `config.yaml` 完整结构、多来源模型与推理强度、沙箱挂载、UI 配置、`keybindings.json`、工作提示语与环境变量 |
| [终端交互指南](tui.md) | 启动参数、全部 18 个斜杠命令、快捷键、运行中介入队列、交互式面板、`/stdin` 接管、Skills、图片粘贴与会话恢复 |
| [Lain Companion](companion.md) | 本地资源/Git/服务采样、Tool Health 60 次窗口热力图、本地 GIF 表情状态、Talk To Lain 只读对话与渲染器对比 |
| [MCP 集成](mcp.md) | `~/.deep-agent/mcp.json` 配置、全局开关、stdio 与 Streamable HTTP、Bearer/OAuth 认证、`/mcp` 命令与宿主运行边界 |

### 核心机制与安全边界

| 文档 | 内容摘要 |
|---|---|
| [沙箱与权限](sandbox.md) | Bubblewrap `restricted`/`full-access` 隔离、HOME 与路径挂载、网络限制、`ask`/`auto`/`allow` 与 Flash 审查、Playwright/X11 与 WSL2 排查 |
| [交互式命令执行](execution.md) | `execute` 与 `write_stdin` 协议、持久 Worker、Pipe 与 PTY、单一写队列（1 MiB / ACK）、超时取消清理以及内建 `rg` 代码搜索 |
| [安全凭据管理](secrets.md) | `request_human_input` 与 `/secrets` 掩码录入、保留字段与确认步骤隔离、`SecretStore`（会话内存 / Fernet 持久化）、作用域注入、MCP 指纹绑定与流式脱敏边界 |
| [SubAgent 委派](subagents.md) | `start_task`/`read_task`/`cancel_task`、同步嵌套与后台叶子规则、并发与深度配额、权限继承与隔离 JSONL 摘要日志 |

### 架构、接口与规范

| 文档 | 内容摘要 |
|---|---|
| [系统架构](architecture.md) | 运行时分层、`AgentRunner` 与图装配、完整工具注册表、Middleware 链、`RunEvent` 事件流、会话持久化与上下文压缩 |
| [领域设计](domain-design.md) | 各子域职责归属、依赖方向与边界约束（面向维护者与重构参考） |
| [TUI 渲染规范](tui-rendering-spec.md) | 工具卡片、交互面板、Markdown 与 Diff 渲染的锁定视觉规范（与黄金快照测试绑定） |
| [Python API](library.md) | 顶层导出符号、`AgentRunner`、`create_agent`、中断恢复、`RunEvent` 消费与自定义工具接入 |
| [开发与测试](development.md) | 本地开发环境、pytest 标记（`sandbox` / `integration` / `pty`）、冒烟脚本、CI 与发布流程 |

### 架构与流程图索引

所有矢量图位于 `assets/readme/`，统一采用与 TUI 一致的深色粉色系纯原生 SVG（无外部字体与脚本）：

| 图表 | 对应文档 | 说明 |
|---|---|---|
| [hero.svg](../assets/readme/hero.svg) | [README.md](../README.md) | 项目首页 Hero 横幅 |
| [runtime-map.svg](../assets/readme/runtime-map.svg) | [README.md](../README.md) | 产品级整体架构概览 |
| [runtime.svg](../assets/readme/runtime.svg) | [系统架构](architecture.md) | Agent Runtime、图装配与事件流 |
| [permissions.svg](../assets/readme/permissions.svg) | [沙箱与权限](sandbox.md) | 权限模式判断与宿主 / Bubblewrap 分流 |
| [execution.svg](../assets/readme/execution.svg) | [交互式命令执行](execution.md) | 持久 Worker、单一写队列与前后台命令生命周期 |
| [secrets.svg](../assets/readme/secrets.svg) | [安全凭据管理](secrets.md) | 凭据录入、存储确认、作用域注入与流式脱敏 |
| [subagents.svg](../assets/readme/subagents.svg) | [SubAgent 委派](subagents.md) | SubAgent 并发控制、深度边界、通知队列与摘要日志 |
| [companion.svg](../assets/readme/companion.svg) | [Lain Companion](companion.md) | 本地监控采样与 Talk To Lain 只读观察边界 |

### 历史记录

以下文件保留为早期重构基线与专项验证记录，不作为当前使用指南：

- [refactor-baseline.md](refactor-baseline.md) — 退出契约与早期重构基线映射（被 `tests/test_exit_contract.py` 引用）
- `docs/reports/` — 历史阶段验证报告存档
