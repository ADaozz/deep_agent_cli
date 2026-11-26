# TUI 展示规范

本文是 `deep_agent_cli` 全部工具卡片、状态语义、颜色、树形连接、对齐、聚合、折叠与输入区布局的**唯一权威规范**。

- [tui.md](tui.md) 仅保留面向用户的操作、命令、快捷键与功能说明，引用本文。
- [architecture.md](architecture.md) 仅保留系统分层与模块职责边界，引用本文。
- 未经用户明确要求，不得修改本文锁定的任何视觉样式、标题文案、状态标记、颜色、树形前缀、间距、分组或折叠规则；任何经授权的展示变更必须同步更新本文与 `tests/test_tui_golden_snapshots.py`。

---

## 1. 模块职责边界

| 模块 | 唯一职责 |
|---|---|
| `agent/tools/catalog.py` | 工具名称常量与基础分类（探索、文件变更、命令、网页、副作用等） |
| `agent/cli/previews.py` | 展示语义、标题文案、Diff 行号/槽宽、聚合与折叠数据（`ToolPreview` / `FileMutationPreview` / `FileSection` / `DiffPreview`） |
| `agent/cli/rendering.py` | Rich 渲染、状态点/Spinner、颜色、缩进、树形连接（`├` / `│` / `└`）、折行与渲染缓存 |
| `agent/cli/highlighting.py` | `CodeRenderer` 语法词法与 Diff 叠色高亮 |
| `agent/cli/display.py` | `UiDisplayLimits` 展示预算 |
| `agent/cli/state.py` | 实时运行事件投影到 `CliState` / `ToolBlock` |
| `agent/sessions/transcript.py` | Checkpoint 历史消息恢复与统一执行状态投影 |
| `agent/cli/app.py` | TUI 窗口布局、滚动、按键与交互编排 |

约束：渲染层不得重复判断工具业务语义；预览层不得硬编码树形连接符（`├` / `│` / `└`）；单个工具不得另建独立样式常量或状态颜色。

---

## 2. 全局视觉规范

| 元素 | 固定样式 | 实现位置 |
|---|---|---|
| 运行中 | 绿色 Braille Spinner（`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`，`green`） | `_tool_status_marker` / `spinner_frame` |
| 成功 | 绿色 `•`（`green`） | `_tool_status_marker` |
| 等待审批或输入 | 黄色 `•`（`yellow`） | `_tool_status_marker` |
| 失败、取消、中断、未确认 | 灰色 `•`（`#888888`） | `_tool_status_marker` |
| 工具标题 | 白色粗体（`bold`） | `render_tool_preview` / `_command_card` / `_failure_card` / `_explore_group` |
| 状态后缀 | 灰色（`dim`） | `_tool_status_suffix` |
| 树形连接线 | 灰色 `├`、`│`、`└`（`TREE_BRANCH_STYLE = "dim"`） | `_tree_branch` / `_tree_frame` / `_frame_text` |
| 普通输出与提示 | 灰色（`dim`）；错误正文为 `red` | `_output_tail` / `_subagent_result` |
| Diff 新增背景 | `#005f00`（`DIFF_ADD_BG`），`+` 标记 `#00af00` | `_diff_row` |
| Diff 删除背景 | `#5f0000`（`DIFF_DELETE_BG`），`-` 标记 `#af0000` | `_diff_row` |
| 新增统计 | `#13a10e`（`DIFF_ADD_STAT`） | `render_tool_preview` / `_mutation_tree` |
| 删除统计 | `#c50f1f`（`DIFF_DELETE_STAT`） | `render_tool_preview` / `_mutation_tree` |
| 卡片之间 | 一个空行（紧接用户消息气泡时复用气泡底部空行，仍恰好一行） | `Padding(..., (0 if after_user else 1, 0, 0, 0))` |
| 卡片标题与正文之间 | 无空行 | `Group(*body)` |
| 文件路径 | 完整显示，宽度不足时按终端单元格折行并保持续行缩进/树干 | `_wrap_card_line` |

---

## 3. 全部工具标题

| 工具 | 运行中 | 成功 |
|---|---|---|
| `ls` | `Listing <path>` | `Listed <path>` |
| `read` | `Reading <path>` | `Read <path>` |
| `write` | `Writing <path>` | `Created <path>`（新建） / `Wrote <path>`（覆盖）；缺少 `artifact.operation` 时保留中性 `write <path>` |
| `edit` | `Editing <path>` | `Edited <path>` |
| `delete` | `Deleting <path>` | `Deleted <path>` |
| `execute` | `Running <cmd>`（运行/等待/中断带 `· <execution_id>`，交互模式追加 `· interactive`） | `Ran <cmd>` |
| `write_stdin` | 不独立显示（更新所属 `execute` 卡片） | 不独立显示（更新所属 `execute` 卡片） |
| `web_search` | `Searching Web "<query>"` | `Searched Web "<query>"` |
| `web_fetch` | `Fetching <url>` | `Fetched <url>` |
| `request_human_input` | `Asking <question>` | `Asked <question>` |
| `compact_conversation` | `Compacting conversation` | `Compacted conversation` |
| `start_task` | `Running SubAgent · <desc>` | `SubAgent · <desc>` |
| `read_task` | `Reading Task <run_id>` | `Read Task <run_id>` |
| `cancel_task` | `Cancelling Task <run_id>` | `Requested Task Cancellation <run_id>` |
| `mcp_list` | `Listing MCP Servers` | `Listed MCP Servers` |
| `mcp_add` | `Adding MCP Server <name>` | `Added MCP Server <name>` |
| `mcp_remove` | `Removing MCP Server <name>` | `Removed MCP Server <name>` |
| 外部 MCP | `MCP · <serverInfo.name> · <tool_name>` | `MCP · <serverInfo.name> · <tool_name>` |
| 其他自定义工具 | 通用卡片（`<tool_name> <operand>`） | 通用卡片（`<tool_name> <operand>`） |

---

## 4. 状态语义

固定状态表达：

```text
⠼ Running npm run build
• Ran npm run build
• Editing /workspace/app.py — waiting for input
• Failed Editing /workspace/app.py
• Failed (exit 1) npm run build
• Timed out npm run build
• Cancelled npm run build
• Failed to start npm run build
• Running npm run build — interrupted (completion unconfirmed)
```

规则：

1. 标题由真实结构化工具状态决定，不根据普通输出文本猜测成功状态。
2. `Failed` 只出现一次；非 `execute` 工具失败时附带进行式短语（如 `Failed Editing /workspace/app.py`）。
3. 等待、失败、取消、中断、未确认一律不使用成功过去式。
4. `request_human_input` 等待输入时标题已为 `Asking ...`，不重复追加 `— waiting for input`。
5. `cancel_task` 成功标题 `Requested Task Cancellation` 只表示取消请求已返回，不代表子任务已停止。

---

## 5. 各类工具卡片布局

### 5.1 Explore

- 仅 `ls`、`read` 参与连续 Explore 聚合；`grep`、`glob`、`execute`、Web、MCP 不进入 Explore。
- 聚合标题动词：有运行中成员用 `Exploring`；全部成功用 `Explored`；无运行成员但存在等待、失败或中断用 `Explore`（并附 `· N failed` / `· N interrupted` / `— waiting for input`）。单数用 `item`，复数用 `items`。
- 成员行与展开后的单卡共用标题；折叠保留末尾 `explore_preview_items` 项（超出顶部显示 `├ … N more`）；失败摘要保留末尾 `explore_failure_items` 项（超出显示 `├ … N more failed`）；底部显示 `Ctrl+O to expand`。
- `Ctrl+O` 展开后拆分为独立工具卡片并显示完整输出。

```text
⠼ Exploring 3 items
  ├ Listed /workspace
  ├ Read /workspace/config.py
  └ Reading /workspace/app.py
  Ctrl+O to expand

• Explored 8 items
  ├ … 3 more
  ├ Read /workspace/config.py
  ├ Listed /workspace/tests
  ├ Read /workspace/app.py
  ├ Read /workspace/main.py
  └ Read /workspace/README.md
  Ctrl+O to expand
```

### 5.2 Create / Edit / Overwrite / Delete

- `Create`、`Edit`、`Overwrite` 共用同一套 `FileSection`、`_changed_rows()`、`_hidden_footer()` 与 `_mutation_tree()` 渲染机制。
- 单文件卡片路径仅在标题出现，正文首行从 `└` 开始，且 `└` 后不叠加额外重复空格；多文件卡片将各路径作为 `├` / `└` 子节点展示（非最后文件正文及折行延续 `│`，最后文件正文仅缩进、无 `│`）。
- 行号槽宽由整张卡片所有实际变更行统一计算：默认宽度 `NUMBER_GUTTER = 4`，当最大行号达到 4 位及以上时扩展为 `max(4, max_digits + 1)`，保证与 `└` / `│` 之间始终保留至少 1 个无背景空格；单文件、多文件、折叠、展开与省略符 `⋮` 共用同一槽宽，渲染层不重新推算。
- 行号右对齐，增删符号（`+` / `-`）与代码正文对齐；`⋮` 右对齐在行号槽内，且仅在该文件存在隐藏变更行时显示；整张卡片底部汇总 `… N changed lines hidden · Ctrl+R to review`。
- Diff 背景色（`#005f00` / `#5f0000`）仅覆盖实际行号、增删符号及代码正文，不覆盖树干、空白缩进或整行终端宽度。
- 同文件连续 `Edit` 合并为一个按真实行号升序、同号新增（`+`）在删除（`-`）之前的 Diff 区域，不显示 `Edit N`，不重新计算文件级净 Diff。
- `Edit` 仅使用当前格式的结构化位置元数据 `artifact.edit_start_lines` 与 `artifact.edit_occurrences`；缺少有效位置元数据（含等待审批或尚未执行的 `Edit`）时仅显示 `Edit` 摘要标题，不推测行号、不显示 `?`。
- 每个文件独立按 `head_lines` 预算折叠；当预算 `>= 2` 且同时存在新增与删除时，至少保留一行 `+` 与一行 `-`。
- 连续同语义操作按 `Create` / `Edit` / `Overwrite` 分别聚合，不跨其他工具或消息。
- `Overwrite` 对同一路径只保留**最后一次**写入结果（先取最后一次 mutation 再检查是否存在 Diff；最后一次写入空文件时不展示前一次写入的旧内容）。
- 运行中与等待审批的 `write` 尚未由 `WriteOperationMiddleware` 写入 `artifact.operation`，因此**不提前推断**新建（`Created`）或覆盖（`Wrote`），而是通过 `_mutation_body()` 的扁平分支展示带行号、语法叠色与折叠的待写内容预览（复用同一套 Diff 数据、排序、折叠与高亮逻辑）；执行完成并获得 `artifact.operation` 后切换为已固化的 `Created` / `Wrote` 树形卡片；失败时走统一失败卡片 `Failed Writing <path>`。
- `Ctrl+R` 与 `Ctrl+O` 共用同一原位展开路径，不拆散聚合卡片；展开后移除 `⋮` 与隐藏提示，不显示 unified diff 头（`---` / `+++` / `@@`）。
- `Delete` 显示 `• Deleted <path>`（运行中 `Deleting <path>`），不参与多文件 Diff 聚合。

```text
⠼ Writing workspace/app.py (+8 -0)
     1+ import os
     2+ import sys
     3+
     4+ def main():
     ⋮
  … 4 changed lines hidden · Ctrl+R to review

• Created /workspace/app.py (+12 -0)
  └   1+ import os
      2+ import sys
      3+
      4+ def main():
      ⋮
  … 8 changed lines hidden · Ctrl+R to review

• Edited 2 files (+6 -2)
  ├ /workspace/config.py (+4 -1)
  │  24+ _model_headers,
  │ 475+ "headers",
  │ 475- "timeout",
  │ 502+ config.get(...)
  │   ⋮
  └ /workspace/client.py (+2 -1)
     32+ session_id,
     56+ default_headers=headers
     56- default_headers=None
  … 1 changed lines hidden · Ctrl+R to review
```

### 5.3 Execute / Write stdin

- 命令正文按终端折行后的 `head_lines` 折叠；实时输出在 `└ output` 下按终端折行后的 `tail_lines` 保留末尾，超出显示 `… N earlier output lines hidden · Ctrl+O to expand`。
- 输出截断与日志路径只显示一次（剥离模型输出尾部重复的 `[Output truncated...]` 提示后统一渲染 `Output truncated · full output: <path>`）。
- 进程未结束时，即使 `execute` 已因 `yield_time` 返回，卡片仍保持 `Running <cmd> · <execution_id>`（交互/PTY 追加 `· interactive`）。
- `write_stdin` 不创建独立卡片，仅将增量输出与终态投影到所属 `execute` 卡片。
- 用户 `/stdin` 直输不伪造工具调用、不产生额外工具卡片；进程真正终止后才更新原 `execute` 卡片终态。

### 5.4 Web 工具

- `web_search` 与 `web_fetch` 保持独立普通工具卡片，折叠时摘要行格式为 `  … <detail> · Ctrl+O to expand`，`Ctrl+O` 展开输出；不进入 Explore，不按搜索提供方分叉样式。

### 5.5 MCP 工具

- 外部 MCP 工具标题固定为 `MCP · {serverInfo.name} · {tool_name}`（缺少服务名时为 `MCP · {tool_name}`，缺少原始工具名时保留完整注册名）；不使用 `Calling MCP` / `Called MCP`，不从 `mcp__` 注册名推测服务名。
- 卡片标题与 Companion Tool Health 复用同一函数 `mcp_preview_title()`；Tool Health 统计仍按完整注册名隔离。
- 折叠显示单行参数摘要与按 `tail_lines` 折叠的末尾输出；`Ctrl+O` 展开显示 `Host (outside sandbox)`、完整结构化参数与完整输出。
- `mcp_list` / `mcp_add` / `mcp_remove` 使用普通工具卡片。

### 5.6 SubAgent

- 每次 `start_task` 渲染为独立卡片，全部深度共用 `_subagent_title()` 与 `_subagent_result()`；嵌套子任务仅增加 `├` / `│` / `└` 树干，非最后一个兄弟任务的结果与换行延续 `│`。
- 每个任务的结果直接位于自己的标题下方，由 `Ctrl+A` 独立控制展开/折叠（提示 `… N output lines hidden · Ctrl+A to expand`）。
- 父任务与子任务各自独立更新真实状态；父任务未完成自身汇总时保持 `Running SubAgent`。
- 后台任务追加 `· background`。
- 不在主界面卡片中展示子 Agent 的思考、内部工具调用、Token、TPS、TTFT 或上下文压缩。
- `read_task`、`cancel_task` 使用普通工具卡片。

### 5.7 人工交互与上下文压缩

- `request_human_input`：`• Asking <question>` → `• Asked <question>`，回答正文位于下方；`secret` 字段仅显示凭据引用名，不显示明文。
- `compact_conversation`：`⠼ Compacting conversation` → `• Compacted conversation`。

### 5.8 Queue 与输入区域布局

固定自上而下相对顺序：

1. `Transcript`（转录区）
2. `Queue messages`（排队消息区）
3. `Working status / Subagent count`（状态栏）
4. `Input editor`（主输入框 / 掩码输入框）
5. `Footer`（双行底栏）

- Queue 的出现、增长、撤回与提交不得改变正常 Working 状态栏与输入框的相对位置。
- 审批交互面板、`/stdin` 进程输入提示条及 `↓ Back to bottom · esc` 保持既有条件显示与位置语义。
- 短终端下 Queue 优先让出行高，并按实际视口高度计算最大滚动偏移，确保压缩后仍可滚动至最底行。

---

## 6. 折叠配置（`UiDisplayLimits`）

| 参数 | 默认值 | 职责 |
|---|---|---|
| `head_lines` | `4` | Diff、命令正文、普通工具失败摘要 |
| `tail_lines` | `3` | 工具输出、思考、SubAgent 结果、命令失败输出 |
| `explore_preview_items` | `5` | Explore 卡片保留的末尾成员数 |
| `explore_failure_items` | `3` | Explore 卡片保留的末尾失败摘要数 |
| `editor_max_lines` | `5` | 主输入框最大显示行数（受终端高度 1/3 约束） |
| `completion_menu_lines` | `8` | 命令补全菜单最大显示行数 |

不为单个工具新增专属折叠参数。

---

## 7. 交互面板（Interaction Panels）

全部人工交互、工具审批与安全凭据输入共用唯一的 `InteractionController`、交互容器、输入区、焦点管理、滚动（`render_interaction_viewport`）与快捷键路由，仅区分三类展示模式（`panel_mode`），不建立独立控制器或重复状态管理器。命令选择器（`/model`、`/resume`、`/permission`、`/sandbox`、`/skill`、`/stdin`、`/mcp`、`/secrets` 辅助选择菜单）继续保持既有 `picker` 样式不变。

### 7.1 三类交互面板与主题色

| 交互类型 | 触发条件 | 标题与选中主题色 | 主要控件 |
|---|---|---|---|
| 普通人工输入（`human`） | `kind == "human"` 且当前字段非 `secret` | 青色（标题 `bold bright_cyan`，选中 `bold cyan`） | 文本、单选、多选（附 `Other:`） |
| 工具审批（`approval`） | `kind == "approval"` | 黄色（标题 `bold yellow`，选中 `bold yellow`） | `Reject`（默认） / `Run` |
| 安全凭据输入（`secret`） | 当前字段为 `type == "secret"`，或独立的存储选择/确认、覆盖确认步骤 | 黄色警示（标题 `bold yellow`，选中 `bold yellow`，掩码输入区黄色边界 `class:secret-editor-border` 与黄色提示符 `class:secret-editor-prompt`） | 独立单行掩码输入（`PasswordProcessor(char="•")`），或独立简短选择步骤 |

- **红色（`red`）** 仅用于实际校验错误、覆盖已有凭据警告（`Warning: ... already exists and will be replaced.`）及宿主沙箱外执行风险（`Execution: Host (outside sandbox)`、`Host command (runs outside the sandbox): ...`）。
- 多字段交互从普通字段切换至 `secret` 字段（或 `Shift+Tab` 返回普通字段）时，视觉模式、输入区与焦点同步切换，并擦除 `secret_buffer` 及回退字段的已输入密钥，不向普通输入框回填明文。

### 7.2 普通人工输入面板

- 标题、问题正文、`reason`、`label` 之间自动去重；单字段不展示通用字段名（如 `Reply` / `回复`）；多字段展示当前进度（`1/2  <字段名>`）。
- 仅在有实际背景时展示简短 `reason`，仅在有实际决策影响时展示简短 `impact`。
- 推荐项直接在对应选项后内联标注 `(recommended)`；仅当推荐理由有额外信息时展示在该选项下方缩进灰字中；不再在选项上方单独渲染重复的 `Recommended:` 区块（仅纯文本无选项字段在存在建议值时显示单行 `Suggested:`）。
- 单选 / 多选只显示选项（模型最多提供 3 项，系统自动附 `Other:`）；选择 `Other:` 后才进入自定义文本输入（显示 `Other: enter your answer`）；多选保留 `[x]` / `[ ]` 与 `Space` 切换语义；纯文本问题直接使用输入框，不附带选项。
- 标题/正文与选项之间、选项与底栏快捷键之间各空一行；单选选项使用 `→ ` / `  ` 前缀，多选使用 `→ [x] ` / `  [ ] ` 前缀。

```text
Choose a database
Which database should the project use?

→ PostgreSQL (recommended)
  SQLite
  MySQL
  Other:

↑↓ select · Enter confirm · Esc close
```

### 7.3 工具审批面板

- 标题固定为 `Approval required`（批量审批为 `Approval required (N actions)`），黄色粗体；默认选中 `Reject`，选项仅保留 `Reject` 与 `Run`，不展示冗余的 `Decision`、`Deny this tool call`、`Approve and execute` 或 `Reject makes no change.` 文案，也不将批准描述为已经执行。
- 各类工具必须完整展示必要操作与真实风险：
  - **Execute**：`Command:`、完整网络权限（`Network access: internet, localhost and LAN` 或 `off`）、真实执行范围（`Execution scope: Sandbox (restricted|full-access)` / `Custom backend`，交互模式附 `· interactive (PTY|pipe)`）。
  - **Write / Edit / Delete**：操作类型（`Write file:` / `Edit file:` / `Delete file:`）、目标路径与必要变更规模信息。
  - **Web**：查询词或目标 URL 与 `Network access: DeepSeek API (host)`。
  - **外部 MCP**：`MCP tool: <server> · <tool>`、红色风险提示 `Execution: Host (outside sandbox)` 与有界结构化 `Arguments:`。
  - **MCP 管理**：添加/删除的服务名、传输类型、URL 或红色风险提示 `Host command (runs outside the sandbox): <cmd argv>`、鉴权/环境变量与 `Target: Host MCP configuration`。
  - **Write stdin**：目标进程 `execution_id` 与实际写入内容、关闭 EOF 或取消操作。
- 批量审批按 `1. ...`、`2. ...` 分段列出全部真实操作，各调用参数严格隔离。

```text
Approval required

Command:
npm install

Network access: internet, localhost and LAN
Execution scope: Sandbox (restricted)

→ Reject
  Run

↑↓ select · Enter confirm · Esc close · F2 reopen
```

### 7.4 安全凭据输入面板

- 当前字段为 `type == "secret"` 时进入安全凭据输入模式：黄色警示标题（`Credential required`；可信 `runtime_auth` 为 `Authentication required`），清晰列出凭据名称与用途，下方紧接黄色边框的独立单行掩码输入区（`•` 掩码，无历史、无粘贴块、无撤销栈残留）。
- 掩码输入界面**不展示**普通问题选项列表、`Other:`、`Recommendation`、额外输入框或内部概念标签（`Secure Input`、`Reference:`、`Scope:`、`Storage: [Session] / Persistent`），也**不再提供**密码输入时的方向键存储切换。
- 存储策略严格由可信宿主运行时信息及消费者约束决定，**不**仅凭 `host` 作用域一律推断持久化，也**不**根据模型生成的文本决定安全策略；模型不得绕过用户确认直接持久化凭据：
  - **`sandbox` 作用域**：默认仅当前会话使用，告知 `Available only for this session (<NAME> · sandbox).` 与 `The agent receives only the credential name.`。
  - **`host` 作用域且匹配已配置全局 MCP 服务**：提交前明确告知 `Stored encrypted on this device for future sessions (<NAME> · MCP: <servers>).`，并在输入后进入独立确认步骤 `Confirm encrypted storage`（默认 `Cancel` / `Store encrypted on this device`），不误称全局 MCP 支持 Session 凭据。
  - **`host` 作用域且未绑定特定已配置 MCP 服务**：掩码输入后进入独立存储选择步骤 `Credential storage`（默认 `This session only` / `Store encrypted on this device`，并明确说明全局 MCP 需要本机加密存储）。
  - **可信 `runtime_auth`**：告知 `Used for this authentication only. Not stored.`，不写入 `SecretStore`。
- 覆盖已有凭据保持独立确认步骤 `Overwrite existing credential?`，红色警告 `Warning: ... already exists and will be replaced.`，默认 `Cancel`。
- 校验错误（如必填为空）以红色显示在底栏提示上方。原始凭据绝不进入普通输入区、`values`、`repr`、ToolMessage、Checkpoint 或日志。

### 7.5 快捷键、状态栏去重与文案规则

- 快捷键提示必须与当前控件及 `Esc` 实际语义一致：
  - 普通人工输入：`↑↓ select · Enter confirm · Esc close`（多选加 `Space toggle`；文本输入为 `Enter submit · Ctrl+J newline · Esc close`；`Other:` 自定义输入为 `Enter submit · Ctrl+J newline · Esc back`；多字段后续步附 `Shift+Tab back`）。
  - 工具审批：`↑↓ select · Enter confirm · Esc close · F2 reopen`。
  - 安全凭据输入：`Enter submit · Esc close · F2 reopen`（`runtime_auth` 与 `/secrets` 为 `Enter submit · Esc cancel`；独立存储/覆盖确认步对应 `↑↓ select · Enter confirm · ...`）。
- 交互面板打开时，状态栏不重复展示面板已有的快捷键提示；关闭待处理交互后，状态栏恢复 `<Approval|Input|Pause> still pending · F2 to decide`，原输入草稿、光标位置、附件与主输入框焦点完整恢复。
- 短终端或窄终端下，`render_interaction_viewport` 固定标题与底栏（含安全告知与错误提示），长选项列表按选中项滚动，无选项长正文以 `…` 折断中间区域。
- **人称与语言规则**（由 `agent/tools/human_input.py` 工具与字段描述从源头约束，不做正则改写或额外模型改写）：模型生成文案简洁自然并跟随当前对话语言；系统固定文案保持一致术语；不在 `question` 中复述 `fields` 选项；选项使用客观的操作名、方案名或结果描述，**禁止**依赖说话者视角的第一、第二人称（如“我来…”、“你去…”、“I will…”、“You…”）。
