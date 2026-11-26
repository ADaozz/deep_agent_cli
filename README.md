<div align="center">

<img src="assets/readme/hero.svg" alt="Deep Agent CLI — 让 Agent 自主工作，让执行始终可见。" width="960" />

<p>
  <img src="https://img.shields.io/badge/version-v0.4.0-f18ab6?style=flat-square&labelColor=18131e" alt="v0.4.0" />
  <img src="https://img.shields.io/badge/python-3.12%2B-b7a8c6?style=flat-square&labelColor=18131e" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20WSL2-6fb3bf?style=flat-square&labelColor=18131e" alt="Linux / WSL2" />
  <img src="https://img.shields.io/badge/license-MIT-a08e9b?style=flat-square&labelColor=18131e" alt="MIT License" />
</p>

<p>
  <a href="#设计理念">设计理念</a> ·
  <a href="#核心体验">核心体验</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#模型与扩展">模型与扩展</a> ·
  <a href="#执行与安全">执行与安全</a> ·
  <a href="#架构概览">架构概览</a> ·
  <a href="docs/README.md">完整文档</a>
</p>

<br />

<img src="assets/readme/terminal.png" alt="Deep Agent CLI 终端界面：主转录区与右侧 Lain Companion" width="960" />

</div>

<br />

## 设计理念

终端里的 Agent 不该是一个只吐出最终答案的黑盒，也不该为了省一次确认而抹掉执行边界。

**Deep Agent CLI** 是一个面向 Linux 与 WSL2 的个人开源终端 Agent。它围绕四条朴素的原则设计：

- **Terminal-native** — 从输入、流式转录、工具卡片、原位 Diff 到交互审批，全部围绕键盘与终端工作流构建。
- **执行始终可见** — 每一次文件改动、命令输出、子任务委派与工具健康度都实时呈现在界面上，而非藏在后台日志里。
- **随时可以介入** — 任务运行过程中可以插入纠正指令、排队后续请求、直接向运行中的进程写入标准输入，或立即暂停与取消。
- **边界先于便利** — 审批模式只决定是否询问，Bubblewrap 沙箱决定命令能看到什么，凭据明文默认不进入模型上下文。

> 当前版本为 **v0.4.0**，仍处于快速演进期。在稳定版前，配置结构、会话存储格式与 Python API 仍可能出现不兼容变更。

---

## 核心体验

### 实时可观察的终端工作流

在项目目录启动 `deep-agent`，所有思考过程、工具调用、文件改动与命令输出都会按轮次记录在主转录区：

- **工具卡片与原位审阅**：每一次文件修改和命令执行都会展示状态、耗时与摘要；按 `Ctrl+R` 可在原卡片展开完整 Diff，按 `Ctrl+O` 切换工具输出折叠，按 `Ctrl+T` 切换思考过程折叠。
- **运行中介入，不必干等**：Agent 仍在执行时，按 `Enter` 可立即发送 steering 指令纠正方向，按 `Alt+Enter` 排队 follow-up 任务；按 `Alt+↑` 可将排队消息撤回输入框，按 `Esc` 取消当前轮次，`/pause` 暂停等待下一轮。
- **结构化交互与会话恢复**：需要审批、补充输入或提交敏感信息时，通过 `F2` 随时重新打开挂起的交互面板；会话自动持久化到 SQLite，使用 `deep-agent resume` 按摘要恢复上下文。

### 前后台可交互命令执行

命令不是“跑完再贴一段静态日志”，而是运行在持久化的沙箱 Worker 中：

- **实时输出与前后台切换**：长时间运行的命令可在 `yield_time` 到期后先返回当前输出与 `execution_id`，进程继续在后台运行，支持 Pipe 与 PTY 两种模式。
- **Agent 与用户共用交互通道**：Agent 可通过 `write_stdin` 向运行中的进程发送输入或控制操作，用户也可以随时用 `/stdin` 接管交互输入（`Ctrl+C`、`Ctrl+D`、`Esc` 分离），适合交互式脚手架、REPL 或长时服务调试。

### 受控的 SubAgent 任务委派

当任务能够拆分为多个独立子问题时，主 Agent 可以通过 `start_task` 派发拥有独立上下文的 SubAgent：

- **同步与后台协作**：同步子任务可在深度上限内继续委派；根 Agent 还可以在后台启动叶子子任务，完成后结果自动回送到下一轮模型边界。
- **清晰的界面与配额边界**：TUI 为每个子任务呈现独立卡片与状态（`Ctrl+A` 展开/折叠结果），内部思考与工具输出保持隔离，避免污染主上下文；默认限制每父节点 4 个并发、全树 10 个、嵌套 2 层，且在 `ask` 模式下不暴露委派能力。

### Lain Companion：独立观察者与伴随对话

终端右侧可以常驻 **Lain Companion** 侧栏（宽终端自动显示，`/lain` 可按需开关或直接提问）：

- **纯本地、零模型开销的运行观察**：持续采样本机 CPU / GPU / RAM / DISK、Git 状态、运行中的服务以及最近 60 次工具调用的 **Tool Health** 热力图，并根据运行事件切换本地 GIF 表情状态——即使未配置伴随模型也完整可用。
- **按需发起的独立只读对话**：按 `Ctrl+→` 切到侧栏或输入 `/lain <问题>`，Lain 会通过独立模型与只读宿主 RPC 查阅当前会话、工具明细、失败重试和仓库源码。它只观察与回答，**从不参与主 Agent 调度，也不修改文件或执行命令**。

---

## 快速开始

### 环境要求

- **操作系统**：Linux 或 Windows WSL2（需要交互式终端；原生 Windows、macOS 与 WSL1 不支持 Bubblewrap 沙箱）
- **运行依赖**：Python `3.12+`、`bubblewrap`（`bwrap`）
- **模型端点**：任意兼容 OpenAI Chat Completions 或 Responses 接口的模型服务
- **可选**：运行 Node 编写的 stdio MCP 服务或 Node 工具时，推荐使用 Node.js 24 最新补丁版；`ripgrep` 已随 Python 依赖自动安装

Ubuntu / WSL2 下如遇到 user namespace 限制，请先查阅 [沙箱排查](docs/sandbox.md#排查ubuntu--wsl2)。

### 1. 安装

```bash
# Debian / Ubuntu
sudo apt install pipx bubblewrap
pipx ensurepath

# 重新打开终端后安装
pipx install deep-agent-cli
deep-agent --version
```

> 系统默认 Python 低于 3.12 时，可使用 `pipx install --python /path/to/python3.12 deep-agent-cli`。

### 2. 初始化配置

进入目标项目目录首次运行 `deep-agent`，程序会自动生成 `~/.deep-agent/config.yaml`、Skills 目录以及 Lain 默认资源后退出（不会覆盖已有配置）：

```bash
cd ~/projects/my-app
deep-agent
```

> 配置、Skills 与按键配置目录必须位于工作区之外，**不要将家目录或其上层目录作为工作区**。

默认模板使用 `deepseek/deepseek-flash`，在当前 Bash 会话中导出 API Key：

```bash
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
printf '\n'
```

如需接入其他 OpenAI 兼容服务，直接编辑 `~/.deep-agent/config.yaml`：

<details>
<summary>查看最小模型配置示例</summary>

```yaml
llm:
  default: deepseek/deepseek-flash
  timeout: 60
  models:
    deepseek:
      base_url: https://api.deepseek.com/v1
      api_key: ${DEEPSEEK_API_KEY}
      api: chat_completions
      reasoning_efforts: [none, low, high, max]
      models:
        deepseek-flash: {}
```

`api` 必须与服务端点一致（省略时默认为 `responses`，程序不会自动猜测）。完整配置项见 [配置参考](docs/configuration.md)。

</details>

### 3. 启动第一个任务

再次运行 `deep-agent`，先从一个只读探索任务开始：

```text
阅读当前项目，列出程序入口、关键模块和测试命令，并注明对应文件。
暂不修改文件或执行命令。
```

随后尝试一次小范围改动：在默认 `ask` 模式下，涉及文件修改或命令执行的操作都会暂停等待你的确认——按 `Ctrl+R` 展开完整 Diff，按 `F2` 打开或返回审批面板。

<details>
<summary>常用 CLI 命令（恢复会话、升级、源码开发）</summary>

```bash
# 按会话摘要交互式恢复
deep-agent resume

# 按会话 ID 或前缀恢复
deep-agent resume 01a08aae

# 升级
pipx upgrade deep-agent-cli
```

如需在自己的 Python 程序中直接嵌入同一套运行核心，请参考 [Python API](docs/library.md)。

</details>

---

## 模型与扩展

- **多来源模型与推理强度**：在 `config.yaml` 中按来源（provider）组织多个 OpenAI 兼容端点，支持 `chat_completions` 与 `responses` 接口、自定义 `headers`（含 `{session_id}` 占位）、每个模型的上下文窗口、图片输入能力和 `reasoning_efforts`。运行中通过 `/model` 或 `Ctrl+P` / `Alt+P` 随时切换。
- **MCP 集成**：通过 `~/.deep-agent/mcp.json` 与 `/mcp` 管理 stdio 和 Streamable HTTP MCP 服务，支持 Bearer Token 与 OAuth 认证。所有 MCP 连接都在宿主进程中显式管理，详见 [MCP 集成](docs/mcp.md)。
- **Skills 与项目级指令**：从项目根读取 `AGENTS.md` 作为项目级行为约束；通过 `~/.deep-agent/skills/` 与项目内 Skills 扩展可复用工作流，在终端中用 `/skill` 快速选择并注入当前任务。

---

## 执行与安全

Deep Agent CLI 将**审批策略**、**沙箱隔离**与**凭据管理**拆分为三条独立边界：

| 维度 | 机制 | 说明 |
|---|---|---|
| **权限模式** | `ask` / `auto` / `allow` | `ask` 对副作用工具逐次审批；`auto` 结合规则、Flash 审查与人工兜底；`allow` 免审批但仅允许在已验证沙箱下启用。运行中可通过 `/permission` 切换。 |
| **沙箱隔离** | Bubblewrap (`restricted` / `full-access`) | 命令默认运行在无网、隔离 HOME 与按需挂载的持久沙箱中；启动自检失败时默认拒绝回落到宿主执行。 |
| **凭据边界** | `SecretStore` + `StreamingSecretRedactor` | 通过 `request_human_input` 或 `/secrets` 以掩码方式录入凭据；Agent 仅可见引用名，明文留在宿主内存或 Fernet 加密存储中，按作用域注入并在输出、日志与审批面板中做精确脱敏。 |

安全模型存在明确边界：文件工具、Web 工具与 MCP 运行在宿主进程而非 Bubblewrap 内部；输出脱敏仅针对已知凭据做精确匹配替换；`sudo` 提权在沙箱内保持禁用。完整说明见 [沙箱与权限](docs/sandbox.md) 与 [安全凭据管理](docs/secrets.md)。

---

## 架构概览

<div align="center">
  <img src="assets/readme/runtime-map.svg" alt="Deep Agent CLI 产品架构概览" width="960" />
</div>

主流程保持清晰的单向分层：Terminal UI 与 Python API 共同驱动 `AgentRunner`，运行状态通过 `RunEvent` 事件流回传界面与只读观察者；模型调用、工具执行、SubAgent 派发与 Bubblewrap 沙箱各司其职。更深入的分层设计与模块边界见 [系统架构](docs/architecture.md) 与 [领域设计](docs/domain-design.md)。

---

## 文档导航

完整目录与按角色的阅读路线见 **[文档总览](docs/README.md)**。

| 类别 | 文档 | 说明 |
|---|---|---|
| **使用与配置** | [配置参考](docs/configuration.md) | 模型来源、接口类型、沙箱挂载、UI、快捷键与环境变量 |
| | [终端交互指南](docs/tui.md) | 斜杠命令、快捷键、队列介入、交互面板、图片与会话管理 |
| | [Lain Companion](docs/companion.md) | 本地监控、Tool Health 热力图、只读对话与 GIF 渲染器 |
| **机制与安全** | [沙箱与权限](docs/sandbox.md) | Bubblewrap 隔离、网络边界、Ask/Auto/Allow 与 WSL2 排查 |
| | [交互式命令执行](docs/execution.md) | 持久 Worker、Pipe/PTY、前后台 `write_stdin` / `/stdin` 与代码搜索 |
| | [安全凭据管理](docs/secrets.md) | 掩码录入、作用域隔离、Fernet 持久化、MCP 消费绑定与流式脱敏 |
| | [SubAgent 委派](docs/subagents.md) | 同步与后台委派、并发配额、深度限制与隔离摘要日志 |
| | [MCP 集成](docs/mcp.md) | `mcp.json` 配置、stdio/HTTP/OAuth、身份展示与运行边界 |
| **架构与开发** | [系统架构](docs/architecture.md) | 运行时分层、图装配、工具注册表、中间件与事件流 |
| | [Python API](docs/library.md) | `AgentRunner`、`create_agent`、中断处理与自定义工具 |
| | [开发与测试](docs/development.md) | 本地开发环境、pytest 标记、沙箱测试与贡献规范 |

---

## License

基于 [MIT License](LICENSE) 开源。
