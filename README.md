# deep-agent-cli

`deep-agent` 是面向 Linux 和 WSL2 的终端 Coding Agent，基于 [Deep Agents](https://github.com/langchain-ai/deepagents) 和 LangGraph 构建，支持 OpenAI 兼容模型服务，通过 [Bubblewrap](https://github.com/containers/bubblewrap) 隔离命令执行。工作区默认为启动目录，也可通过配置指定。

当前版本为 0.1.6，处于早期开发阶段。稳定版发布前，配置格式、会话存储格式和 Python API 可能出现不兼容变更。

## 功能

- **代码与文件**：目录浏览、读取、编辑、写入、删除，以及默认正则搜索。
- **命令执行与审批**：使用会话内的持久沙箱；默认 `ask` 模式在命令执行、文件变更和 Web 搜索前等待审批。
- **模型与上下文**：支持 Chat Completions / Responses、流式思考与回答、模型和推理强度切换、自动与手动上下文压缩。
- **交互控制**：运行中追加指令、排队后续任务、暂停、取消，以及结构化人工输入。
- **会话与附件**：按工作区保存 SQLite 会话，支持恢复；具备图片输入能力的模型可接收图片附件。
- **项目扩展**：加载 `~/.deep-agent/skills/` 中的 Skills 和工作区根目录的 `AGENTS.md`；配置 Tavily key 后提供 `web_search`。
- **HTTP 服务**：可选服务复用 Runner，提供文字任务、SSE、审批、人工输入与恢复。接口范围见 [HTTP 服务](docs/server.md)。

## 运行要求

- Linux 或 WSL2；原生 Windows、macOS 和 WSL1 不支持 Bubblewrap 沙箱。
- Python 3.12+；CI 覆盖 Python 3.12 和 3.13。
- Bubblewrap 及可用的非特权 user namespace；Ubuntu / WSL2 的限制与处理见 [沙箱排查](docs/sandbox.md#排查ubuntu--wsl2)。
- 支持 Chat Completions 或 Responses 的模型端点及相应凭据。
- CLI 需要交互式终端；非交互接入使用 [HTTP 服务](docs/server.md) 或 [Python API](docs/library.md)。

ripgrep（`rg`）由项目的 `ripgrep-bin` Python 依赖自动安装，无需默认执行系统安装步骤。定位顺序为当前 Python 环境 scripts 目录、系统 PATH、Python 正则回退；运行时不自动下载二进制或修改 PATH。备选安装与验证见 [正则搜索](docs/search.md)。

## 安装

Debian / Ubuntu：

```bash
sudo apt install pipx bubblewrap
pipx ensurepath
# 重新打开终端后安装
pipx install deep-agent-cli
deep-agent --version
```

系统默认 Python 低于 3.12 时，可指定已安装的解释器，不替换发行版的 `/usr/bin/python3`：

```bash
pipx install --python /path/to/python3.12 deep-agent-cli
```

也可使用虚拟环境：

```bash
python3.12 -m venv ~/.venvs/deep-agent
~/.venvs/deep-agent/bin/pip install deep-agent-cli
~/.venvs/deep-agent/bin/deep-agent --version
```

升级现有 pipx 安装：

```bash
pipx upgrade deep-agent-cli
```

## 快速开始

### 1. 生成配置

```bash
deep-agent
```

未指定 `DEEP_AGENT_CONFIG` 且默认配置不存在时，首次运行创建 `~/.deep-agent/config.yaml` 和 Skills 目录，然后退出。已存在的配置不会被模板覆盖。

### 2. 配置模型

在 `~/.deep-agent/config.yaml` 中配置模型端点。以下示例使用本地 Chat Completions 服务，`local-model` 应与端点提供的模型 ID 一致：

```yaml
llm:
  default: local/local-model
  models:
    local:
      base_url: http://localhost:8000/v1
      api_key: sk-local
      api: chat_completions
      models:
        local-model:
          input: [text]
```

远程端点可通过 `api_key: ${MODEL_API_KEY}` 引用环境变量。仅声明端点实际支持的输入模态、上下文窗口和推理强度；`api` 省略时默认为 `responses`，不会按模型名或 URL 自动判断协议。完整字段与服务商示例见 [配置参考](docs/configuration.md)。

### 3. 在项目目录启动

```bash
cd ~/projects/my-app
deep-agent
```

启动时检查配置与沙箱。默认在沙箱不可用时拒绝启动；宿主执行回退需要显式授权，见 [沙箱与权限](docs/sandbox.md)。主配置、Skills 和按键配置目录必须位于工作区之外，因此不能将家目录或其上层目录作为工作区。

终端界面接受自然语言任务，以 `/` 开头的输入用于调用 CLI 命令。

### 4. 恢复会话

```bash
deep-agent resume           # 选择已保存的会话
deep-agent resume 01a08aae   # 替换为已有会话的 ID 或前缀
```

恢复加载会话状态，不自动继续未完成任务。取消不会回滚已经发生的文件修改或外部副作用，恢复后需核对实际状态。

## 常用操作

| 操作 | 用途 |
|---|---|
| `/help`、`/status` | 查看帮助、模型、权限和执行状态 |
| `/model` | 选择模型与推理强度 |
| `/permission ask\|allow` | 切换审批模式与后续命令的网络策略 |
| `/resume`、`/new` | 切换或新建会话 |
| `/skill` | 选择 Skill 并插入输入框 |
| `/image <路径>` | 附加图片 |
| `/compact` | 手动压缩上下文 |
| `Enter` / `Alt+Enter` | 提交任务或运行中追加指令 / 排队后续任务 |
| `Esc` | 请求取消当前任务 |
| `F2` | 打开待处理的审批或人工输入面板 |
| `Ctrl+O` / `Ctrl+T` | 展开工具详情 / 思考内容 |
| `/quit` | 退出 |

完整命令、按键、显示和取消行为见 [终端界面](docs/tui.md)。HTTP 取消为异步操作，以最终 Run 状态及 `run_finished` 事件为准，见 [服务接口](docs/server.md)。

## 正则搜索

`grep` 默认使用正则表达式；搜索包含正则元字符的普通文本时，设置 `fixed_string=true`：

```text
grep(pattern="def (run|execute)\\(", output_mode="content", glob="*.py")
grep(pattern="config.yaml", fixed_string=true)
```

旧版 grep 默认按字面量匹配，包含 `.`, `*`, `+`, `[` 等字符的表达式需要注意语义变化。本地工具支持 `files_with_matches`、`content`、`count` 三种模式，结果与执行时间均有上限。Python 回退与 rg 的正则和 ignore 行为存在差异；非本地自定义 Backend 保留自身接口。

rg 定位与验证、备选系统安装、组合/否定 Glob、忽略规则和输出限制见 [正则搜索](docs/search.md)。

## 使用 Skills

每个 Skill 位于 `~/.deep-agent/skills/<技能目录>/`，入口为 `SKILL.md`，YAML 头部包含 `name` 和 `description`。缺少有效元数据的目录不进入技能列表。

`/skill` 选择后在输入框插入技能块，可追加任务说明：

```text
[Skill: playwright-cli] 检查当前项目的登录流程并记录截图。
```

Skills 目录以只读方式映射到 `/skills`，模型按需读取入口与参考文件。Skill 提供操作说明，不负责安装执行工具；Node、Playwright、浏览器等需预先安装，并通过沙箱配置暴露。[Playwright CLI 官方安装](https://github.com/microsoft/playwright-cli#installing-skills) 生成的 `playwright-cli` 技能目录需复制到 `~/.deep-agent/skills/`。本项目的挂载与有头浏览器配置见 [沙箱工具链](docs/sandbox.md#playwright-与有头-x11)，选择器行为见 [终端界面](docs/tui.md)。

## 安全边界与限制

- **审批与隔离独立**：`ask` 默认审批命令、Web 搜索及文件变更；`allow` 仅支持沙箱模式，免审批且所有 execute 使用有网实例。切换权限不停止此前的后台进程。
- **路径与挂载**：普通文件工具在宿主进程执行，访问受 Backend 路径约束；命令在 Bubblewrap 内访问授权挂载。工作区内的 `.git`、`.env` 等也在可访问范围内，Skills 只读。额外挂载会扩大访问范围。
- **网络**：`ask` 下 execute 默认无直接 IP 网络，可为单条命令声明 `network=true` 并审批；无网命名空间不阻断通过共享 Unix socket、FIFO 或文件实现的一切间接通信。模型与 Tavily 请求在宿主发送，不受该命令网络策略约束。
- **资源与生命周期**：没有 CPU、内存、磁盘或进程数硬配额。会话内按需复用有网/无网沙箱，模型和权限切换不重建实例；会话切换及关闭时清理。
- **本地状态**：同一会话同时只允许一个进程持有；工作区可能生成 `.deep-agent/` 命令日志和 `.deepagents/` 压缩历史，程序不修改 `.gitignore`。
- **客户端限制**：模型和 Tavily 请求不读取代理环境变量；剪贴板图片仅支持 WSL，其他环境通过文件添加图片；当前不提供 MCP 或子 Agent。

隔离范围、环境变量、工具链挂载、宿主回退及排查步骤以 [沙箱与权限](docs/sandbox.md) 为准。

## 文档

| 文档 | 内容 |
|---|---|
| [配置参考](docs/configuration.md) | 模型、推理强度、Web 搜索、路径与环境变量 |
| [终端界面](docs/tui.md) | 命令、快捷键、图片、Skills 和会话操作 |
| [正则搜索](docs/search.md) | rg 安装定位、搜索模式、Glob、ignore 和结果限制 |
| [沙箱与权限](docs/sandbox.md) | 隔离、网络、日志、工具链和系统排查 |
| [HTTP 服务](docs/server.md) | 部署、接口、审批、人工输入、SSE 与恢复 |
| [Python API](docs/library.md) | AgentRunner、事件、自定义工具与 Backend |
| [架构](docs/architecture.md) | Harness 分层、运行边界、Middleware 和持久化 |
| [开发](docs/development.md) | 测试、冒烟、CI 与发布 |

## 开发

```bash
git clone https://github.com/ADaozz/deep_agent_cli.git
cd deep_agent_cli
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

自动化测试不调用真实模型；真实模型冒烟需单独配置和运行。环境要求与验证方法见 [开发文档](docs/development.md)。

## 许可证

[MIT](LICENSE)
