# Deep Agent

一个面向本地开发环境的 AI Coding Agent。

在终端里打开任意项目，就能读改工作区文件、在沙箱里执行命令、流式输出思考与回答，并按工作区记住会话。命令默认无网；在默认的 ask 模式下，写文件、删文件和运行命令前会请求审批。自定义副作用工具需要显式配置审批规则。

![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)
![Linux](https://img.shields.io/badge/os-Linux%20%2F%20WSL2-FCC624?logo=linux&logoColor=black)
![Status](https://img.shields.io/badge/status-active%20development-0E8A16)

它要解决的问题很具体：多数 Coding Agent 要么绑云端 IDE，要么绑一整套控制面。Deep Agent 把 Agent 放进当前目录，用本地 OpenAI 兼容模型跑起来，权限和会话都在你机器上。

和 Claude Code / Codex / 平台托管 Runtime 的差别：

- **本地优先** — 不依赖 Control Plane、Redis、Docker、ToolGateway
- **物理隔离** — Linux Bubblewrap，默认 `--unshare-net`，不是只靠模型“答应不乱来”
- **Runtime 与 TUI 分离** — 终端只是一层皮，同一套 `AgentRunner` 可以接到 SSE 或自己的 UI
- **不 Fork Deep Agents** — 装配入口只有 `create_deep_agent()`

## 核心理念

`deep_agent_cli` 的目标是做一个**简单、可控、可恢复的本地 Coding Agent**，而不是一个构建 Agent 的框架。

- **少造抽象**：优先复用 Deep Agents / LangGraph 已有能力。
- **单一状态源**：项目文件归文件系统，会话上下文与计划归 checkpoint，避免重复状态。
- **安全边界明确**：Bubblewrap 限制能力范围，Permission 控制用户授权，两者互不混淆。
- **恢复不等于继续**：`/resume` 只恢复会话状态，不自动执行未完成任务。F2 只重开 pending overlay。
- **默认安全，显式扩权**：workspace 外资源、网络及高风险操作必须由用户明确开放。
- **按真实需求演进**：没有实际问题，就不提前构建复杂机制。

> 保持它是一个 Agent，而不是一个构建 Agent 的框架。

## Features

- **工作区工具** — `ls` / `read` / `write` / `edit` / `glob` / `grep` / `delete` 访问 `/workspace`；用户 Skills 可在只读 `/skills` 下读取
- **沙箱执行** — 默认隔离网络；`execute(network=true)` 使用宿主网络，可访问互联网、localhost 和局域网
- **权限模式** — `ask` 审批 `execute` 和内置文件写入、删除工具；`allow` 仅 SANDBOXED 可启用（需输入 `ALLOW`）
- **持久会话** — 每个工作区一份 SQLite，`/resume` 恢复最近线程，checkpoint 是恢复依据
- **流式输出** — 思考和回答按增量刷新；`execute` 的 stdout/stderr 原地更新同一个 Tool 块
- **运行中转向** — `Enter` 注入下一条指令，`Esc` 取消并把未发送的内容还原到输入框
- **后续任务** — `Alt+Enter` 排入 Runtime 队列，当前任务完成后由 `AgentRunner` 接续执行
- **多模型** — YAML profile + `/model` / `Ctrl+P`；切换重建 graph，会话保留。本地网关与阿里云百炼 Qwen Token Plan 都已适配
- **图片附件** — Ctrl+V / 路径 / `/image`；checkpoint 只存引用，请求模型时才编码
- **自动上下文压缩** — `create_deep_agent()` 默认带 `SummarizationMiddleware`，上下文接近上限时自动摘要；被挤掉的历史落到工作区，需要时还能再读
- **人工交互** — Agent 缺判断时弹出单选、多选、布尔、单行、多行，不绑特定 UI
- **任务规划** — `write_todos` 使用上游 TodoListMiddleware 更新 LangGraph 的 `todos` state；TUI 从 `values` 状态流替换当前计划，从 checkpoint 恢复计划，不保留历史版本的工具块
- **Skills** — 只读加载 `~/.deep-agent/skills/*/SKILL.md`

自动压缩已由 Deep Agents 的 `SummarizationMiddleware` 处理，不需要额外叠加一层。

## Quick Start

### Requirements

- Python 3.12+
- Linux 或 WSL2（需要 [Bubblewrap](https://github.com/containers/bubblewrap)）
- 一台 OpenAI 兼容推理服务（默认 `http://localhost:8000/v1`；百炼 Qwen Token Plan 的 compatible-mode 端点已适配，见 Configure）

### Installation

```bash
cd deep-agent

sudo apt-get install bubblewrap   # Debian / Ubuntu

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
```

### Configure

首次运行 `deep-agent` 会生成 `~/.deep-agent/config.yaml` 和空的 `~/.deep-agent/skills/`，提示编辑配置后退出。修改 API Key 和端点后，再次运行：

```yaml
llm:
  default: qwen-plus
  models:
    qwen-plus:
      model: qwen3.5-plus
      input: [text, image]
      context_window: 1m
      api_key: sk-your-key
      base_url: http://localhost:8000/v1
    qwen3.6-flash:
      model: qwen3.6-flash
      input: [text, image]
      context_window: 1m
      api_key: sk-your-key
      base_url: http://localhost:8000/v1
```

也可以不改文件，启动时指定：

```bash
export DEEP_AGENT_CONFIG=/path/outside/workspace/config.yaml
```

接阿里云百炼 **Qwen Token Plan** 时用 `compatible-mode` 端点加 `provider: openai-compatible`，并打开 `stream_usage`，否则流式响应不回报 usage，底栏的上下文占用百分比会一直显示未知。profile 里没写的字段继承 `llm` 下的扁平配置，所以多个 Token Plan 模型可以共用一份端点和密钥：

```yaml
llm:
  default: token-plan-qwen3.8-max
  api_key: sk-your-token-plan-key
  base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
  provider: openai-compatible
  stream_usage: true
  models:
    token-plan-qwen3.8-max:
      model: qwen3.8-max
      source: Token Plan
      input: [text, image]
      context_window: 1m
    token-plan-auto:
      model: auto                # 由 Token Plan 侧自动路由
      source: Token Plan
      input: [text]
      context_window: 1m
```

`source` 只是 `/model` 列表、页脚和状态里显示的来源标签，不参与路由；`context_window` 决定自动压缩阈值和占用百分比分母。区域按自己的开通情况替换 `cn-beijing`。密钥只写在 workspace 外的配置里，不要提交进仓库。

### Run

```bash
# 任意项目目录 = Agent 的工作区
export PATH="$PATH:/path/to/deep-agent/bin"
cd ~/projects/my-app
deep-agent
deep-agent resume                 # 列出有内容的会话
deep-agent resume 01a08aae-...   # 按 id 恢复
```

仓库内也可以：

```bash
python examples/run_cli.py
```

没有 `bwrap` 时默认拒绝启动。交互式终端必须完整输入 `UNSANDBOXED` 才降级到宿主机执行；脚本 / 服务必须在配置里写 `sandbox.allow_unsandboxed: true`。

## Usage

进入交互会话后，普通文本就是任务。以 `/` 开头是命令。

```text
/help          命令与快捷键
/status        工作区、沙箱、权限、session
/session       当前线程、创建/更新时间与 SQLite 路径
/new           开一条新线程
/resume        列出有内容的会话并选择恢复
/resume a1b2   按 id 前缀切换 session
/model         打开模型选择器
/model qwen3   按 id 前缀切换
/compact       达到手动压缩门槛后摘要旧对话；未达到时显示当前占用百分比
/permission ask|allow   # allow 仅 SANDBOXED；UNSANDBOXED / CUSTOM 只有 ask
/image clipboard | <path> | clear
/pause         在下一个模型安全点暂停
/quit
```

常用按键：

| 操作 | 按键 |
|------|------|
| 提交；运行中注入 steering | `Enter` |
| 命令候选 | 输入 `/` 后用方向键选择，`Tab` / `Enter` 填入，再按 `Enter` 执行 |
| 换行 | `Ctrl+J` |
| 排到本轮结束后再问 | `Alt+Enter` |
| 取消并还原未应用内容 | `Esc` |
| 切换模型 | `Ctrl+P` / `Alt+P` |
| 粘贴图片 | `Ctrl+V` / `Alt+V` |
| 展开已产生的工具输出 / Thinking | `Ctrl+O` / `Ctrl+T` |
| 审查即将批准的 edit/write/delete | `Ctrl+R` |
| 重开 pending 交互 | `F2` |
| 取回未应用的 steering | `Alt+Up` |
| 回看历史输出 | 滚轮 / `PgUp` / `PgDn`，`Ctrl+Home` 到顶，`Ctrl+End` 回到底部跟随 |
| 拖动滚动条 | 在最右列按下并拖动；点击即跳到对应位置 |

底栏第三行显示当前工作区的 git 分支与改动文件数（每 5 秒后台刷新一次 `git status`，非 git 目录时留空），右侧是 `context_window` 对应的上下文占用百分比，数据来自模型最近一次返回的 token usage。

作为库使用：

```python
from agent import AgentRunner, create_agent

prepared = create_agent()
runner = AgentRunner(prepared=prepared, on_delta=print, on_event=print)
result = runner.invoke("列出 /workspace 下的文件")
# result.status: completed | waiting_confirmation | waiting_human | paused | failed
# runner.continue_run() / approve_tool(id) / reject_tool(id) / submit_human_input(values)
```

`on_event` 不含 ANSI 和终端宽度，HTTP / SSE 可以复用同一条 Runtime。
`execute` 的 `tool_completed` 事件在 `result` 中携带 `exit_code`、`truncated`、`host_log_path`、`agent_log_path` 和 `termination_reason`；`content` 仍是发给模型的可读结果。

默认工具包括文件、沙箱命令、人工输入和 `write_todos`。`agent.tools.examples` 中的文档查询示例需要作为 `extra_tools` 显式加入；自定义副作用工具需要通过 `interrupt_on` 显式追加审批规则，内置审批规则不能被覆盖。工具异常不会自动重试，以免超时后重复执行副作用；模型传输错误由 OpenAI SDK 最多重试两次。
人工输入只使用 `request_human_input`：可传 `fields` 描述文本、布尔或选择题；恢复时用 `runner.submit_human_input({"text": "..."})`。审批用 `approve_tool` / `reject_tool`，暂停用 `continue_run`。旧工具 `handoff_to_human` 已移除，停在该工具调用上的旧会话恢复时会给出迁移错误。

传入 `create_deep_agent()` 的 `middleware=[...]` 是附加到默认栈，不会整表替换。Deep Agents 0.7.17 会自动加入 `create_summarization_middleware(model, backend)`。本项目只禁用了默认 general-purpose subagent，没有 `excluded_middleware`，因此自动压缩是开着的。

配置正数 `context_window` 时会将其作为模型的 `max_input_tokens` 传给 Deep Agents：约 85% 触发压缩，保留最近 10% 的上下文。未配置且模型自身也没有 `max_input_tokens` 时使用保守默认：约 170,000 tokens 触发、保留最近 6 条消息、旧工具参数在约 20 条消息时预裁剪。被挤掉的对话会写到 backend 上的会话历史文件，而不是直接丢掉。

`/compact` 调用 Deep Agents 的 `compact_conversation` 工具手动压缩，要求最近一次模型 usage 达到自动阈值的一半；配置了上下文窗口时约为 42.5%。未达到门槛时，CLI 显示最近一次模型报告的占用百分比；模型未报告 usage 时显示未知。

## Architecture

```text
你的终端
   │
   ▼
cli/                 输入、渲染、按键、人工确认
   │ RunEvent
   ▼
AgentRunner          流式、中断、恢复、session、附件
   │
   ▼
create_deep_agent()  LangGraph 图（不 Fork 上游）
   │
   ├── Model         OpenAI 兼容 / Qwen Responses
   ├── Tools         文件、execute、人工输入、write_todos
   ├── Middleware    暂停、转向、取消
   │                 + Deep Agents 默认栈（含自动摘要）
   └── Storage       SQLite checkpoint + session catalog
          │
          ▼
     Bubblewrap /workspace
```

四层各做一件事：

| 层 | 职责 |
|----|------|
| `agent/cli/` | 终端交互。换 UI 不改 Runtime |
| `agent/runner.py` | 一次 run 的生命周期：invoke / continue_run / approve_tool / reject_tool / submit_human_input / steer / cancel |
| `agent/factory.py` | `AgentSpec` 静态装配和 `create_deep_agent()` 构图 |
| `agent/sandbox.py` | Bubblewrap 策略与降级 |

更细的中间件顺序、挂载策略和 Qwen 事件适配在源码注释里，不在 README 展开。

### Project Structure

```text
agent/
├── cli/           # TUI
├── tools/         # 文件、execute、人工输入
├── middleware/    # 暂停、转向、取消、联网
├── factory.py     # 装配入口
├── runner.py      # 运行时
├── sandbox.py     # Bubblewrap
├── session.py     # SQLite session
└── llm.py         # 模型适配
bin/deep-agent     # 任意目录启动（cwd → /workspace）
examples/          # TUI 与流式冒烟
skills/            # SKILL.md 示例
tests/
config.example.yaml
```

## Configuration

CLI 使用 `DEEP_AGENT_CONFIG` 指定的现有配置，否则读取 `~/.deep-agent/config.yaml`；默认配置缺失时会生成模板并退出。项目内的配置文件不会自动加载。作为库使用时可显式传入 `Settings`。

主配置和按键配置必须位于 workspace 外；如果将 home 目录作为 workspace，启动时会拒绝位于 `~/.deep-agent` 的配置。已有项目内的 `config.yaml` 可一次性迁到 `~/.deep-agent/config.yaml`，确认内容后删除旧文件。配置中的相对路径（除 `sandbox.workspace: .`）仍相对于配置文件所在目录解析，迁移时需检查这些路径。

旧配置中的 `sandbox.protected_workspace_paths` 已移除，启动时会提示删除。将原工作区 `skills/` 中需要保留的目录手动复制到 `~/.deep-agent/skills/`；项目内的 `skills/` 不再自动加载。Agent 通过只读 `/skills` 路径读取用户 Skills；其他外部只读资源可使用 `extra_read_only_mounts` 显式开放。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `agent.instructions` | `null` | 追加到默认身份之后的长期说明；支持 YAML 多行文本 |
| `llm.default` | 首个 profile | 启动时的模型 |
| `llm.models.<id>.model` | — | OpenAI 兼容模型名 |
| `llm.models.<id>.provider` | `qwen-responses` | `qwen-responses` 或 `openai-compatible`；Token Plan 的 compatible-mode 端点用后者 |
| `llm.models.<id>.source` | 空 | `/model` 列表、页脚和状态中显示的来源，例如 `Local gateway` 或 `Token Plan` |
| `llm.models.<id>.stream_usage` | `false` | 流式 Chat Completions 请求附带 `stream_options.include_usage`，以获取实际 Token 用量；也可在 `llm.stream_usage` 为多个模型设置默认值 |
| `llm.models.<id>.base_url` | `http://localhost:8000/v1` | 推理端点 |
| `llm.models.<id>.input` | `[text]` | 图片模型写成 `[text, image]` |
| `llm.models.<id>.context_window` | `0` | 模型输入窗口，用于自动压缩阈值和底栏占用百分比；可写 `1000000`、`128k` 或 `1m`；`0` 表示未知并隐藏该指标 |
| `sandbox.workspace` | `.` | 映射到 `/workspace`；`.` = 启动时的 cwd |
| `sandbox.allow_unsandboxed` | `false` | 无 bwrap 时是否允许宿主机执行 |
| `sandbox.timeout_seconds` | `null`（无限制） | 可选的单次命令超时上限，单位秒；工具可请求更短时间 |
| `sandbox.max_output_bytes` | `100000` | TUI 实时显示开头、最终结果保留尾部的字节上限；超出时保存完整日志 |
| `paths.state_path` | `null` | Session DB；默认 `~/.deep-agent/sessions/<hash>.sqlite3` |
| `paths.config_dir` | `null` | 按键配置；默认 `~/.deep-agent` |

按键覆盖：`~/.deep-agent/keybindings.json`。

`execute` 输出超限时，完整日志保存到当前工作区的 `.deep-agent/logs/exec/`。最终工具结果同时给出宿主机真实路径和 Agent 可用文件工具读取的 `/workspace/.deep-agent/logs/exec/...` 路径。建议在自己的项目 `.gitignore` 中加入 `.deep-agent/`；程序不会修改项目的忽略规则。
父 shell 退出后，如果后台进程仍占有输出管道，`execute` 会继续收集数据，直到管道关闭或连续 100 毫秒没有新输出；后台进程在此后写出的内容不会进入本次工具结果。

`qwen-responses` 使用 `QwenChatOpenAI` 和 Responses API；`openai-compatible` 使用普通 `ChatOpenAI`，固定走 Chat Completions，百炼 Token Plan 的 compatible-mode 端点走这一条。两者都可通过 `AttachmentStore` 引用发送图片，前提是模型 profile 声明 `input: [text, image]` 且端点支持图片。

每次构图都会按默认身份、`agent.instructions`、工作区根目录 `AGENTS.md` 的顺序组成 system prompt；`AGENTS.md` 映射到 Agent 内的 `/workspace/AGENTS.md`。切换模型或权限会重新读取它。作为库调用时，`create_agent(instructions="...")` 可覆盖配置中的长期说明。

LangGraph checkpoint 保存消息、中断和图状态；同一 SQLite 的 `session_catalog` 另存 thread 的 model、permission 和上一轮运行原因，展示状态按运行原因计算。旧 catalog 在打开时迁移。模型的 `finish_reason` 保留在 checkpoint 消息中。`/resume` 只恢复状态，不调用模型。若上一轮是 `pending` 且已有 checkpoint，或上一轮是 `aborted`、`error`，下一次用户输入保持原文写入 checkpoint，恢复说明仅临时加入首次模型请求。空白新 thread 的 `pending` 不触发恢复说明。明确的审批或暂停中断仍按 checkpoint 恢复，不自动重跑工具。切换或新建会话时，TUI 会把未执行的 steering / follow-up 退回输入框；库调用者需先取回队列，才能切换会话。

沙箱默认挂载当前工作区，并将 `~/.deep-agent/skills/` 只读挂载为 `/skills`；宿主家目录的其他内容不可见。`network=true` 取消网络命名空间隔离，可访问宿主网络，包括 localhost、局域网和内网。Bubblewrap 不管 CPU / 内存配额。`UNSANDBOXED` 和显式传入的 `CUSTOM` backend 只有 ask：所有 `execute` 都要审批，不能切到 allow。

## Development

```bash
source .venv/bin/activate
pytest -q
```

`requirements.lock` 记录 Python 3.12 环境的完整依赖版本；升级依赖时，在干净的虚拟环境中安装 `requirements.txt`，运行 `python -m pip freeze > requirements.lock`，再运行测试。

测试不打真模型。对着本地端点做流式冒烟：

```bash
python examples/stream_smoke.py
```

## Roadmap

- [x] 交互式 TUI
- [x] 按工作区持久化 session
- [x] 工作区文件工具
- [x] Bubblewrap 沙箱（默认无网）
- [x] 权限 ask / 仅沙箱 allow
- [x] 流式思考与回答
- [x] 多模型切换
- [x] 图片附件
- [x] 运行中 steering / 取消
- [x] 语义化人工输入
- [x] 自动上下文压缩（Deep Agents 默认 `SummarizationMiddleware`）
- [ ] 可安装的 Python 包
- [ ] MCP

## Project Status

项目在活跃开发中。

配置格式、session schema、工具集合和公开 API 在第一个稳定版之前都可能变。请先当本地工具用，不要当生产 SDK 依赖。

## Contributing

Issue 和 Pull Request 都欢迎。改行为请带测试；不要在 PR 里提交 `config.yaml`、`.venv` 或 session 数据库。

## License

尚未指定开源许可证。使用前请先确认仓库后续声明。
