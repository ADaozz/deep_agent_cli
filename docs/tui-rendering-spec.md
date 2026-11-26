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
