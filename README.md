<div align="center">

# deep-agent-cli

**在终端中理解代码、审阅修改、运行命令。**

Linux / WSL2 · Python 3.12+<br>
OpenAI 兼容接口 · Bubblewrap

[快速开始](#快速开始) · [常用操作](#常用操作) · [架构与安全](#架构与安全) · [文档](#文档)

</div>

<p align="center">
  <img src="终端.png" width="760" alt="deep-agent 真实终端空闲界面，底栏显示工作区、模型来源、SANDBOXED 和 perm:ask。">
</p>

`deep-agent` 是基于 [Deep Agents](https://github.com/langchain-ai/deepagents) 和 LangGraph 的终端 Coding Agent。它在当前项目中工作，默认在文件变更、命令执行和网页搜索前请求批准，通过 [Bubblewrap](https://github.com/containers/bubblewrap) 隔离命令执行。

在项目目录运行 `deep-agent` 进入交互式终端，用 `deep-agent resume` 恢复已有会话。要在其他 Python 程序里复用同一套运行核心，使用 [Python API](docs/library.md)。

> 当前版本 **0.1.8**，处于早期开发阶段。稳定版前，配置、会话存储格式和 Python API 可能出现不兼容变更。

## 能做什么

- **查代码、改文件** — 浏览、读取、正则搜索、编辑和删除；`Ctrl+R` 审阅完整改动，`F2` 处理审批。
- **运行项目命令** — 实时查看输出，会话内复用持久沙箱；默认审批、隔离直接 IP 网络。
- **随时介入任务** — 追加指令、排队后续任务、暂停、取消，以及结构化人工输入。
- **保留工作上下文** — SQLite 会话恢复、模型与推理强度切换、上下文压缩；支持图片输入的模型可接收附件。

支持通过 Skills 和 `AGENTS.md` 扩展项目指令。

## 快速开始

需要 **Linux / WSL2、Python 3.12+、Bubblewrap**，以及支持 Chat Completions 或 Responses 的模型端点。CLI 需要交互式终端；原生 Windows、macOS 和 WSL1 不支持这套沙箱。Ubuntu / WSL2 的 user namespace 限制见 [沙箱排查](docs/sandbox.md#排查ubuntu--wsl2)。

### 1. 安装

Debian / Ubuntu：

```bash
sudo apt install pipx bubblewrap
pipx ensurepath
# 重新打开终端后安装
pipx install deep-agent-cli
deep-agent --version
```

系统默认 Python 低于 3.12 时，使用 `pipx install --python /path/to/python3.12 deep-agent-cli` 指定已安装的解释器。ripgrep 随 Python 依赖安装，无需另行安装。

### 2. 初始化与配置

从项目目录启动，首次运行会创建 `~/.deep-agent/config.yaml` 和 Skills 目录，然后退出。已有配置不会被覆盖；设置了 `DEEP_AGENT_CONFIG` 时使用指定配置。

```bash
cd ~/projects/my-app  # 替换为你的项目目录
deep-agent
```

配置、Skills 和按键配置目录必须位于工作区之外，**不要将家目录或其上层目录作为工作区**。

默认模板使用 `token-plan/qwen3.8-flash`。在启动 Agent 的同一个 Bash 终端中输入对应密钥：

```bash
read -rsp 'Token Plan API key: ' TOKEN_PLAN_API_KEY
export TOKEN_PLAN_API_KEY
printf '\n'
```

使用其他模型服务时，编辑 `~/.deep-agent/config.yaml`，调整端点、密钥、模型 ID 及能力配置。`api` 必须与端点匹配；省略时默认为 `responses`，不会自动判断。

<details>
<summary>查看模型配置示例</summary>

以下与默认模板一致；`llm.default` 使用“来源/模型”标识，模型项继承来源级字段，并可单独覆盖。

```yaml
llm:
  default: token-plan/qwen3.8-flash
  models:
    token-plan:
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
      api_key: ${TOKEN_PLAN_API_KEY}
      api: chat_completions
      reasoning_efforts: [none, low, medium, xhigh]
      models:
        qwen3.8-flash:
          input: [text, image]
          context_window: 1m
```

端点、上下文窗口、输入模态和推理强度取决于所选服务，完整说明见 [配置参考](docs/configuration.md)。

</details>

### 3. 开始第一个任务

在项目目录再次运行 `deep-agent`，输入：

```text
阅读当前项目，列出程序入口、关键模块和测试命令，并注明对应文件。
暂不修改文件或执行命令。
```

获得项目概览后，可以继续要求一次小范围修改。默认 `ask` 下，文件变更会等待审批：`Ctrl+R` 查看改动，`F2` 打开审批面板。启动检查失败时默认拒绝进入宿主执行，见 [沙箱与权限](docs/sandbox.md)。

<details>
<summary>恢复会话、升级与虚拟环境安装</summary>

```bash
deep-agent resume           # 选择已保存的会话
deep-agent resume 01a08aae   # 换成已有会话的 ID 或前缀
pipx upgrade deep-agent-cli
```

恢复只加载状态，不自动继续未完成任务；旧数据库格式不自动迁移。取消不回滚已发生的文件修改或外部副作用。

不使用 pipx 时，也可安装到虚拟环境：

```bash
python3.12 -m venv ~/.venvs/deep-agent
~/.venvs/deep-agent/bin/pip install deep-agent-cli
~/.venvs/deep-agent/bin/deep-agent --version
```

</details>

## 常用操作

| 操作 | 用途 |
|---|---|
| `/model` | 切换模型与推理强度 |
| `Ctrl+R` / `F2` | 审阅文件改动 / 处理审批与提问 |
| `Enter` / `Alt+Enter` | 提交或运行中追加指令 / 排队后续任务 |
| `Esc` | 请求取消当前任务 |
| `Ctrl+O` / `Ctrl+T` | 展开工具输出 / 思考内容 |
| `/resume` / `/new` | 恢复或新建会话 |
| `/help` / `/status` | 查看完整操作与当前状态 |

更多按键、图片附件、队列、Skills 和显示设置见 [终端界面](docs/tui.md)。

## 架构与安全

**审批决定是否执行，隔离决定命令能访问哪些资源。** 文件工具和命令走不同通路：

- **文件工具 → 宿主进程**：Backend 将 `/workspace` 映射到工作区，`/skills` 只读，拒绝其他虚拟路径。
- **命令 → Bubblewrap**：`execute` 经沙箱池运行，会话内按需复用有网、无网 Worker。模型与 Tavily 请求从宿主发送，不受命令网络策略约束。

<details>
<summary>展开运行架构图</summary>

![TUI 进入共享的 AgentRunner 和 Deep Agents / LangGraph；普通文件工具在宿主进程访问授权路径，execute 经 SandboxPool 进入 Bubblewrap Worker。](assets/readme/runtime-map.svg)

SQLite 保存会话状态。`grep` 免审批且独立于 `execute`；沙箱模式下有 rg 时复用沙箱池，否则使用宿主 Python 正则回退。完整分层与生命周期见 [架构文档](docs/architecture.md)。

</details>

| 模式 | 审批 | 命令网络 |
|---|---|---|
| **沙箱 + `ask`**（默认） | 文件变更、命令、网页搜索需批准 | 默认隔离直接 IP 网络；`network=true` 获批后共享宿主网络 |
| 沙箱 + `allow` | 所有工具免审批 | 所有 execute 共享宿主网络 |
| 宿主回退 + `ask` | 显式授权回退后，仍需审批 | 以宿主用户权限执行，无沙箱网络隔离 |

只读文件工具与 `grep` 免审批；`allow` 仅限沙箱模式。**工作区内的 `.env`、`.git` 同样可访问，取消不回滚已发生的副作用。**

<details>
<summary>更多边界与当前限制</summary>

- **网络与进程**：无网命名空间不阻断通过共享 Unix socket、FIFO 或文件实现的间接通信。有网实例可访问互联网、localhost 和局域网；切回 `ask` 不停止此前的后台进程。会话切换及关闭时清理沙箱。
- **挂载与资源**：额外挂载会扩大访问范围；没有 CPU、内存、磁盘或进程数硬配额。Skills、`AGENTS.md` 和提示词不是操作系统安全边界。
- **本地状态**：同一会话只允许一个进程持有；工作区可能产生 `.deep-agent/` 命令日志和 `.deepagents/` 压缩历史，程序不修改 `.gitignore`。
- **客户端**：模型与 Tavily 请求忽略代理环境变量；剪贴板图片仅支持 WSL，其他环境通过文件添加；当前不提供 MCP 或子 Agent。

完整隔离范围、环境变量与宿主回退说明见 [沙箱与权限](docs/sandbox.md)。

</details>

## 文档

- **使用**：[配置参考](docs/configuration.md) · [终端界面](docs/tui.md) · [正则搜索](docs/search.md)
- **机制**：[沙箱与权限](docs/sandbox.md) · [架构设计](docs/architecture.md)
- **集成**：[Python API](docs/library.md) · [开发指南](docs/development.md)

<details>
<summary>扩展 Skills 与网页搜索</summary>

Skills 位于 `~/.deep-agent/skills/<技能目录>/SKILL.md`，使用 `/skill` 选择；配置 Tavily key 后启用 `web_search`。外部工具与浏览器需预先安装并通过沙箱挂载，见 [Skills 用法](docs/tui.md#skills) 与 [沙箱工具链](docs/sandbox.md#playwright-与有头-x11)。

</details>

<details>
<summary>从源码开发</summary>

```bash
git clone https://github.com/ADaozz/deep_agent_cli.git
cd deep_agent_cli
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

自动化测试不调用真实模型；CI 覆盖 Python 3.12 和 3.13。更多说明见 [开发指南](docs/development.md)。

</details>

[MIT License](LICENSE)
