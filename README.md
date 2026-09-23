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


## Features

- **工作区工具** — `ls` / `read` / `write` / `edit` / `glob` / `grep` / `delete`，路径与 `execute` 都落在 `/workspace`
- **沙箱执行** — 默认无网络；`execute(network=true)` 经审批后，这一次才联网
- **权限模式** — `ask` 审批 `execute` 和内置文件写入、删除工具；`allow` 仅 SANDBOXED 可启用（需输入 `ALLOW`）
- **持久会话** — 每个工作区一份 SQLite，`/resume` 恢复最近线程，checkpoint 是恢复依据
- **流式输出** — 思考和回答按增量刷新；`execute` 的 stdout/stderr 原地更新同一个 Tool 块
- **运行中转向** — `Enter` 注入下一条指令，`Esc` 取消并把未发送的内容还原到输入框
- **后续任务** — `Alt+Enter` 排入 Runtime 队列，当前任务完成后由 `AgentRunner` 接续执行
- **多模型** — YAML profile + `/model` / `Ctrl+P`；切换重建 graph，会话保留
- **图片附件** — Ctrl+V / 路径 / `/image`；checkpoint 只存引用，请求模型时才编码
- **自动上下文压缩** — `create_deep_agent()` 默认带 `SummarizationMiddleware`，上下文接近上限时自动摘要；被挤掉的历史落到工作区，需要时还能再读
- **人工交互** — Agent 缺判断时弹出单选、多选、布尔、单行、多行，不绑特定 UI
- **任务规划** — `write_todos` 使用上游 TodoListMiddleware 更新 LangGraph 的 `todos` state；TUI 从 `values` 状态流替换当前计划，从 checkpoint 恢复计划，不保留历史版本的工具块
- **Skills** — 读取工作区 `skills/*/SKILL.md`

`/compact` 显示 unavailable，只表示没有「立刻手动压缩」接口。自动压缩已经在跑，不要再叠一层 `SummarizationMiddleware`，否则会压两次。

## Quick Start

### Requirements

- Python 3.12+
- Linux 或 WSL2（需要 [Bubblewrap](https://github.com/containers/bubblewrap)）
- 一台 OpenAI 兼容推理服务（默认 `http://localhost:8000/v1`）

### Installation

```bash
cd deep-agent

sudo apt-get install bubblewrap   # Debian / Ubuntu

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
```

### Configure

编辑 `config.yaml`，至少改 API Key 和端点：

```yaml
llm:
  default: qwen-plus
  models:
    qwen-plus:
      model: qwen3.5-plus
      input: [text, image]
      api_key: sk-your-key
      base_url: http://localhost:8000/v1
    qwen3.6-flash:
      model: qwen3.6-flash
      input: [text, image]
      api_key: sk-your-key
      base_url: http://localhost:8000/v1
```

也可以不改文件，启动时指定：

```bash
export DEEP_AGENT_CONFIG=/path/to/config.yaml
```

### Run

```bash
# 任意项目目录 = Agent 的工作区
export PATH="$PATH:/path/to/deep-agent/bin"
cd ~/projects/my-app
deep-agent
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
/session       当前线程与 SQLite 路径
/new           开一条新线程
/resume        列出并恢复本工作区最近会话
/resume a1b2   按 id 前缀恢复
/model         打开模型选择器
/model qwen3   按 id 前缀切换
/permission ask|allow   # allow 仅 SANDBOXED；UNSANDBOXED / CUSTOM 只有 ask
/image clipboard | <path> | clear
/pause         在下一个模型安全点暂停
/compact       占位：自动压缩已启用，没有手动立即压缩入口
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
| 展开 Tool / Thinking | `Ctrl+O` / `Ctrl+T` |
| 取回未应用的 steering | `Alt+Up` |

作为库使用：

```python
from agent import AgentRunner, create_agent

prepared = create_agent()
runner = AgentRunner(prepared=prepared, on_delta=print, on_event=print)
result = runner.invoke("列出 /workspace 下的文件")
# result.status: completed | waiting_confirmation | waiting_human | paused | failed
```

`on_event` 不含 ANSI 和终端宽度，HTTP / SSE 可以复用同一条 Runtime。

默认工具包括文件、沙箱命令、人工输入和 `write_todos`。`agent.tools.examples` 中的文档查询示例需要作为 `extra_tools` 显式加入；自定义副作用工具需要通过 `interrupt_on` 显式追加审批规则，内置审批规则不能被覆盖。工具异常不会自动重试，以免超时后重复执行副作用。

传入 `create_deep_agent()` 的 `middleware=[...]` 是附加到默认栈，不会整表替换。Deep Agents 0.7.17 会自动加入 `create_summarization_middleware(model, backend)`。本项目只禁用了默认 general-purpose subagent，没有 `excluded_middleware`，因此自动压缩是开着的。

当前 Qwen profile 没有 `max_input_tokens`，走 Deep Agents 的保守默认：约 170,000 tokens 触发、保留最近 6 条消息、旧工具参数在约 20 条消息时预裁剪。被挤掉的对话会写到 backend 上的会话历史文件，而不是直接丢掉。

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
   ├── Middleware    暂停、转向、取消、模型重试
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
| `agent/runner.py` | 一次 run 的生命周期：invoke / resume / steer / cancel |
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

加载顺序：显式路径 → `DEEP_AGENT_CONFIG` → 当前目录 / 项目根的 `config.yaml` → `config.example.yaml` → 内置默认值。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `agent.instructions` | `null` | 追加到默认身份之后的长期说明；支持 YAML 多行文本 |
| `llm.default` | 首个 profile | 启动时的模型 |
| `llm.models.<id>.model` | — | OpenAI 兼容模型名 |
| `llm.models.<id>.provider` | `qwen-responses` | `qwen-responses` 或 `openai-compatible` |
| `llm.models.<id>.base_url` | `http://localhost:8000/v1` | 推理端点 |
| `llm.models.<id>.input` | `[text]` | 图片模型写成 `[text, image]` |
| `sandbox.workspace` | `.` | 映射到 `/workspace`；`.` = 启动时的 cwd |
| `sandbox.allow_unsandboxed` | `false` | 无 bwrap 时是否允许宿主机执行 |
| `sandbox.timeout_seconds` | `120` | 单次命令超时 |
| `sandbox.max_output_bytes` | `100000` | 返回给模型的输出上限 |
| `paths.state_path` | `null` | Session DB；默认 `~/.deep-agent/sessions/<hash>.sqlite3` |
| `paths.config_dir` | `null` | 按键配置；默认 `~/.deep-agent` |

按键覆盖：`~/.deep-agent/keybindings.json`。

`qwen-responses` 使用 `QwenChatOpenAI` 和 Responses API；`openai-compatible` 使用普通 `ChatOpenAI`，固定走 Chat Completions。两者都可通过 `AttachmentStore` 引用发送图片，前提是模型 profile 声明 `input: [text, image]` 且端点支持图片。

每次构图都会按默认身份、`agent.instructions`、工作区根目录 `AGENTS.md` 的顺序组成 system prompt；`AGENTS.md` 映射到 Agent 内的 `/workspace/AGENTS.md`。切换模型或权限会重新读取它。作为库调用时，`create_agent(instructions="...")` 可覆盖配置中的长期说明。

LangGraph checkpoint 保存消息、中断和图状态；同一 SQLite 的 `session_catalog` 另存 thread 的 model、permission 和上一轮运行状态。模型的 `finish_reason` 保留在 checkpoint 消息中。`/resume` 只恢复状态，不调用模型。若上一轮是 `pending` 且已有 checkpoint，或上一轮是 `aborted`、`error`，下一次用户输入保持原文写入 checkpoint，恢复说明仅临时加入首次模型请求。空白新 thread 的 `pending` 不触发恢复说明。明确的审批或暂停中断仍按 checkpoint 恢复，不自动重跑工具。

沙箱默认只挂当前工作区，`skills/` 只读，宿主家目录不可见。Bubblewrap 不管 CPU / 内存配额。`UNSANDBOXED` 和显式传入的 `CUSTOM` backend 只有 ask：所有 `execute` 都要审批，不能切到 allow。

## Development

```bash
source .venv/bin/activate
pytest -q
```

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
- [ ] 手动立即压缩（`/compact`）
- [ ] 可安装的 Python 包
- [ ] MCP

## Project Status

项目在活跃开发中。

配置格式、session schema、工具集合和公开 API 在第一个稳定版之前都可能变。请先当本地工具用，不要当生产 SDK 依赖。

## Contributing

Issue 和 Pull Request 都欢迎。改行为请带测试；不要在 PR 里提交 `config.yaml`、`.venv` 或 session 数据库。

## License

尚未指定开源许可证。使用前请先确认仓库后续声明。
