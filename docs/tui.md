# 终端界面

## 启动

```bash
deep-agent                    # 在当前目录开始新会话
deep-agent resume             # 打开会话列表
deep-agent resume <id或前缀>  # 恢复指定会话
deep-agent --help
deep-agent --version
```

- `--help` 和 `--version` 不读取配置，不启动沙箱和模型。
- 首次运行只生成配置模板并退出，即使 stdin 来自管道。
- 之后 stdin 和 stdout 都必须是终端，否则输出错误并以退出码 2 退出。
- 源码检出中也可以运行 `python -m agent.cli.main` 或 `python examples/run_cli.py`。
- 退出时，如果当前会话有内容，会打印恢复它的命令。

## 命令

输入 `/` 显示候选，方向键选择，`Tab` 或 `Enter` 填入。`/model ` 和 `/permission ` 后面的参数也会补全。

| 命令 | 作用 |
|---|---|
| `/help` | 命令和快捷键列表 |
| `/status` | 运行状态、模型与推理强度、权限、会话 id、执行模式、工具数 |
| `/session` | 当前会话信息与数据库路径 |
| `/new` | 新建会话（首次发送消息后才保存） |
| `/resume` | 列出有内容的会话并选择；`/resume <id前缀>` 直接切换 |
| `/model` | 依次选择来源、模型、推理强度；`/model <来源>` 或 `/model <来源/模型>` 跳过前几步 |
| `/permission ask\|allow` | 切换权限模式，见 [sandbox.md](sandbox.md#权限模式) |
| `/compact` | 手动压缩上下文 |
| `/image <路径>` | 为下一条消息附加图片；`/image clipboard` 从剪贴板读取；`/image` 列出；`/image clear` 清空 |
| `/attachments cleanup` | 删除不再被任何会话引用的图片文件 |
| `/pause` | 在下一个模型安全点暂停 |
| `/clear` | 清空屏幕上的转录内容，不影响 Agent 上下文 |
| `/quit`、`/exit` | 退出 |

## 快捷键

| 按键 | 作用 |
|---|---|
| `Enter` | 提交；任务运行中提交为 steering |
| `Ctrl+J` | 换行 |
| `Alt+Enter` | 排队为 follow-up，当前任务结束后执行 |
| `Esc` | 取消当前任务，并把未发送的 steering 还原到输入框；转录区已上滚时先回到底部 |
| `Alt+Up` | 取回尚未生效的 steering / follow-up |
| `F2` | 重新打开待处理的审批或提问 |
| `Ctrl+O` | 展开 / 收起工具详情 |
| `Ctrl+T` | 展开 / 收起思考内容 |
| `Ctrl+R` | 查看 `write_file` / `edit_file` / `delete` 的完整改动 |
| `Ctrl+P` / `Alt+P` | 切换到下一个 / 上一个模型 |
| `Ctrl+V` / `Alt+V` | 粘贴图片或文本 |
| `Ctrl+C` | 清空输入；0.5 秒内再按一次退出 |
| `Ctrl+D` | 退出 |
| 滚轮、`PgUp` / `PgDn`、`Ctrl+Home` / `Ctrl+End` | 滚动转录区；`Ctrl+End` 回到底部并恢复跟随 |
| 鼠标左键拖动 | 选择转录内容，松开后复制到系统剪贴板；拖到边缘会自动滚动 |

按键可在 `keybindings.json` 中修改，见 [configuration.md](configuration.md#按键keybindingsjson)。部分终端（如 Windows Terminal）会占用 `Ctrl+V`，此时用 `Alt+V`。

## 运行中的输入

- **Steering**（`Enter`）：在下一次模型调用之前或模型提出的工具调用开始之前交给 Agent。若模型返回时已有 steering 在排队，这批尚未进入审批或执行的工具调用会被丢弃，模型带着新输入重新推理。已经在等待审批的调用仍需处理，已经开始执行的工具会先完成。
- **Follow-up**（`Alt+Enter`）：当前任务结束后作为新一轮输入执行。
- **取消**（`Esc`）：终止正在运行的命令（向进程组发送 `SIGKILL`），结束本轮。
- 切换或新建会话时，未生效的 steering / follow-up 会退回输入框。

## 模型切换

- `/model` 只能在空闲时打开。`Esc` 逐步返回上一级。
- `Ctrl+P` / `Alt+P` 在运行中也可用：切换会排队（提示 `Model queued: …`），在当前工具流程结束、下一次模型调用之前生效。
- 有待处理的审批或暂停时，切换同样排队。
- 有未发送的图片附件时不能切换模型。
- 底栏按 `来源 · 模型 · 推理强度` 显示当前模型。模型列表显示实际 API 模型名，命令参数使用配置中的 `来源/模型键`。

## 审批与提问

- `ask` 模式下，需要审批的工具调用会弹出面板，选择 Run 或 Reject。
- Agent 调用 `request_human_input` 时弹出提问面板，字段类型有单行文本、多行文本、单选、多选、布尔。选择题自动附带 `Other:` 选项用于自定义输入。
- 关掉面板后按 `F2` 重新打开。

## 图片

- 当前模型（或排队中的模型）必须在配置中声明 `input: [text, image]`。
- 格式：PNG、JPEG、WebP、GIF；单张不超过 10 MiB；每条消息最多 4 张。
- 添加方式：`/image <路径>`；粘贴一个指向图片文件的路径；WSL 下也可以从 Windows 剪贴板粘贴图片或文件，或粘贴 Windows 路径（自动转换）。
- 剪贴板图片读取通过 `powershell.exe` 实现，**只在 WSL 中可用**。其他环境请用路径。
- 图片保存在会话数据库旁的 `.attachments` 目录，会话中只存引用，请求模型时才读取并编码。

## 长文本粘贴

粘贴 500 字符及以上的文本时，输入框显示为 `[Pasted Content N chars]` 块，块前后仍可编辑；提交时还原为完整内容。

## 复制

拖选转录区后按以下顺序尝试复制：WSL 的 `clip.exe`、`pbcopy`、`wl-copy`、`xclip`、`xsel`，都不可用时发送 OSC 52 转义序列（需要终端支持）。

## 显示

- **思考区**：默认折叠，保留最后 5 行（`ui.thinking_tail_lines`），`Ctrl+T` 展开。
- **命令输出**：实时滚动显示最后 4 行（`ui.execute_tail_lines`），`Ctrl+O` 展开。失败时显示退出码和简短错误。
- **探索类工具**（`ls`、`read_file`、`glob`、`grep`）连续调用时合并为 `Explored N items`。
- **文件写入**：新建显示 `Create <路径>`，连续新建合并为 `Create N files`；覆盖写入显示前 6 行；`Ctrl+R` 查看完整内容。
- **任务计划**：Agent 调用 `write_todos` 时显示当前计划，恢复会话时一并恢复。
- 每轮结束显示 `Worked for <耗时> · <结束时间>`，时区由 `ui.timezone` 决定。恢复会话时不重建历史回合的耗时行。

所有折叠行数可在配置的 `ui` 部分调整，见 [configuration.md](configuration.md#界面ui)。

## 状态栏与底栏

- 输入框上方的状态栏显示运行状态（如 `Working…  Esc to cancel`）、排队数量和提示；运行时旁边轮换显示 `working_messages.yaml` 中的文案。
- 底栏第一行：工作区路径、当前模型、会话 id；右侧为 Git 分支和改动文件数。
- 底栏第二行：运行状态、执行模式、权限模式；右侧为上下文占用。
- 上下文占用 = 模型最近一次返回的 token usage / `context_window`。端点不返回 usage 时只显示窗口大小；`context_window` 为 0 时不显示。
- Git 状态每 5 秒在后台刷新，单次探测最长 10 秒。可能的显示：`⎇ checking git`、`⎇ no git`、`git unavailable`、`git error`、`git timeout`。

## 会话

- 每个工作区一个 SQLite 文件，默认 `~/.deep-agent/sessions/<哈希>.sqlite3`。
- 新会话（启动时或 `/new`）先只在内存中，首次发送消息时才写入。只启动、切换模型或退出不会留下空记录。
- 恢复只加载消息、计划和待处理的审批 / 提问，**不会自动调用模型或重新执行工具**。
- 恢复时，没有保存结果的工具调用显示为 `interrupted (completion unconfirmed)`。
- 如果上一轮异常结束、被取消或出错，恢复后的第一次模型请求会附带一段说明，提醒模型先检查工作区状态；这段说明不写入会话历史。
- 同一会话同一时间只能被一个进程打开。目标会话被其他窗口占用时，`/resume` 和 `deep-agent resume <id>` 会进入等待，`Esc` 取消等待。
- 会话保存其模型、推理强度和权限模式。若保存的模型已不在配置中，恢复时提示并改用 `llm.default`。

## 上下文压缩

- 自动：由 Deep Agents 处理，上下文接近窗口上限时摘要旧消息，见 [architecture.md](architecture.md#上下文压缩)。期间状态栏显示 `Compacting context`。
- 手动：`/compact`。要求最近一次模型 usage 达到自动阈值的一半（配置了 `context_window` 时约为窗口的 42.5%），未达到时显示当前占用。运行中或有待处理的交互时不可用。
