# 领域边界与 Python API

项目按功能领域组织代码，将稳定契约、业务规则和外部系统适配分开。`agent` 根包继续提供应用级 API，使用延迟导入；只导入模型或运行事件类型不会初始化配置、SQLite、HTTP 客户端和运行器。

## 领域地图

| 上下文 | 状态与规则 | 适配与协调 |
|---|---|---|
| `models` | 模型能力、推理参数、模型目录查找 | 协议客户端、流生命周期、消息解析和 HTTP 环境 |
| `sessions` | 会话元数据、图片引用、恢复规则、快照结果 | Repository 接口、SQLite、租约、附件文件存储、快照读取 |
| `runtime` | 运行契约、当前和待生效配置、审批规则 | Runner 用例入口、图执行、取消与队列、事件和计时 |
| `execution` | 文件系统策略、挂载值、执行能力 | 文件路由、挂载布局、Bubblewrap、进程输出、资源池和独立 Worker |
| `mcp` | 操作结果和连接状态 | 配置持久化、授权、连接、工具快照和宿主调用 |
| `subagents` | 子任务配置和并发约束 | 隔离图运行、取消关联和日志 |
| `cli` | 对话框、输入草稿、运行展示、导航与选择状态 | 功能控制器、按键、布局、渲染和终端生命周期 |
| `bootstrap` | 配置加载与启动流程 | 创建模型、工具、中间件和图，连接上述上下文 |

纯规则模块不导入配置加载、展示、SQLite、HTTP 或图框架。框架消息转换、中间件、工具和存储实现是适配器，可以调用所属领域契约。跨领域的可变状态通过所有者提供的方法修改，不通过复制状态来同步。

## 状态和接口

- `RuntimeConfiguration` 管理当前配置及待切换请求；请求不立即改变当前值。Runner 在操作锁和模型安全边界内协调图重建、存储及发布，失败时保留既有执行语义。
- `GraphExecution` 将图流、框架中断及取消结果转换为运行结果；Runner 负责资源、会话所有权及收尾。
- 模型传输通过 `ImageReader` 读取附件，不依赖附件文件存储实现。
- 会话摘要的首轮资格、脱敏和标题校验由 `sessions.summary` 管理；`models.session_summary` 发起独立标题请求，CLI 控制后台任务和显示缓存。摘要以真实 thread_id 进行一次性写入。
- `SessionRuntime` 通过 `SessionRepository` 访问会话和租约，通过 `ModelCatalog` 决定恢复方案。快照读取不更改活动会话租约。
- `WorkspaceCompositeBackend` 先准备文件路由，再调用 `ManagedExecutor.reconfigure` 停止旧 Worker 并发布执行配置；失败时不发布新路由。
- CLI 功能控制器接收所需服务、状态和回调，不保存 `CliApplication`。`DialogState`、`InputDrafts`、`RunPresentation` 保存共享展示状态，导航状态由 `TranscriptNavigation` 管理。跨功能调用保留显式依赖，延迟绑定处理界面组装顺序。

## 模块与接口

应用级公开类型和工厂函数从根包导入：

```python
from agent import AgentRunner, ModelProfile, RunEvent, create_agent
from agent.bootstrap.config import Settings
```

直接引用领域模块时，按职责选择导入位置：

| 职责 | 模块 |
|---|---|
| 配置加载、启动初始化、图装配 | `agent.bootstrap.config`、`agent.bootstrap.setup`、`agent.bootstrap.factory` |
| 模型配置、目录查找、协议客户端 | `agent.models.profile`、`agent.models.catalog`、`agent.models.client` |
| 运行入口、事件契约、待生效配置 | `agent.runtime.runner`、`agent.runtime.contracts`、`agent.runtime.configuration` |
| 审批、取消、队列与计时 | `agent.runtime.permissions`、`agent.runtime.cancellation`、`agent.runtime.control`、`agent.runtime.metrics` |
| 会话元数据、存储接口、SQLite 实现 | `agent.sessions.contracts`、`agent.sessions.ports`、`agent.sessions.store` |
| 会话生命周期、快照读取、租约 | `agent.sessions.lifecycle`、`agent.sessions.reader`、`agent.sessions.locks` |
| 图片引用与附件文件存储 | `agent.sessions.images`、`agent.sessions.attachments` |
| 执行策略、能力、后端和文件路由 | `agent.execution.policy`、`agent.execution.types`、`agent.execution.backend`、`agent.execution.filesystem` |
| 挂载布局、进程、资源池和 Worker | `agent.execution.layout`、`agent.execution.process`、`agent.execution.pool`、`agent.execution.worker` |
| MCP 状态、连接管理、子任务配置 | `agent.mcp.contracts`、`agent.mcp.manager`、`agent.subagents.configuration` |

`ModelProfile` 和 `InputKind` 属于模型领域；`BindMount`、`SandboxConfig` 和 `SandboxMode` 属于执行策略；运行事件和中断定义在 `runtime.contracts`；会话元数据定义在 `sessions.contracts`。`Settings` 承担配置加载和组装输入。

CLI 的功能入口位于对应控制器，例如 `app.model_selection.select_model()`、`app.run_dispatch._start_run()` 和 `app.transcript.scroll_transcript()`；展示状态通过 `app.dialogs`、`app.drafts` 和 `app.run_view` 访问。这些是内部接口，嵌入应用应使用 Runner API。

用户配置由启动层加载；SQLite 会话、checkpoint 和图片引用由会话领域管理。提示词提供运行上下文，沙箱执行策略与工具审批分别控制资源访问和调用许可。

## 验证

`tests/test_domain_boundaries.py` 检查纯规则的导入方向、控制器与应用的解耦、根包延迟导入，以及配置请求与会话恢复的不变量。修改边界时应同时运行相关功能测试；目录或文件行数本身不作为完成标准。

完整验证包含本地 HTTP 模拟服务、真实 Bubblewrap、PTY 和浏览器场景。运行这些测试需要对应系统权限及运行时；跳过与失败必须分别记录。构建还需验证 sdist/wheel 中包含新模块、配置模板和独立 Worker，并从安装环境而非源码工作区检查导入及入口。

本次构建、完整套件及修复后复测结果见 [验证记录](reports/domain-refactor-validation.md)。
