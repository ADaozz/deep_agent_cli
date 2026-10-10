# deep-agent-cli

`deep-agent` 是一个在终端中运行的 Coding Agent，支持 Linux 和 WSL2。它把启动时所在的目录作为工作区，调用你配置的 OpenAI 兼容模型服务，并在 [Bubblewrap](https://github.com/containers/bubblewrap) 沙箱中执行命令。Agent 运行时基于 [Deep Agents](https://github.com/langchain-ai/deepagents) 和 LangGraph 构建。

当前版本 0.1.3，处于早期开发阶段。第一个稳定版之前，配置格式、会话存储格式和 Python API 都可能出现不兼容变更。

## 功能

- **文件操作**：在工作区中列目录、读、写、编辑、搜索、删除文件。
- **命令执行**：在 Bubblewrap 沙箱中运行 shell 命令，默认没有网络；输出实时显示。
- **审批**：默认 `ask` 模式下，每次运行命令、Web 搜索、写入、编辑、删除文件前都会请求你确认。
- **会话**：按工作区保存在本地 SQLite 中，可以随时恢复。
- **多模型**：在配置文件中定义多个模型，运行中切换模型和推理强度。支持 Chat Completions 和 Responses 两种接口。
- **流式输出**：思考内容和回答都流式显示；思考区默认保留按终端宽度折行后的末尾 5 行正文，`Ctrl+T` 展开。
- **运行中干预**：任务执行时可以追加指令、排队后续任务或取消。
- **图片输入**：向声明支持图片的模型发送图片。
- **结构化提问**：Agent 需要你做决定时，会弹出单选、多选、是/否或文本输入。
- **上下文压缩**：接近模型上下文窗口时自动摘要旧对话，也可以手动压缩。
- **技能**：从 `~/.deep-agent/skills/` 加载 Skills，通过 `/skill` 选择技能并附带任务说明，支持读取技能目录中的参考文件。
- **可选扩展**：配置 Tavily API key 后可以搜索公开网页；读取工作区根目录的 `AGENTS.md` 作为项目说明。

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

模型按「来源」分组。来源下可以写的字段，模型项中也都可以写：模型项省略则继承来源，写了就覆盖。列表字段（`input`、`reasoning_efforts`）是整表替换，不是合并；`reasoning_efforts` 写成 `null` 或 `[]` 会清除继承。

`api` 未填写时默认为 `responses`（Responses API），只提供 Chat Completions 的服务需要显式写 `chat_completions`。

配置只接受 `api: responses/chat_completions`；`provider`、`stream_usage` 已移除，出现时直接报错。Chat Completions 固定请求 usage。右下角 Context 仅显示最近一次 API 返回的输入 token 占用，缺失时显示 `Unknown`，不做估算。

`reasoning_efforts` 需要按服务商文档填写，程序不会自动探测。常见取值包括 `none`、`low`、`medium`、`high`、`xhigh`、`max`，同一模型在不同端点上可能不同。列表非空时，`/model` 只显示这些值，默认选中第一项；空列表或未配置时只显示 `default`，请求不携带强度参数。`default` 是 CLI 选项，不能写进列表。`none` 仅在端点明确支持关闭思考时才写；`low` 仍然会思考。Chat Completions 发送 `reasoning_effort`，Responses API 发送 `reasoning.effort`。

其他字段和服务商示例见 [配置参考](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/configuration.md)。

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
| `/skill` | 选择技能，在输入框插入技能名称块 |
| `/permission ask\|allow` | 切换审批模式 |
| `/resume`、`/new` | 切换到已有会话、新建会话 |
| `/compact` | 手动压缩上下文 |
| `/image <路径>` | 为下一条消息附加图片 |
| `/quit` | 退出 |

输入 `/` 显示命令候选，按连续子串匹配，例如 `/elp` 匹配 `/help`，`/hlp` 不匹配。完整匹配和前缀匹配优先；方向键选择，`Tab` 或 `Enter` 填入，退格后重新计算候选。`/model ` 和 `/permission ` 的参数也支持补全。

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

## 使用 Skills

每个技能放在 `~/.deep-agent/skills/` 下的独立目录中，入口文件为 `SKILL.md`。其 YAML 头部必须包含 `name` 和 `description`；技能列表显示 `name`，缺少有效元数据的目录会被跳过。

### 安装示例：Playwright CLI

准备好 Node.js 和 npm 后，在终端执行以下命令。先使用[微软官方 Playwright CLI](https://github.com/microsoft/playwright-cli#installing-skills) 的安装命令生成 Skill，再复制到本项目使用的目录：

```bash
mkdir -p ~/.deep-agent/skills
cd ~/.deep-agent/skills

# 如果还没安装 CLI
npm install -g @playwright/cli@latest

# 安装官方 Skill 到临时目录
skill_tmp_dir="$(mktemp -d)"
(cd "$skill_tmp_dir" && playwright-cli install --skills=agents)

# 复制到 DeepAgent 的技能目录
cp -a "$skill_tmp_dir/.agents/skills/playwright-cli" ~/.deep-agent/skills/

# 清理临时目录
rm -rf "$skill_tmp_dir"
```

验证安装：

```bash
ls -R ~/.deep-agent/skills/playwright-cli
```

安装后目录结构如下，`references/` 中的参考文档也能读取：

```text
~/.deep-agent/
└── skills/
    └── playwright-cli/
        ├── SKILL.md
        └── references/
            ├── playwright-tests.md
            ├── request-mocking.md
            ├── running-code.md
            ├── session-management.md
            ├── storage-state.md
            ├── test-generation.md
            ├── tracing.md
            ├── video-recording.md
            ├── element-attributes.md
            └── pr-attachments.md
```

### 在输入框中调用技能

在 CLI 中输入 `/skill` 后回车，用上下键选择技能，再回车将 `[Skill: playwright-cli]` 插入输入框。此时可以直接回车调用，也可以追加说明后提交：

```text
[Skill: playwright-cli] 检查当前项目页面的登录流程，并记录截图。
```

选择时按 `Esc` 取消；完整删除输入框中的技能块即可取消本次技能调用。技能列表在每次打开时重新读取目录。发送后的聊天记录仍显示技能名称块和附带说明，恢复会话时也保持这一显示方式。

技能目录以只读方式映射到 `/skills/`。DeepAgent 原生 SkillsMiddleware 加载技能名称和描述；提交时携带所选技能的名称及路径，由模型按需读取 `/skills/playwright-cli/SKILL.md` 和相关参考文件，技能正文不会整段展开到输入框中。

加载技能只提供操作说明。工具链要在宿主机装好，再通过沙箱挂载暴露给命令。沙箱不能 `sudo` 或 `apt-get`，所以不要在里面执行 `npx playwright install chrome`。

`network=false` 使用 Bubblewrap 的独立网络命名空间（`--unshare-net`），限制本次命令及子进程直接访问 IP 网络；不保证完全没有间接联网。例如有网后台服务在共享 `/workspace` 创建 Unix Socket，无网命令通过它请求服务代为联网；共享文件或 FIFO 也可形成类似通路。这是已知隔离边界，不是沙箱逃逸，也不表示本轮已修复。见 [网络边界](docs/sandbox.md#网络)。

### 沙箱里的工具链

首次启动时，[`agent/config.example.yaml`](agent/config.example.yaml) 会复制成 `~/.deep-agent/config.yaml`。其中的 `sandbox` 段已经是默认工具链配置：`PATH` 包含 Node.js 和 Cargo，并挂载下面这些目录。`optional: true` 的源目录在宿主机上不存在时会被跳过，不会阻止启动。

| 宿主机路径 | 沙箱路径 | 权限 | 用途 |
|---|---|---|---|
| `~/.nvm/versions/node/v22.23.2` | `/opt/node` | 只读 | `node`、`npm`、`playwright-cli` |
| `~/.cargo` | `/opt/cargo` | 只读 | Rust 工具链 |
| `/opt/google` | `/opt/google` | 只读 | 系统 Chrome。`playwright-cli open` 使用 `/opt/google/chrome/chrome` |
| `~/.cache/ms-playwright` | `/opt/ms-playwright` | 可写 | CLI 版本对应的 Chromium / Chrome for Testing。环境变量 `PLAYWRIGHT_BROWSERS_PATH` 指向这里 |

Node.js 版本以本机 `~/.nvm/versions/node/` 下的目录名为准，和模板不一致时改 `source`。其他装在 `~/.local`、`/opt` 或家目录里的程序同样处理：用 `readlink -f "$(command -v 工具名)"` 找到落在 `/usr`、`/bin`、`/lib` 之外的目录，按同样格式追加挂载，并把可执行文件所在目录加到 `PATH` 前面。`PATH` 会替换沙箱默认值，末尾保留 `/usr/local/bin:/usr/bin:/bin`。

`playwright-cli open` 默认走系统 Chrome（通常位于 `/opt/google/chrome/chrome`），而 `playwright-cli open --browser=chromium` 选择 CLI 依赖版本对应的缓存 Chromium。没有系统 Chrome 时，先在宿主机查看当前版本的帮助，再安装浏览器：

```bash
playwright-cli install-browser --help
playwright-cli install-browser chromium
```

不要用独立的 `npx playwright install chromium` 代替：它可能解析到另一版 Playwright，下载的浏览器修订号与 CLI 查找的缓存目录不同。命令以实际安装的 CLI 帮助为准；本轮核对的 CLI 0.1.22 支持上述命令（包括 `--dry-run`），不要求锁定项目 Node 或 Playwright 版本。缓存目录可写时，沙箱中的安装和删除会改宿主机上的同一目录；只使用已安装浏览器且需要只读缓存时，先从 `extra_read_write_mounts` 移除该项，再加入 `extra_read_only_mounts`，不要在两个列表中重复配置同一目标。下载浏览器需要网络，由用户在宿主机完成。

Node 本体也可以来自系统 `/usr/bin/node`。若 CLI 和 npm 包安装在自定义 npm prefix（例如 `npm config get prefix` 返回 `~/.local`），必须挂载整个 prefix，而不只是它的 `bin` 目录，再调整 `PATH`。系统 Node 已在默认只读系统目录内，无需 nvm：

```yaml
sandbox:
  extra_read_only_mounts:
    - source: ~/.local  # 替换为本机 npm prefix
      destination: /opt/npm-prefix
  env_set:
    PATH: /opt/npm-prefix/bin:/usr/local/bin:/usr/bin:/bin
    PLAYWRIGHT_BROWSERS_PATH: /opt/ms-playwright
```

此示例只新增 npm prefix 挂载；浏览器缓存沿用默认模板中的 `extra_read_write_mounts`，目标仍为 `/opt/ms-playwright`，不要重复添加。

使用系统 Chrome 时保留 `/opt/google` 的只读挂载。系统目录以外的 Node 本体则需要另行只读挂载，并把其 `bin` 加到 `PATH`。

有头模式还需要把 `DISPLAY` 加入 `sandbox.env_allowlist`，并确保对应 X Server 可访问。已测试的 X11 抽象 Socket 环境中，无网沙箱的网络命名空间无法连接宿主 X Server；当前流程应使用有网沙箱，ask 模式必须声明 `network=true` 并审批。Xauthority 认证可能需要额外挂载认证文件，该场景未验证；Wayland 也不在本轮验证范围。

保留 Bubblewrap 的 `--unshare-ipc`。用户提供的真实机器 A/B 验证表明，在专用 Xvfb 关闭 MIT-SHM 可以稳定渲染，无需添加 Chrome 启动参数：

```bash
Xvfb :2 -screen 0 1600x900x24 -extension MIT-SHM
# 启动 deep-agent 前设置 DISPLAY=:2，并在 env_allowlist 中允许 DISPLAY
```

以上为用户手动准备环境的示例，CLI 不会自动安装 Node、浏览器或 Xvfb。有头渲染必须通过实际 X 窗口截图核验；读取 title、DOM 或看到窗口存在不足以证明渲染成功。详细 A/B 结果和挂载说明见 [docs/sandbox.md](docs/sandbox.md#playwright-与有头-x11)。Playwright CLI 可能生成 `.playwright-cli/`，建议在自己的项目 `.gitignore` 中忽略它；程序不会自动修改 `.gitignore`。

已经存在的 `~/.deep-agent/config.yaml` 不会被模板覆盖。把模板里的 `sandbox.env_set` 和两个挂载列表合并进现有文件，保存后退出并重新启动 `deep-agent`。挂载规则见 [沙箱与权限](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/sandbox.md)。

## 安全模型

| | 行为 |
|---|---|
| 命令能访问的文件 | 工作区（读写，在沙箱中为 `/workspace`）、`~/.deep-agent/skills/`（只读）、系统的 `/usr`、`/bin`、`/lib`（只读）。家目录的其余部分不可见 |
| 网络 | 命令默认无网络。模型可以为单条命令申请网络，此时它能访问互联网、`localhost` 和局域网 |
| 沙箱复用 | 每个会话最多一个无网沙箱和一个有网沙箱，按需启动、跨命令复用（`/tmp` 和后台进程保留），会话结束时销毁。切换模型或权限模式不重建沙箱；`ask` / `allow` 只决定之后的命令要不要审批 |
| 环境变量 | 只传入 `LANG`、`TERM` 等少量变量，其余需在配置中显式列出 |
| `ask` 模式（默认） | 每次运行命令、Web 搜索、写入、编辑、删除文件前需要确认 |
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
