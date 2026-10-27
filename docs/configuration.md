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

## 全局网络环境（`trust_env`）

在主配置 `config.yaml` 顶层设置，修改后重启生效：

```yaml
trust_env: false
```

只接受 YAML 布尔值 `true` / `false`，不要写成字符串 `"true"`，也不要放在 `llm`、`sandbox` 或 `mcp.json` 中。

默认 `false`。设为 `true` 后，模型 API（同步和异步）、MCP HTTP/OAuth 和 Tavily 客户端读取代理与 CA 环境变量；stdio MCP 子进程和 `execute` 命令也自动继承这些变量。

- 代理：`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，以及对应的小写名称。
- CA：`SSL_CERT_FILE`、`SSL_CERT_DIR`；子进程还传递 `REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`。
- 子进程传递 `NODE_USE_ENV_PROXY`，未指定时默认设为 `1`，让支持该开关的 Node 版本使用环境代理。其他程序是否使用这些变量由程序自身决定。

`false` 时，上述变量也会从子进程环境移除，即使写在 `sandbox.env_allowlist`、`sandbox.env_set` 或 MCP `env_passthrough` 中。`true` 时显式的子进程配置优先于继承值。此开关不改变沙箱网络审批与文件挂载；自定义 CA 路径需要在子进程所在环境中可访问。它也不阻止命令自行设置代理或读取自己的配置文件。

例如使用本机 HTTP 代理，在主配置顶层改为 `trust_env: true`，再从设置了代理的终端启动：

```bash
# 将端口替换为实际代理端口。
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
export NO_PROXY=localhost,127.0.0.1,::1
deep-agent
```

无需再把代理变量列入 `sandbox.env_allowlist` 或 MCP `env_passthrough`。已有用户配置不会随升级覆盖，需手动添加此项。

## MCP（`mcp.json`）

MCP 目录与模型配置分开，默认是 `~/.deep-agent/mcp.json`。CLI 把它放在沙箱隐藏的配置目录里。这个文件是项目自己的目录，不会原样交给 LangChain `MCPAdapter`。

使用 `node` / `npx` 启动 stdio MCP 服务时，推荐 Node.js 24（使用最新的 24.x 补丁版本），具体兼容范围以服务自身要求为准。HTTP MCP 客户端由 Python 运行，不依赖本机 Node 版本。

根级 `enabled` 是整个 MCP 系统的开关，默认 `true`。旧文件没有这个字段时也视为开启，并且不会为了补上默认值而改写文件。只有 JSON 布尔值 `true` / `false` 有效；字符串 `"false"` 或数字 `0` 会使配置加载失败，原文件保持不动。`config.yaml` 不保存这个开关。

`servers.<name>.enabled` 只控制这一个服务。两个开关都为 `true` 时，CLI 才会连接该服务并把它的工具交给模型。全局关闭后不连接任何服务，不启动 stdio 子进程，也不触发 OAuth；已保存的服务配置和 `oauth/` 令牌缓存都保留。用 `/mcp enable` 重新打开后，仍按下面的规则自动连接和授权。

启动时，仅在全局开关打开时自动连接各个已启用的服务。无需鉴权的服务直接连接；Bearer 服务从 `token_env` 指定的环境变量读取密钥；OAuth 服务优先复用缓存凭证，访问令牌过期时先自动刷新，首次授权或凭证无法刷新时自动打开浏览器，等待用户完成授权。后续请求也按此流程处理凭证失效。运行中切换全局开关不会打断当前 Agent，工具表要到下一个安全点才重建。

OAuth 令牌、刷新令牌和客户端注册信息加密保存在 `mcp.json` 同目录下的 `oauth/` 中，默认路径为 `~/.deep-agent/oauth/`。缓存目录权限为 `0700`，本地加密密钥权限为 `0600`，不写入 `mcp.json`。同一进程中每个服务 60 秒内最多打开一次授权页，防止并发请求或失败重试连续弹窗；这不是定时重新授权，凭证有效期间不会打开浏览器。授权取消或超时后不循环重试，可稍后执行 `/mcp test <name>` 重试。此限制只针对 OAuth，不影响无需鉴权的服务。

MCP 服务器在宿主进程中连接和执行，不经过 Bubblewrap，也不受沙箱的文件系统或网络限制。工具审批统一受 `/permission` 控制：`ask` 需批准，`allow` 自动执行，包括添加和删除服务。stdio 的命令直接在宿主运行。Bearer token 只记录环境变量名，文件里不写明文。OAuth 由 FastMCP 打开浏览器，Agent 不能代替用户确认。

```json
{
  "enabled": true,
  "servers": {
    "deepwiki": {
      "transport": "streamable_http",
      "url": "https://mcp.deepwiki.com/mcp",
      "enabled": true
    },
    "internal": {
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "auth": {"type": "bearer", "token_env": "INTERNAL_MCP_TOKEN"},
      "enabled": true
    },
    "oauth-service": {
      "transport": "streamable_http",
      "url": "https://example.com/oauth-mcp",
      "auth": {"type": "oauth"},
      "enabled": true
    }
  }
}
```

MCP HTTP 请求与模型、Tavily 客户端统一受全局 `trust_env` 控制。stdio 使用 `command` 和 `args`（参数数组，不是 shell），需要的环境变量名放在 `env_passthrough`。服务器名在文件内唯一。工具在 Agent 里的名称是 `mcp__<服务器>__<远程工具>`。一个服务器连不上不会阻止其他服务器。文件损坏时启动会报错，并且拒绝覆盖原文件。用 `/mcp` 或 Agent 的 `mcp_list` / `mcp_add` / `mcp_remove` 修改。`mcp_list` 只读、免审批；`mcp_add` / `mcp_remove` 在 `ask` 下需确认，`allow` 免审批；`/mcp add` 向导在 `allow` 下也跳过最终保存确认。全局关闭时不注入这些管理工具。OAuth 浏览器授权仍由用户完成。工具表只在下一次安全点重建，不会卸掉正在执行或正在等待审批的图。

同一服务的并发连接请求合并为一次，避免启动连接、`/mcp test` 和 `mcp_add` 相互覆盖。同一 MCP 客户端连接上的工具调用由宿主排队执行，模型可一次提交多个工具，宿主会自动串行发送；不同 MCP 连接仍可并行。排队时间计入调用超时，排队期间取消的调用不会发送。会话关闭后，下一次调用会先通过 SDK 公共接口重建连接；已失败的工具不会自动重放。同名 `mcp_add` 会检查客户端实际连接状态，坏连接可直接重建，无需先删除服务。

`application/octet-stream` 内嵌资源会在模型媒体过滤前转换为文本：可读 UTF-8 数据直接提供内容，其他二进制数据以可还原的 Base64 文本提供。图片、其他已声明的媒体类型、资源 URI 和结构化结果保持原有处理方式；这不代表模型获得了任意二进制文件的原生理解能力。大结果仍使用现有工具输出卸载机制。

MCP / OAuth 日志及相关 SDK 警告只写后台日志，不显示在 TUI 消息区。日志位于 `mcp.json` 同目录下的 `logs/mcp-<进程号>.log`，每个文件最多 2 MiB，保留两份轮转文件。日志目录权限为 `0700`，日志文件权限为 `0600`。

当前不实现 Sampling 回调、旧式服务端 Elicitation 或 MCP Apps UI。依赖这些能力的示例工具可能超时或失败；MCP Apps 仅显示服务提供的兜底内容。

## 工具可见性（`tools.hidden`）

```yaml
tools:
  hidden:
    - request_human_input
```

未写 `hidden` 时不隐藏任何工具。示例配置 [`config.example.yaml`](../agent/config.example.yaml) 里写了 `compact_conversation`，首次生成的配置会按该列表隐藏；已有配置不会被覆盖。列表按工具名匹配，在 Agent 初始化时生效；自定义工具、文件工具及中间件提供的工具均会从模型请求中过滤。内置系统提示词使用中文，字段名、工具名及枚举值保持英文；系统提示词不包含工具用法。工具说明与参数由上游工具定义原样提供，不做翻译；本地自定义工具可直接用中文编写说明。外部 MCP 服务的原始说明和用户、项目提供的内容不自动翻译。用户和项目指令保持原样，配置后应自行避免要求调用已隐藏的工具。

此设置只控制模型可见工具，不改变 ASK/ALLOW 审批规则，不提供 `/tool` 命令或运行时热切换。

## 模型（`llm`）

模型按“来源”分组。来源是一组共用端点和密钥的模型：

```yaml
llm:
  default: token-plan/qwen3.8-flash
  timeout: 60
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

来源下可以写的字段，模型项中也都可以写；模型项中的值覆盖来源级的值。`timeout` 例外：它只写在 `llm` 下一次，所有模型共用。

接入新的标准兼容模型，只需要在这里配置 `api`、端点地址/凭据、模型 ID 和模型能力。协议格式转换由程序完成，无需修改 Agent 核心，也无需配置厂商兼容模式。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://localhost:8000/v1` | OpenAI 兼容端点 |
| `api_key` | `sk-local` | 字面值或 `${变量名}` |
| `api` | `responses` | `responses` 使用 Responses API；`chat_completions` 使用 Chat Completions |
| `max_retries` | `5` | 非负整数，额外重试次数；`0` 禁用。模型级覆盖来源级 |
| `stream_chunk_timeout` | `120` | 模型流无进展超时（秒）。连续这么久没有新的解析后 chunk 就中断并重试当前请求；持续输出可以超过它。`0` 禁用。模型级覆盖来源级 |
| `model` | 模型键 | 仅模型项可用，实际请求的模型名 |
| `input` | `[text]` | 支持图片输入时写 `[text, image]`；必须包含 `text` |
| `context_window` | `0` | 模型输入窗口，可写 `128000`、`128k`、`1m`。`0` 表示未知：底栏不显示占用百分比，压缩阈值使用 Deep Agents 的默认值 |
| `reasoning_efforts` | `[]` | 推理强度可选值，见下文 |

不再支持的写法会直接报错：`llm.model`、放在 `llm` 顶层的 `base_url` / `api_key` 等字段，以及不分来源的扁平 `llm.models.<模型>`。这些旧布局需要手动改写。旧协议字段也不再接受，见下文。

### 两种 API

- `chat_completions`：Chat Completions。会读取流式和非流式响应中的 `reasoning_content` 或 `reasoning` 字符串字段作为思考内容，并在后续请求中原样回传。
- `responses`：Responses API。会把 Qwen 的 `response.reasoning_text.*` 事件转换成 LangChain 能识别的格式。

两种 API 的客户端统一受全局 `trust_env` 控制，默认忽略代理与 CA 环境变量。连接失败、请求超时、429、5xx 等请求故障默认额外重试 5 次，可通过来源级或模型级 `max_retries` 调整。

流式调用由单层逻辑统一重试连接错误和响应读取中断（包括缺少结束事件就直接 EOF），共享同一预算：首次请求 + 最多 5 次重试，总 HTTP 请求次数不超过 6 次。每次流式请求使用独立的 SDK 客户端副本并禁用 SDK 内部重试，不修改共享客户端；非流式调用仍使用 SDK 原生重试。

模型流的无进展超时由 `stream_chunk_timeout` 配置，默认 120 秒：连续这么久没有新的解析后 chunk 就中断当前这次模型请求并按同一预算重试。持续输出的流可以超过该值。SSE keepalive 不会重置这个计时器。它不是整次 Agent Run 的总时间上限。设为 `0` 则关闭。不完整的工具参数不会被执行。

HTTP 首字节超时由 `llm.timeout` 配置，默认 60 秒，所有模型共用，不能写在来源或单个模型上。它限制建立连接，以及等到第一批响应数据。响应开始之后，只要仍有数据到达，整轮生成可以超过 60 秒。完全没有字节时，它会先于 `stream_chunk_timeout` 触发。必须为正数。

重试重新发起当前模型调用，清除失败尝试的临时文本、思考内容和工具参数，已完成的工具不会重跑；Esc 在请求和退避等待期间均可取消，取消不触发重试。重试只覆盖模型请求，不覆盖工具执行。

### 已移除的配置字段

`provider` 和 `stream_usage` 不再兼容：来源级或模型级出现任何一个字段，都会明确报错，即使同时配置了有效的 `api` 也不能保留旧字段。

协议只通过 `api: responses` 或 `api: chat_completions` 选择；模型级覆盖来源级，省略时默认为 `responses`。不会按模型名或 Base URL 推测协议，也不会自动改写用户配置文件。Chat Completions 固定请求流式 usage，无需配置开关。

### Context 与 Token Usage

全新会话（首次启动或 `/new` 后尚未运行任务）显示 `0.0% used`。这表示当前对话尚未使用窗口，不是对系统提示词或首个请求 token 数的估算；首次任务开始后，没有有效用量时仍显示 `Unknown`。恢复有内容的会话或仅清空屏幕不会触发新会话的零值显示。

Chat Completions 固定请求 `stream_options.include_usage=true`；Responses 从完成事件读取 usage。服务端明确拒绝该参数时报告错误，不会关闭参数重试。普通响应缺失 usage 不影响调用成功。

Context 百分比只使用最近一次 API 响应的 `usage_metadata.input_tokens / context_window * 100`，不使用 `total_tokens`、累计用量、Tokenizer 或字符估算。输入为零显示 `0.0%`；缺失时显示 `Unknown`。生成过程中在服务端返回 usage 前显示 `Unknown`，收到 usage-only 数据块立即更新。

每次模型调用开始、取消/失败、切换模型、压缩完成都会清除旧值。checkpoint 保存响应所属的模型来源 ID；恢复会话时只使用当前模型、当前有效消息的最后一次 usage。旧会话仍可读取，但无法确认模型来源的旧 usage 显示 `Unknown`，下一次调用后重新获得准确数据。

这里的 input_tokens 表示最近一次请求的输入量，不表示下一次请求的精确上下文。Deep Agents 自动压缩仍使用自身原有估算和阈值，独立于 TUI 显示。

### 推理强度（`reasoning_efforts`）

程序不探测端点支持哪些强度值，列表需要你按服务商文档填写。

- 非空列表：`/model` 的第三步只显示这些值，默认选中第一项。
- 空列表或未配置：只显示 `default`，请求中不携带强度参数，由服务端决定行为。`default` 不等于关闭思考，也不能写进列表。
- 只有端点明确支持时才写 `none`（关闭思考）；`low` 仍然会思考。
- 继承规则：模型项省略该字段时继承来源的列表；写了就完整替换；写 `null` 或 `[]` 清除继承。

| API | 发送的参数 |
|---|---|
| `chat_completions` | `reasoning_effort` |
| `responses` | `reasoning.effort` |

同一模型在两种协议下的可选值可能不同。模板 [`config.example.yaml`](../agent/config.example.yaml) 中有阿里云百炼、百炼 Token Plan、DeepSeek、智谱的注释示例，使用前请对照服务商当前文档确认。

示例：百炼 Token Plan（Chat Completions 端点）

```yaml
llm:
  default: token-plan/qwen3.8-max
  models:
    token-plan:
      api_key: ${TOKEN_PLAN_API_KEY}
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
      api: chat_completions
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

system prompt 按顺序由三部分组成：内置身份说明、`agent.instructions`、工作区根目录的 `AGENTS.md`（若存在）。启用 MCP 且注入工具时，构图阶段还会在末尾追加一段宿主运行与不可信数据说明。切换模型或权限模式时会重新读取 `AGENTS.md`。它在构图时确定，不包含环境快照。环境快照由中间件在每次模型调用时附到系统提示后面，不写入 checkpoint。它不是配置项。沙箱模式、工作区、HOME 或额外挂载变化后重新采集；恢复会话时按当前沙箱重新检查；切换模型不会因此重新探测。已给出的字段不要再查。

## Web 搜索（`web_search`）

```yaml
web_search:
  tavily_api_key: ${TAVILY_API_KEY}
```

也可以只设置环境变量 `TAVILY_API_KEY`；两者都有值时环境变量优先。两者都没有时不注册 `web_search` 工具。

`web_search` 由 `deep-agent` 进程在宿主机上直接请求 `https://api.tavily.com/search`，超时 20 秒，不经过沙箱，也不会给 `execute` 开放网络。`ask` 模式下每次搜索都需先批准；拒绝时不会发送搜索请求。它只返回标题、URL 和摘要，不抓取完整网页。模型可用的参数：`query`、`max_results`（1–20，默认 5）、`topic`（`general` / `news`）、`time_range`（`day` / `week` / `month` / `year`）、`include_domains`。

## 沙箱（`sandbox`）

沙箱行为的说明见 [sandbox.md](sandbox.md)。代码默认值和示例模板都是最小基线，只覆盖常见本机布局；须按实际部署以及需要保护的目录调整挂载和环境变量，见 [文件系统模式](sandbox.md#文件系统模式)。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `workspace` | `.` | 宿主项目目录。沙箱路径和当前目录都是它的真实绝对路径（如 `/mnt/d/test`）；`.` 表示启动时的当前目录 |
| `bwrap_path` | `bwrap` | Bubblewrap 可执行文件名或路径 |
| `mode` | `restricted` | 文件系统模式：`restricted` 或 `full-access`。其他值启动失败。`full-access` 写入当前真实环境，仍在 Bubblewrap 中执行，不取消审批。`/dev` 默认是沙箱自己的设备 tmpfs，宿主设备由 `extra_read_write_mounts` 的 `dev_bind` 传入。运行中可用 `/sandbox` 切换，不写回本文件。`restricted` 使用独立持久 HOME（`~/.deep-agent/sandbox/home`），`full-access` 使用真实用户 HOME。切换模式不迁移用户级安装，也不删除另一模式已有的数据。行为见 [HOME 隔离与持久化](sandbox.md#home-隔离与持久化) |
| `timeout_seconds` | `null` | 单条命令的超时上限（秒）；`null` 表示不限制。模型可为单次调用请求更短的超时 |
| `max_output_bytes` | `100000` | 单条命令保留的输出上限，超出部分写入日志文件 |
| `env_allowlist` | `[]` | 允许传入沙箱的宿主环境变量名 |
| `env_set` | `{}` | 固定注入沙箱的环境变量 |
| `extra_read_only_mounts` | `[]` | 额外只读挂载，`[{source, destination, optional}]`。`optional: true` 且源目录不存在时跳过。固定系统文件列表里没有的路径也写在这里，例如 CentOS 的 `/etc/pki` 挂到同名路径。SANDBOXED 下文件工具按只读使用同一目标。不能设 `dev_bind` |
| `extra_read_write_mounts` | `[]` | 额外读写挂载，格式同上，另可设 `dev_bind: true`，对该条使用 `--dev-bind`，用来挂宿主 `/dev` 或单个设备节点。普通 `--bind` 带 `nodev`，打不开 USB 串口。SANDBOXED 下文件工具也可写这些目标。重复目标、后挂载盖住先挂载或错位嵌套在启动时拒绝。同一目录的只读和读写别名可以共存，权限按虚拟路径区分 |

表中的默认值是代码内默认值。首次生成的 [`config.example.yaml`](../agent/config.example.yaml) 写成 `restricted`。宿主 `/dev` 写在 `extra_read_write_mounts` 里，并带 `modes: [full-access]`，所以新模板的 restricted 使用沙箱私有 `/dev`，切到 full-access 才做宿主 `dev_bind`。已有的 `~/.deep-agent/config.yaml` 不会被覆盖，也不会被悄悄改掉。旧配置里没有 `modes` 的 `/dev` 挂载在两种模式都仍然生效；若希望 restricted 不再开放宿主设备，给那一条加上 `modes: [full-access]` 后重启。`sandbox.dev` 已移除，配置里出现时启动失败。

`sandbox.protected_workspace_paths` 与 `sandbox.allow_unsandboxed` 已移除，配置中出现时启动报错。Bubblewrap 不可用时启动失败，没有宿主机执行回退。

### 默认工具挂载

以下路径是 `restricted` 收窄可见范围时的示例挂载，不是模板默认生效项。

| 用途 | 宿主 `source` | 沙箱 `destination` | 权限 |
|---|---|---|---|
| NVM 全部已安装版本 | `~/.nvm/versions/node` | `/opt/node` | 只读 |
| Cargo 可执行文件及目录 | `~/.cargo` | `/opt/cargo` | 只读 |
| Rustup 工具链及目录 | `~/.rustup` | `/opt/rustup` | 只读 |
| Chrome 程序目录 | `/opt/google` | `/opt/google` | 只读 |
| X11 文件 Socket | `/tmp/.X11-unix` | `/tmp/.X11-unix` | 只读 |
| Playwright 浏览器缓存 | `~/.cache/ms-playwright` | `/opt/ms-playwright` | 读写 |

以上项目是 `restricted` 收窄可见范围时的例子，不是模板的生效配置。路径不存在就跳过，不寻找替代路径，也不表示对应工具已通过运行检查。

模板默认是 `mode: restricted`。`env_allowlist` 仍含 `PATH` 和 `DISPLAY`，但这只是允许传入这些变量，不表示宿主 `PATH` 上的目录已经挂进沙箱。`env_set` 为空。程序会将 `/run/deep-agent` 自动放到 `PATH` 最前。配置文件未写 `mode` 时，代码默认仍是 `restricted`。挂载可以写 `modes: [restricted]`、`[full-access]` 或两者；省略 `modes` 时两种模式都生效。空数组、未知值或重复模式会在加载配置时失败。

`source` 支持 `~`；相对源路径按配置文件目录解析。`destination` 必须是沙箱绝对路径，不支持 `~`。`env_set.PATH` 也应使用沙箱中的实际路径。挂载权限由这两个列表表达，程序没有工具链自动发现或版本选择逻辑。

用户可以缩小到单个版本或所需子目录，也可以显式扩大可见范围；挂载调整不改变 `ask` / `allow` 的审批规则。只读挂载禁止写入，仍会暴露目录中的敏感文件。缓存自定义目录、WSLg 源路径和配置合并示例见 [沙箱与权限](sandbox.md#工具目录与环境变量)。

## 路径（`paths`）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `state_path` | `null` | 会话数据库路径。默认 `~/.deep-agent/sessions/<工作区路径哈希>.sqlite3`，每个工作区一个文件。设置后所有工作区共用这一个文件 |
| `config_dir` | `null` | `keybindings.json` 和 `working_messages.yaml` 所在目录，默认 `~/.deep-agent` |

## 界面（`ui`）

修改后重启 CLI 生效。内容折叠统一使用 `head_lines` 和 `tail_lines`，均为正整数，默认各 4 行。需要开头预览的命令正文、写入、差异和普通失败摘要使用 `head_lines`；思考和工具输出、命令失败输出使用 `tail_lines`。命令正文和思考按终端折行后的显示行计数；其他内容按逻辑行计数。展开工具输出后不再受折叠行数限制。带 `_items` 的字段按条目计数。

```yaml
ui:
  head_lines: 4
  tail_lines: 4
```

原来的 `thinking_tail_lines`、`execute_tail_lines`、`tool_tail_lines`、`expanded_tool_lines`、`edit_preview_changed_lines`、`write_preview_lines`、`failure_preview_lines`、`command_failure_tail_lines` 已移除。已有配置中请删除这些字段，改用上面的两个字段；程序不会覆盖已有配置文件。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `timezone` | `Asia/Shanghai` | 每轮结束时 `Worked for … · HH:MM` 使用的 IANA 时区 |
| `head_lines` | `4` | 内容折叠时保留的开头行数 |
| `tail_lines` | `4` | 内容折叠时保留的末尾行数 |
| `explore_preview_items` | `5` | 合并的“Explored N items”保留的最后几项 |
| `explore_failure_items` | `3` | 探索摘要中额外列出的最近失败项数 |
| `create_preview_items` | `5` | 合并的“Created N files”保留的最后几个路径 |
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
| `messages_toggle` | `c-b` |
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


## 百炼官方来源

可以在 `llm.models` 下添加以下来源，密钥从 `ALIYUN_API_KEY` 环境变量读取。此变量名是用户配置约定，程序仍按 `${变量名}` 统一解析。

```yaml
bailian:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: ${ALIYUN_API_KEY}
  api: responses
  models:
    qwen3.5-plus:
      input: [text, image]
      context_window: 1m
    qwen3.8-flash:
      input: [text, image]
      context_window: 1m
      reasoning_efforts: [none, low, medium, xhigh]
    qwen3-vl-flash:
      api: chat_completions
      input: [text, image]
      context_window: 262144
```

北京公共域名仍可使用；官方建议迁移到业务空间专属域名，见 [Responses 端点说明](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)。协议只由 `api` 决定；所有地址均按 SDK 标准格式序列化 `extra_body`。`qwen3-vl-flash` 使用 Chat Completions，窗口按 [官方模型规格](https://help.aliyun.com/zh/model-studio/qwen3-vl-flash) 填写。示例不为旧型号推测推理强度列表。


## SubAgent 委派

默认启用 Deep Agents 原生同步 `task`，注册 `general-purpose` 子 Agent。
配置位于顶层 `subagents`：

```yaml
subagents:
  enabled: true
  max_concurrent: 2
  model: null
  reasoning_effort: null
  permission: allow
```

`max_concurrent` 是同时执行上限；达到上限的调用立即返回错误，已有任务结束后可以继续委派。
`model` 必须是 `llm.models` 中的完整模型 ID，`reasoning_effort` 必须属于目标模型声明的能力。
空值继承当前主模型和思考强度；模型切换只影响下一次创建的任务。
当前仅支持 `allow` 权限，不提供子任务审批；没有经过验证的 `SANDBOXED` 后端时，委派明确失败。

子 Agent 使用独立消息历史和内存 checkpoint，不继承主对话；共享主 Runner 的文件系统与
SandboxPool。可使用文件工具、execute、已配置的 web_search、Skills 和 AGENTS.md，
不装载 MCP、人工输入、TUI 审批或递归委派工具。文件修改仍遵循原有沙箱边界。
父任务取消会取消所有关联任务、模型 HTTP 流及执行命令；单个任务结束不会关闭共享 Pool。
`task` 等待任务实际完成，没有跨轮次后台任务。

输入框上方现有空白行右侧只显示 `1 subagent` / `2 subagents` 等运行数量，零时隐藏。
任务卡片与内部输出不会显示在实时或恢复后的 TUI 中；checkpoint 仍保留合法的最终 task 结果。

完整事件写入配置目录（默认 `~/.deep-agent`）下的
`subagents/<父会话 ID 的 SHA-256>/<run_id>.jsonl`，使用私有目录/文件权限。
这样可复用现有受保护配置目录，避免工作区工具直接读取其他会话日志。
每项事件最多约 24 KB，较大内容截断并标记；模型增量、工具输出、最终状态按顺序追加并 flush。
已知配置密钥及常见 token/password 字段会脱敏；这不是任意自然语言秘密的通用识别器。
日志独立于 SQLite schema，重启后仍可查询，暂不自动清理。

主 Agent 可按需调用 `read_subagent_log(run_id=null, offset=0, limit=100)` 查看当前会话的
最近任务 ID；指定 ID 后分页读取事件。ID 不接受路径，查询限定父会话，单页最多 100 项、约 64 KB。
常规 task 结果只包含 run ID、最终回答或简短错误，内部日志仅在显式查询时进入主上下文。
