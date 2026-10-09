# deep-agent-cli

`deep-agent` 是一个在终端中运行的 Coding Agent，支持 Linux 和 WSL2。它把启动时所在的目录作为工作区，调用你配置的 OpenAI 兼容模型服务，并在 [Bubblewrap](https://github.com/containers/bubblewrap) 沙箱中执行命令。Agent 运行时基于 [Deep Agents](https://github.com/langchain-ai/deepagents) 和 LangGraph 构建。

当前版本 0.1.1，处于早期开发阶段。第一个稳定版之前，配置格式、会话存储格式和 Python API 都可能出现不兼容变更。

## 功能

- **文件操作**：在工作区中列目录、读、写、编辑、搜索、删除文件。
- **命令执行**：在 Bubblewrap 沙箱中运行 shell 命令，默认没有网络；输出实时显示。
- **审批**：默认模式下，运行命令、写入、编辑、删除文件前都会请求你确认。
- **会话**：按工作区保存在本地 SQLite 中，可以随时恢复。
- **多模型**：在配置文件中定义多个模型，运行中切换模型和推理强度。支持 Chat Completions 和 Responses 两种接口。
- **流式输出**：思考内容和回答都流式显示。
- **运行中干预**：任务执行时可以追加指令、排队后续任务或取消。
- **图片输入**：向声明支持图片的模型发送图片。
- **结构化提问**：Agent 需要你做决定时，会弹出单选、多选、是/否或文本输入。
- **上下文压缩**：接近模型上下文窗口时自动摘要旧对话，也可以手动压缩。
- **可选扩展**：配置 Tavily API key 后可以搜索公开网页；从 `~/.deep-agent/skills/` 加载 Skills；读取工作区根目录的 `AGENTS.md` 作为项目说明。

## 运行要求

- Linux 或 WSL2。WSL1、原生 Windows 和 macOS 无法使用沙箱。
- Python 3.12 或更高版本（CI 测试 3.12 和 3.13）。
- Bubblewrap，并且系统允许非特权 user namespace。Ubuntu 24.04 默认限制这一点，处理方法见 [沙箱排查](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/sandbox.md#排查ubuntu--wsl2)。
- 一个 OpenAI 兼容的模型服务（本地或远程）及其 API key。

## 安装

Debian / Ubuntu：

```bash
sudo apt install pipx bubblewrap
pipx ensurepath          # 之后重新打开终端
pipx install deep-agent-cli
deep-agent --version
```

系统 Python 低于 3.12 时，安装一个新版本的 Python，然后指定解释器（不要替换发行版自带的 `/usr/bin/python3`）：

```bash
pipx install --python /path/to/python3.12 deep-agent-cli
```

也可以装进虚拟环境：`python3 -m venv ~/.venvs/deep-agent && ~/.venvs/deep-agent/bin/pip install deep-agent-cli`。

## 快速开始

**1. 生成配置文件**

```bash
deep-agent
```

首次运行会创建 `~/.deep-agent/config.yaml` 和 `~/.deep-agent/skills/`，然后退出。

**2. 配置模型**

编辑 `~/.deep-agent/config.yaml`，把 `llm` 部分改成你的服务：

```yaml
llm:
  default: local/main                  # 启动时使用的模型：来源/模型键
  models:
    local:                             # 来源名，自定义
      base_url: http://localhost:8000/v1
      api_key: ${MY_API_KEY}           # 也可以直接写密钥
      provider: openai-compatible      # 端点支持 Responses API 时可改为 qwen-responses
      stream_usage: true
      models:
        main:                          # 模型键，自定义
          model: your-model-name       # 发给服务端的模型名
          context_window: 128k
          input: [text]                # 支持图片时写 [text, image]
```

`provider` 未填写时默认为 `qwen-responses`（Responses API），只提供 Chat Completions 的服务需要显式写 `openai-compatible`。其他字段、推理强度和服务商示例见 [配置参考](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/configuration.md)。

**3. 在项目目录中启动**

```bash
cd ~/projects/my-app
deep-agent
```

启动时会先检查沙箱是否可用；不可用时程序给出诊断信息并拒绝启动。界面中直接输入任务即可，以 `/` 开头的输入是命令。

**4. 恢复会话**

```bash
deep-agent resume            # 选择一个已保存的会话
deep-agent resume 01a08aae   # 按 id 或前缀恢复
```

恢复只加载会话状态，不会自动继续执行未完成的任务。

## 常用操作

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令和快捷键 |
| `/status` | 当前模型、权限模式、沙箱状态 |
| `/model` | 选择模型和推理强度 |
| `/permission ask\|allow` | 切换审批模式 |
| `/resume`、`/new` | 切换到已有会话、新建会话 |
| `/compact` | 手动压缩上下文 |
| `/image <路径>` | 为下一条消息附加图片 |
| `/quit` | 退出 |

| 按键 | 作用 |
|---|---|
| `Enter` | 提交；任务运行中提交为追加指令 |
| `Ctrl+J` | 换行 |
| `Alt+Enter` | 排队，当前任务结束后执行 |
| `Esc` | 取消当前任务 |
| `F2` | 重新打开待处理的审批或提问 |
| `Ctrl+O` / `Ctrl+T` | 展开工具详情 / 思考内容 |
| `Ctrl+R` | 查看文件改动 |
| `Ctrl+P` | 切换到下一个模型 |
| `Ctrl+C` | 清空输入；连按两次退出 |

完整的命令、按键和界面行为见 [终端界面](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/tui.md)。

## 安全模型

| | 行为 |
|---|---|
| 命令能访问的文件 | 工作区（读写，在沙箱中为 `/workspace`）、`~/.deep-agent/skills/`（只读）、系统的 `/usr`、`/bin`、`/lib`（只读）。家目录的其余部分不可见 |
| 网络 | 命令默认无网络。模型可以为单条命令申请网络，此时它能访问互联网、`localhost` 和局域网 |
| 环境变量 | 只传入 `LANG`、`TERM` 等少量变量，其余需在配置中显式列出 |
| `ask` 模式（默认） | 运行命令、写入、编辑、删除文件前需要确认 |
| `allow` 模式 | 所有工具自动批准，所有命令都开放网络。仅在沙箱可用时可开启，开启时需输入 `ALLOW` |
| 沙箱不可用 | 拒绝启动。只有在配置中设置 `sandbox.allow_unsandboxed: true`，或在启动提示中输入 `UNSANDBOXED`，命令才会以当前用户身份直接在宿主机上运行，此时只能使用 `ask` 模式 |

细节见 [沙箱与权限](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/sandbox.md)。

## 已知限制

- 沙箱不限制 CPU、内存和磁盘用量。
- 沙箱中只有系统目录下的程序。安装在 `~/.local`、`~/.nvm`、`/opt` 等位置的工具链默认不可用，需要在配置中添加只读挂载。
- 文件工具在 `deep-agent` 进程中直接读写宿主文件，不经过沙箱，路径限定在工作区和 Skills 目录内。工作区里的所有内容 Agent 都能读写，包括 `.git/` 和 `.env`。
- 程序会在工作区中创建 `.deep-agent/`（命令输出超限时的日志）和 `.deepagents/`（上下文压缩时保存的历史），但不会修改 `.gitignore`。
- 不能在家目录或其上层目录启动，因为配置目录 `~/.deep-agent` 必须位于工作区之外。
- 只能在交互式终端中使用，没有非交互或批处理模式。
- 模型请求和 Tavily 请求不读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等代理环境变量。
- 从剪贴板粘贴图片只支持 WSL；其他环境需要通过文件路径添加图片。
- 推理强度的可选值需要按服务商文档手动配置，程序不会自动探测。
- 不支持 MCP，也没有子 Agent。
- 同一个会话同一时间只能被一个进程打开。

## 文档

- [配置参考](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/configuration.md)：所有配置项、模型与推理强度、Web 搜索、按键、环境变量
- [沙箱与权限](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/sandbox.md)：隔离范围、网络、日志、Ubuntu / WSL2 排查
- [终端界面](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/tui.md)：命令、快捷键、图片、会话、显示规则
- [作为 Python 库使用](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/library.md)：`AgentRunner`、事件、自定义工具
- [架构](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/architecture.md)：分层、中间件、会话存储、上下文压缩
- [开发](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/development.md)：测试、冒烟脚本、CI 与发布

## 开发

```bash
git clone https://github.com/ADaozz/deep_agent_cli.git
cd deep_agent_cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

自动测试不调用真实模型。欢迎提交 Issue 和 Pull Request；修改行为时请附带测试。更多说明见 [开发文档](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/development.md)。

## 许可证

[MIT](https://github.com/ADaozz/deep_agent_cli/blob/main/LICENSE)
