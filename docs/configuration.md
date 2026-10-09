# 配置参考

## 配置文件位置

CLI 按以下顺序查找主配置：

1. 环境变量 `DEEP_AGENT_CONFIG` 指向的文件（文件必须存在）。
2. `~/.deep-agent/config.yaml`。

两者都不存在时，`deep-agent` 从内置模板 [`agent/config.example.yaml`](../agent/config.example.yaml) 生成 `~/.deep-agent/config.yaml`（权限 `0600`），然后退出。设置了 `DEEP_AGENT_CONFIG` 时不会生成模板。项目目录中的 `config.yaml` 不会被自动读取。

约束：

- 主配置、`~/.deep-agent/skills/` 和按键配置目录必须位于工作区之外，否则启动报错。因此不能把家目录（或其上层目录）作为工作区启动。
- 配置中的相对路径相对于配置文件所在目录解析；例外是 `sandbox.workspace: .`，它表示启动时的当前目录。
- 解析失败时 CLI 输出 `Configuration error: <文件>: <原因>` 并以退出码 2 退出，不会自动回退到其他模型或配置。
- 只有 `api_key` 和 `web_search.tavily_api_key` 支持 `${变量名}` 形式的环境变量引用，且必须是整个值；引用的变量不存在时报错。

## 模型（`llm`）

模型按“来源”分组。来源是一组共用端点和密钥的模型：

```yaml
llm:
  default: local/main            # 必填，格式为 来源/模型键
  models:
    local:                       # 来源名，自定义，不能含 /
      base_url: http://localhost:8000/v1
      api_key: ${LOCAL_API_KEY}
      provider: openai-compatible
      stream_usage: true
      models:
        main:                    # 模型键，自定义，不能含 /
          model: qwen3.5-plus    # 发给 API 的模型名；省略时使用模型键
          input: [text, image]
          context_window: 128k
        fast: {}                 # 全部继承来源级字段，请求模型名为 fast
```

来源下可以写的字段，模型项中也都可以写；模型项中的值覆盖来源级的值。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://localhost:8000/v1` | OpenAI 兼容端点 |
| `api_key` | `sk-local` | 字面值或 `${变量名}` |
| `provider` | `qwen-responses` | `qwen-responses` 使用 Responses API；`openai-compatible` 使用 Chat Completions |
| `model` | 模型键 | 仅模型项可用，实际请求的模型名 |
| `input` | `[text]` | 支持图片输入时写 `[text, image]`；必须包含 `text` |
| `context_window` | `0` | 模型输入窗口，可写 `128000`、`128k`、`1m`。`0` 表示未知：底栏不显示占用百分比，压缩阈值使用 Deep Agents 的默认值 |
| `stream_usage` | `false` | 仅 Chat Completions：请求中附加 `stream_options.include_usage`。端点在流式响应中不返回 usage 时，底栏的上下文占用会显示为未知 |
| `reasoning_efforts` | `[]` | 推理强度可选值，见下文 |

不再支持的写法会直接报错：`llm.model`、放在 `llm` 顶层的 `base_url` / `api_key` 等字段，以及不分来源的扁平 `llm.models.<模型>`。旧配置需要手动改写，程序不做迁移。

### 两种 provider

- `openai-compatible`：Chat Completions。会读取流式和非流式响应中的 `reasoning_content` 字段作为思考内容，并在后续请求中原样回传。
- `qwen-responses`：Responses API。会把 Qwen 的 `response.reasoning_text.*` 事件转换成 LangChain 能识别的格式。

两种 provider 的客户端都设置了 `trust_env=False`，因此忽略 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等代理环境变量。模型请求传输失败时由 OpenAI SDK 最多重试 2 次。

### 推理强度（`reasoning_efforts`）

程序不探测端点支持哪些强度值，列表需要你按服务商文档填写。

- 非空列表：`/model` 的第三步只显示这些值，默认选中第一项。
- 空列表或未配置：只显示 `default`，请求中不携带强度参数，由服务端决定行为。`default` 不等于关闭思考，也不能写进列表。
- 只有端点明确支持时才写 `none`（关闭思考）；`low` 仍然会思考。
- 继承规则：模型项省略该字段时继承来源的列表；写了就完整替换；写 `null` 或 `[]` 清除继承。

| provider | 发送的参数 |
|---|---|
| `openai-compatible` | `reasoning_effort` |
| `qwen-responses` | `reasoning.effort` |

同一模型在两种协议下的可选值可能不同。模板 [`config.example.yaml`](../agent/config.example.yaml) 中有阿里云百炼、百炼 Token Plan、DeepSeek、智谱的注释示例，使用前请对照服务商当前文档确认。

示例：百炼 Token Plan（Chat Completions 端点）

```yaml
llm:
  default: token-plan/qwen3.8-max
  models:
    token-plan:
      api_key: ${TOKEN_PLAN_API_KEY}
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
      provider: openai-compatible
      stream_usage: true          # 不开启时底栏无法显示上下文占用
      context_window: 1m
      reasoning_efforts: [none, low, medium, xhigh]
      models:
        qwen3.8-max:
          input: [text, image]
        glm-5.3:
          reasoning_efforts: [low, high, max]   # 替换来源级列表
        auto:
          reasoning_efforts: []                 # 清除继承，只提供 default
```

区域（`cn-beijing`）按自己的开通情况替换。

## Agent 说明（`agent`）

```yaml
agent:
  instructions: |
    优先使用中文回答。
```

system prompt 按顺序由三部分组成：内置身份说明、`agent.instructions`、工作区根目录的 `AGENTS.md`（若存在）。切换模型或权限模式时会重新读取 `AGENTS.md`。

## Web 搜索（`web_search`）

```yaml
web_search:
  tavily_api_key: ${TAVILY_API_KEY}
```

也可以只设置环境变量 `TAVILY_API_KEY`；两者都有值时环境变量优先。两者都没有时不注册 `web_search` 工具。

`web_search` 由 `deep-agent` 进程在宿主机上直接请求 `https://api.tavily.com/search`，超时 20 秒，不经过沙箱，也不会给 `execute` 开放网络。它只返回标题、URL 和摘要，不抓取完整网页。模型可用的参数：`query`、`max_results`（1–20，默认 5）、`topic`（`general` / `news`）、`time_range`（`day` / `week` / `month` / `year`）、`include_domains`。

## 沙箱（`sandbox`）

沙箱行为的说明见 [sandbox.md](sandbox.md)。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `workspace` | `.` | 映射为 `/workspace` 的宿主目录；`.` 表示启动时的当前目录 |
| `bwrap_path` | `bwrap` | Bubblewrap 可执行文件名或路径 |
| `allow_unsandboxed` | `false` | 沙箱不可用时是否允许直接在宿主机执行命令 |
| `timeout_seconds` | `null` | 单条命令的超时上限（秒）；`null` 表示不限制。模型可为单次调用请求更短的超时 |
| `max_output_bytes` | `100000` | 单条命令保留的输出上限，超出部分写入日志文件 |
| `env_allowlist` | `[]` | 允许传入沙箱的宿主环境变量名 |
| `env_set` | `{}` | 固定注入沙箱的环境变量 |
| `extra_read_only_mounts` | `[]` | 额外只读挂载，`[{source: /host/path, destination: /sandbox/path}]` |
| `extra_read_write_mounts` | `[]` | 额外读写挂载，格式同上 |

`sandbox.protected_workspace_paths` 已移除，配置中出现时启动报错。

## 路径（`paths`）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `state_path` | `null` | 会话数据库路径。默认 `~/.deep-agent/sessions/<工作区路径哈希>.sqlite3`，每个工作区一个文件。设置后所有工作区共用这一个文件 |
| `config_dir` | `null` | `keybindings.json` 和 `working_messages.yaml` 所在目录，默认 `~/.deep-agent` |

## 界面（`ui`）

修改后重启 CLI 生效。所有数量都是正整数，按逻辑行（换行符）计数，不计终端自动折行。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `timezone` | `Asia/Shanghai` | 每轮结束时 `Worked for … · HH:MM` 使用的 IANA 时区 |
| `thinking_tail_lines` | `5` | 思考区折叠时保留的末尾行数 |
| `execute_tail_lines` | `4` | 命令输出折叠时保留的末尾行数 |
| `tool_tail_lines` | `8` | 其他工具输出折叠时保留的末尾行数 |
| `expanded_tool_lines` | `40` | 展开的普通工具输出最多显示的行数 |
| `explore_preview_items` | `5` | 合并的“Explored N items”保留的最后几项 |
| `explore_failure_items` | `3` | 探索摘要中额外列出的最近失败项数 |
| `create_preview_items` | `5` | 合并的“Create N files”保留的最后几个路径 |
| `edit_preview_changed_lines` | `8` | 修改预览的增删行预算，至少为 2 |
| `write_preview_lines` | `6` | 覆盖写入预览显示的前几行 |
| `failure_preview_lines` | `4` | 失败工具摘要显示的前几行 |
| `command_failure_tail_lines` | `6` | 通用命令失败摘要保留的末尾行数 |
| `editor_max_lines` | `10` | 输入框最大行数；实际还不超过终端高度的三分之一 |
| `completion_menu_lines` | `8` | 命令补全菜单最大行数 |

## 按键（`keybindings.json`）

位于 `paths.config_dir`（默认 `~/.deep-agent/keybindings.json`）。只覆盖写出的动作，值可以是字符串或字符串列表：

```json
{
  "newline": ["c-j", "alt+enter"],
  "follow_up": "c-f"
}
```

可配置的动作及默认值：

| 动作 | 默认 |
|---|---|
| `submit` | `enter` |
| `newline` | `c-j` |
| `follow_up` | `alt+enter` |
| `interrupt` | `escape` |
| `clear_or_exit` | `c-c` |
| `exit` | `c-d` |
| `tools_expand` | `c-o` |
| `review_diff` | `c-r` |
| `reopen_interaction` | `f2` |
| `thinking_toggle` | `c-t` |
| `dequeue` | `alt+up` |
| `image_paste` | `ctrl+v`, `alt+v` |
| `model_cycle_forward` | `ctrl+p` |
| `model_cycle_backward` | `alt+p` |

按键写法：`ctrl+x`、`alt+x`、`shift+tab`，或 prompt_toolkit 的键名（如 `c-j`、`escape`、`f2`）。文件无法解析时使用默认按键，并在界面中提示。

## 运行状态文案（`working_messages.yaml`）

任务运行时，状态栏旁每隔 `interval_seconds` 秒轮换一条短句。首次启动时，默认文案被复制到 `paths.config_dir/working_messages.yaml`，已有文件不会被覆盖；仓库中的默认文件是 [`agent/cli/working_messages.yaml`](../agent/cli/working_messages.yaml)。

```yaml
interval_seconds: 8
jokes:
  - "需求改了八次，唯一没改的是交付日期。"
  - text: "需求很简单，工期很勇敢。"
    speaker: "今日冷笑话"
fgo:
  - text: "流星一条！"
```

- 每条可以是字符串，或含 `text`、可选 `speaker`、`source_url`、`published_on` 的对象；`fgo` 条目的 `speaker` 不显示。
- 两组都存在时按“两条 `jokes`、一条 `fgo`”交替，每轮随机打乱。
- 设置 `fgo: []` 只显示 `jokes`；两组都为空列表时关闭轮换。
- 文件有误时使用默认文案并在转录区提示，不会改写你的文件。

## 环境变量

| 变量 | 作用 |
|---|---|
| `DEEP_AGENT_CONFIG` | 主配置文件路径 |
| `DEEP_AGENT_CONFIG_DIR` | `paths.config_dir` 未设置时的按键与文案目录 |
| `DEEP_AGENT_STATE_PATH` | `paths.state_path` 未设置时的会话数据库路径 |
| `TAVILY_API_KEY` | Tavily 密钥，优先于配置文件 |
