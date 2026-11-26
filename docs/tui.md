# 终端界面

## 启动

```bash
deep-agent                    # 在当前目录开始新会话
deep-agent resume             # 打开会话列表
deep-agent resume <id或前缀>  # 恢复指定会话
deep-agent --help
deep-agent --version
```

- 子命令只有 `resume`。不带子命令时，在当前目录开始新会话。
- `--help` 和 `--version` 不读取配置，不启动沙箱和模型。
- 首次运行只生成配置模板并退出，即使 stdin 来自管道。
- 之后 stdin 和 stdout 都必须是终端，否则输出错误并以退出码 2 退出。
- 源码检出中也可以运行 `python -m agent.cli.main` 或 `python examples/run_cli.py`。
- 退出时，如果当前会话有内容，会打印恢复它的命令。

## 命令

输入 `/` 显示候选，按连续子串匹配（例如 `/elp` 匹配 `/help`），完整匹配和前缀匹配优先。继续输入可缩小候选范围，`Tab` 或 `Enter` 填入当前候选，退格后重新显示符合当前输入的候选。`/model `、`/permission ` 和 `/sandbox ` 后面的参数也按连续子串补全。

| 命令 | 作用 |
|---|---|
| `/help` | 命令和快捷键列表 |
| `/lain [问题\|stats]` | 开关或向右侧 Lain Companion 提问；`/lain stats` 查看上轮工具明细（无需模型），见 [companion.md](companion.md) |
| `/status` | 查看模型、目录、权限、会话信息和上下文剩余量 |
| `/status details` | 运行状态、模型与推理强度、输入能力、工具数及其名称、按模型与强度分组的会话 TPS 统计 |
| `/new` | 继承当前模型、推理强度、权限和沙箱模式，新建空会话（首次发送消息后才保存） |
| `/resume` | 列出有内容的会话并选择；`/resume <id前缀>` 直接切换 |
| `/model` | 依次选择来源、模型、推理强度；`/model <来源>` 或 `/model <来源/模型>` 跳过前几步 |
| `/skill` | 打开技能选择器，把技能块插入输入框；不接受参数 |
| `/permission ask\|auto\|allow` | 切换权限模式，见 [sandbox.md](sandbox.md#权限模式) |
| `/mcp` | 查看和管理宿主 MCP 服务（`enable`/`disable`/`add`/`remove`/`test`/`tools`），见 [mcp.md](mcp.md) |
| `/secrets` | 管理宿主 `SecretStore` 凭据（`list`/`set`/`update`/`delete`，掩码输入），见 [secrets.md](secrets.md) |
| `/stdin [execution_id\|off]` | 进入当前会话运行中交互进程（`pipe` / `pty`）的直输模式，`/stdin off` 或 `Esc` 脱离，见 [execution.md](execution.md) |
| `/sandbox restricted\|full-access` | 切换文件系统模式。`full-access` 写入当前真实环境，见 [sandbox.md](sandbox.md#文件系统模式) |
| `/compact` | 手动压缩上下文 |
| `/image <路径>` | 为下一条消息附加图片；`/image clipboard` 从剪贴板读取；`/image` 列出；`/image clear` 清空 |
| `/attachments cleanup` | 删除不再被任何会话引用的图片文件 |
| `/pause` | 在下一个模型安全点暂停 |
| `/clear` | 清空屏幕上的转录内容，不影响 Agent 上下文 |
| `/exit` | 退出 |

## MCP 服务

`/mcp` 先显示全局开关。开启时列出服务，例如：

```text
MCP: Enabled

Servers:
  codex_apps   connected       294 tools
  codex_tui    connected        12 tools
  psd2live     failed            0 tools
```

`connected` 为绿色，其余状态为红色：

| 状态 | 含义 |
|---|---|
| `connected` | 已连接，同一行给出已发现的工具数量 |
| `failed` | 连接失败或服务配置无效 |
| `disconnected` | 有配置，尚未连接 |
| `auth_required` | 尚未完成授权或鉴权失败 |
| `disabled` | 该服务在 `mcp.json` 里是 `enabled: false`，全局开关打开时也不会自动连接 |

全局关闭时不列出连接状态：

```text
MCP: Disabled

3 servers configured.
Connections and tools are inactive.
```

Agent 正在运行或仍有待决审批时，`/mcp disable` 只排队，提示 `MCP disable queued until the next safe point`，并要求先结束当前操作；有待决审批时额外提示 `Resolve the pending approval first.`。工具表真正切换之后才显示上面的 `MCP: Disabled`。待决审批保留，不会被丢弃。空闲时会立即重建 Graph。`/mcp enable` 写回配置后自动连接已启用的服务，并在安全点恢复 MCP 工具。

使用 `/mcp tools <服务名>` 查看工具；服务不可用时显示错误详情。输入时会补全 `enable`、`disable`、`add`、`remove`、`test`、`tools`，服务名需要手动填写。`/mcp remove <服务名>` 删除配置，连接在安全点释放。要停用全部 MCP，执行 `/mcp disable`；要只停用一个服务并保留它的配置，把该服务的 `enabled` 改为 `false`。关闭全局开关不会删除 `mcp.json` 或 OAuth 令牌缓存。重复执行已经生效的 `/mcp enable` 或 `/mcp disable` 只显示当前状态，不会重新连接或再次写配置。

启动时，只有全局开关为开启才自动连接已启用的服务。无需鉴权的服务直接连接；OAuth 使用缓存凭证，令牌过期先自动刷新，首次授权或刷新失败时打开浏览器。同一进程中每个服务 60 秒内最多打开一次授权页，用于防止重复弹窗，并非每隔 60 秒重新授权。取消或超时后可稍后执行 `/mcp test <服务名>` 重试。工具审批由 `/permission` 控制，浏览器授权由用户完成。全局关闭期间执行 `/mcp test` 或其他修改命令会要求先 `/mcp enable`，并且不会打开浏览器。

MCP 服务端日志、OAuth 客户端日志及相关 SDK 警告只写后台文件 `~/.deep-agent/logs/mcp-<进程号>.log`，不显示在 TUI。自定义配置目录时，日志位于该目录的 `logs/` 下。凭证缓存、连接恢复和资源返回方式见 [MCP 配置](configuration.md#mcpmcpjson)。

退出时会取消尚未完成的 MCP 连接与 OAuth 授权等待，关闭相关连接后再显示继续会话的命令。退出清理期间的 MCP 日志仍写后台文件。

## 快捷键

| 按键 | 作用 |
|---|---|
| `Enter` | 提交；任务运行中提交为 steering |
| `Ctrl+J` | 换行 |
| `Alt+Enter` | 排队为 follow-up，当前任务结束后执行 |
| `Esc` | 有排队消息时撤回到输入框，否则中断当前任务；转录区已上滚时先回到底部 |
| `Alt+Up` | 取回尚未生效的 steering / follow-up |
| `F2` | 重新打开待处理的审批或提问 |
| `Ctrl+O` | 展开 / 收起工具详情 |
| `Ctrl+A` | 展开 / 收起 SubAgent 结果 |
| `Ctrl+T` | 展开 / 收起思考内容 |
| `Ctrl+B` | 展开 / 收起已发送的长消息 |
| `Ctrl+R` | 在原卡片展开 / 收起 `write` / `edit` / `delete` 的全部变更行；`Ctrl+O` 对文件变更走同一条展开 |
| `Ctrl+P` / `Alt+P` | 切换到下一个 / 上一个模型 |
| `Ctrl+V` / `Alt+V` | 粘贴图片或文本 |
| `Ctrl+C` | 清空当前的主对话或玲音输入框；空闲时提示约 1 秒后消失；1 秒内再按一次退出 CLI，切换焦点后也有效 |
| `Ctrl+D` | 输入为空时退出；非空时删除光标后的字符 |
| 滚轮、`PgUp` / `PgDn`、`Ctrl+Home` / `Ctrl+End` | 滚动转录区；`Ctrl+End` 回到底部并恢复跟随 |
| `←` / `→` | 输入框为空且没有待发送附件时，跳到上一轮 / 下一轮用户消息 |
| `↑` / `↓` | 有草稿时按屏幕显示行移动光标；输入框随内容增高，超过五行时内部滚动 |
| 鼠标左键拖动 | 选择转录内容，松开后复制到系统剪贴板；拖到边缘会自动滚动 |

按键可在 `keybindings.json` 中修改，见 [configuration.md](configuration.md#按键keybindingsjson)。部分终端（如 Windows Terminal）会占用 `Ctrl+V`，此时用 `Alt+V`。

## 运行中的输入

- **Steering**（`Enter`）：在下一次模型调用之前或模型提出的工具调用开始之前交给 Agent。若模型返回时已有 steering 在排队，这批尚未进入审批或执行的工具调用会被丢弃，模型带着新输入重新推理。已经在等待审批的调用仍需处理，已经开始执行的工具会先完成。
- **Follow-up**（`Alt+Enter`）：当前任务结束后作为新一轮输入执行。
- 已有排队消息时，后续输入继续追加到队尾；steering 不会越过更早入队的 follow-up。
- 排队消息显示在 TUI 的独立区域，位于 Working 状态栏上方，标题为 `Messages to be submitted after next tool call (press esc to restore to input, enter twice to interrupt and send immediately)`，下方按 `↳ 消息` 逐条显示。状态栏始终紧贴输入框：消息出现、增加或撤回时只改变排队区自身，不会把 Working 顶离输入框；短终端下排队区优先让出行高。可滚动查看；实际取用后才进入对话记录。按 Esc 或 Alt+Up 撤回队列到输入框，不中断当前任务；与 Ctrl+C 使用相同的 1 秒双击窗口；连续按两次 Enter，中断当前任务并在停止后发送队列，同时在对话区显示 `• Model interrupted to submit steer instructions.`。任务结束交接期间收到的消息也会自动继续处理。
- **取消**（`Esc`）：终止正在运行的命令（向进程组发送 `SIGKILL`），结束本轮。
- 切换或新建会话时，未生效的 steering / follow-up 会退回输入框。

## SubAgent

只有问题能拆分为多个独立、互不干扰的子问题时，主 Agent 才应通过 `start_task` 派生 SubAgent。
主 Agent 会等待同步任务的结果，也可在后台任务运行期间继续工作。[委派配置](configuration.md#subagent-委派)说明嵌套、并发和日志权限。

运行期间，`1 subagent` / `2 subagents` 与 `⠼ Working…` 显示在同一条状态行，Working 靠左，数量靠右，数量为零时隐藏。输入框内不显示子任务数量。
每个 `start_task` 调用独立显示一张 SubAgent 卡片，不与探索工具合并。标题和状态点与其他工具相同：运行中是绿色盲文转圈，完成后是绿色 `•`，失败或取消是灰色 `•`。任务描述只在标题中归一化并截断，不改变实际委派参数。该任务自己的结果写在标题下方，默认按 `ui.tail_lines` 折叠：提示下面保留末尾几行，`Ctrl+A` 展开全文。卡片和后面的工具执行信息之间空一行。卡片不显示 Token、TPS、TTFT 或上下文压缩状态，也不再用 `└ result` 把总结果接到子任务后面。

卡片状态来自任务记录里的 `status`。没有终态记录时显示完成状态未确认，不根据正文猜测成功或失败。父卡片结束不会改写子任务自己的状态。

一级任务如果派生了二级任务，每条二级任务用 `├` / `└` 嵌在同一张卡片里，标题和结果沿用上面这一套，不按深度另写样式。文件变更行用同一套短标记。每个子任务的结果写在自己那一行下面，折叠方式与上面相同。不是最后一个兄弟时，结果行在 `├` 那一列保留 `│`，一直连到下一个 `└`。某一行结束后立即更新，不影响仍在运行的兄弟任务。父任务要等自己的调用真正结束才显示完成，因为此时它可能还在汇总。二级任务的原始工具调用、思考和日志不会进入这张卡片，只显示它自己的最终结果。

右侧数量统计仍在执行的一级任务卡片，再加上尚未结束的二级任务。调度器里只是在关闭 HTTP 流、已经不再执行的任务不计入这个数量，也不单独驱动界面，避免启动、完成或取消时两处显示不一致。

后台派发（`start_task` 且 `run_in_background=true`）使用同一种卡片，标题末尾标注 `background`。启动后主 Agent 继续工作，卡片保持运行状态，
本轮回答结束后也不会结算为中断；此时状态行显示 `Background  Esc to cancel` 和运行数量。子任务完成后卡片原地显示结果，
主 Agent 空闲时自动开始新一轮处理该结果，不产生用户消息；有待审批或对话框时结果排队，等该次运行恢复后再处理。
主 Agent 空闲时按 Esc 只取消后台任务，被取消的任务不会唤醒主 Agent。新建或切换会话会取消后台任务并丢弃其结果。

`deep-agent resume` 通过 `tool_call_id` 恢复原任务描述和结果；不读取 JSONL、不额外调用模型，也不重建运行中的任务树。子 Agent 内部思考、文件工具、execute 和内部日志仍不转发到主界面。

Root 的任务树可以看到整棵树的名称、层级、状态和耗时，但 `read_task` 仍只返回调用者的直属子任务：Root 读不到 Depth 2，Depth 1 也读不到兄弟分支的子任务。
子任务的权限跟随主 Agent：Auto 子任务使用相同的 Auto 审批，需要人工确认时返回父任务处理；`allow` 下子任务使用沙箱 `allow`。子任务没有审批和主动提问入口，因此 `ask` 下主 Agent 不能派生新的子任务，已在运行的后台任务仍可查询、等待和取消。同步子 Agent 在深度允许时可以继续同步派发；后台子 Agent 不能再派发。子任务不装载 MCP。
Esc 取消当前 Run 时会传播到全部关联子任务（包括后台任务）；退出沿用既有 Ctrl+C、Ctrl+D 和 `/exit` 规则。

主模型切换只影响下一次派生使用的模型 Profile；示例配置 `subagents.reasoning_effort: default` 使用子模型原生默认强度，不向 API 发送强度参数，
不受主 Agent 当前 high/low 影响。执行摘要仅包含操作、状态、耗时及有界最终结果，不保存模型 Token 或思考；按需查询见 [配置参考](configuration.md#subagent-委派)。

## 模型切换

- `/model` 只能在空闲时打开。`Esc` 逐步返回上一级。
- `Ctrl+P` / `Alt+P` 在运行中也可用：切换会排队（提示 `Model queued: …`），在当前工具流程结束、下一次模型调用之前生效。
- 有待处理的审批或暂停时，切换同样排队。
- 有未发送的图片附件时不能切换模型。
- 底栏按 `来源 · 模型 · 推理强度` 显示当前模型。模型列表显示实际 API 模型名，命令参数使用配置中的 `来源/模型键`。

## 审批与提问

三类交互面板（普通人工输入、工具审批、安全凭据输入）的视觉主题、布局、信息去重、风险/存储提示与快捷键规范统一维护在 [TUI 展示规范 §7](tui-rendering-spec.md#7-交互面板interaction-panels)，此处仅保留操作说明：

- `ask` 模式下，需要审批的工具调用会弹出黄色审批面板，默认选中 `Reject`，可选择 `Run` 或 `Reject`。正在输入草稿或有待发送附件时，只显示待审批提示，不打断输入；按 `F2` 打开审批，处理或按 `Esc` 关闭后恢复草稿、光标、附件与焦点。等待审批时提交的新消息进入队列，原审批仍需明确处理。
- Agent 调用 `request_human_input` 时弹出青色提问面板，支持单行/多行文本、单选、多选、布尔及安全凭据（`secret`）。每道选择题最多 3 个模型提供的客观选项，系统另行附带 `Other:` 自定义输入（选中后才展开输入框）。
- **安全凭据输入（`secret`）**：当前字段为 `secret` 时自动切换为黄色警示模式与独立单行掩码输入区（`•` 掩码，不写入普通输入框、历史记录、粘贴块或撤销栈）。`sandbox` 凭据默认仅本会话使用；需要选择或确认本机加密存储（如全局 MCP 凭据）以及覆盖已有凭据时，均通过独立简短确认步骤完成，不与密码框混合显示选项。提交后先写入宿主 `SecretStore`，**仅将凭据引用名返回给 Agent**，明文不进入 Graph 状态、Checkpoint 数据库、ToolMessage 或 RunEvent。复杂凭据管理使用 `/secrets`。
- 面板打开时状态栏不重复展示快捷键提示；按 `Esc` 关闭待处理交互后，待处理提示恢复到原 Working 状态栏位置（不追加到聊天记录），按 `F2` 重新打开。
- 正在输入 `ALLOW` 时，新的审批不会替换确认栏。`F2` 打开审批，`Esc` 回到确认；确认结束或取消后，审批面板再出现。

## 运行中进程直输（`/stdin`）

- 当本会话有正在运行的 `pipe` 或 `pty` 命令时，可执行 `/stdin`（或 `/stdin <execution_id>`）切换到进程输入模式；输入框上方显示 `⌨ Process stdin · <execution_id> (<mode>)` 提示条，与普通聊天输入明确区分。
- 进程输入模式下：`Enter` 经由异步 FIFO 队列直接向该进程 stdin 写入一整行（等待 ACK 不阻塞 TUI 事件循环，也不生成聊天消息、不伪造 `write_stdin` 工具调用、不写入聊天历史或消费 Agent 的未读输出窗口）；`Ctrl+D`（输入框为空）发送 EOF；`Ctrl+C`（输入框为空）绕过排队中的未发输入立即向 PTY 发送 `\x03` 或取消 PIPE 进程；`Esc` 或 `/stdin off` 脱离进程输入模式并返回聊天输入，**不会终止正在运行的进程**。
- 附加到 PTY 进程时自动同步终端尺寸（`TIOCSWINSZ`）。进程退出、取消或切换会话时自动退出进程输入模式。
- 普通进程直输通道**不是**安全凭据输入通道；输入敏感令牌或密码应使用 `request_human_input` 的 `secret` 字段或 `/secrets`。

## Skills

`/skill` 只在空闲且没有待处理交互时可用，不接受参数。选择后在输入框插入 `[Skill: 名称]` 块，可在其后追加任务说明。提交时还原为“使用技能 …，先读取 /skills/…/SKILL.md 并遵循其说明。”；编辑该标记后需还原或删除才能提交。

缺少有效 `name` / `description` 的技能目录不进入列表。Skills 目录以读写方式映射到 `/skills`，Agent 可以在这里安装 skill，并持久化到宿主的 skills 目录。Skill 提供操作说明，不负责安装执行工具。工具目录和版本由用户通过挂载及 PATH 配置；默认模板不自动选择 NVM 版本，`/status` 也不执行工具链预检。挂载与环境变量见 [sandbox.md](sandbox.md#工具目录与环境变量)，浏览器设置见 [Playwright 与有头 X11](sandbox.md#playwright-与有头-x11)。

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

- **标题**：显示 `DeepAgentCLI v<版本号>`，版本与 `deep-agent --version` 使用同一来源；顶部不再显示第二行快捷键提示，完整操作见 `/help`。
- **思考区**：默认折叠，保留按当前终端宽度折行后的最后 3 行正文（`ui.tail_lines`），标题和隐藏提示另计；`Ctrl+T` 展开。
- **顶部提问预览**：当本轮用户消息完全滚出视口上沿时，顶部显示相对轮次和单行提问。最新一轮标为 `current`，之前的轮次依次标为 `1 before`、`2 before`。回滚到原消息时预览自动收起。
- **用户长消息**：发送后默认显示前 3 个终端显示行，超出时显示隐藏行数和 `Ctrl+B` 提示。`Ctrl+B` 切换长消息的展开状态；只改变显示，发送给模型和会话中保存的内容保持完整。待发送队列保持可见，可滚动查看。
- **工具与文件变更卡片**：全部工具卡片的标题、状态标记、颜色、树形连接、行号对齐、聚合与展开（`Ctrl+O` / `Ctrl+R` / `Ctrl+A`）及折叠规则统一维护在 [TUI 展示规范](tui-rendering-spec.md)，本文不再重复定义。代码语法高亮（文件变更、`read` 展开与助手 Markdown 围栏）共用独立的 `CodeRenderer`（Pygments Lexer + `VSCodeDarkPlusStyle`）。
- 每轮结束显示 `Worked for <耗时> · <结束时间>`，时区由 `ui.timezone` 决定。耗时写入会话数据库，恢复会话时重建历史回合的耗时行；升级前未记录耗时的旧回合不补算。

所有折叠行数可在配置的 `ui` 部分调整，见 [configuration.md](configuration.md#界面ui)。

## 状态栏与底栏

- `/status details` 按“模型 + 推理强度”分组，例如 `deepseek-flash-low`；每组下方仅缩进显示 5% low / 5% top / Average TPS 三项。状态栏不显示性能指标，任务完成后照常隐藏。
- 输入框上方的状态栏显示运行状态（如 `Working…  Esc to cancel`）、运行中的 SubAgent 数量和提示；运行时旁边轮换显示 `working_messages.yaml` 中的文案。排队内容显示在状态栏上方的独立区域，不改变状态栏与输入框的位置。
- 权限或模型切换排队、输入清空、复制成功等临时提示在该位置显示 1 秒后恢复原状态；工作中、取消中、交互选择和待审批状态持续显示。临时提示自动消失不会删除聊天记录中的相关消息。
- 底栏第一行：工作区路径、当前模型、会话中文摘要（无摘要时显示 thread_id）。当前用户家目录缩写为 `~/`，例如 `/home/neo/project` 显示为 `~/project`；仅改变显示，不改变实际访问路径。
- 底栏第二行：运行状态、执行模式、权限模式；右侧为上下文占用。
- 上下文占用 = 模型最近一次返回的 token usage / `context_window`。端点不返回 usage 时只显示窗口大小；`context_window` 为 0 时不显示。


### Lain Companion

终端至少 120 列、32 行时，右侧 28% 的区域显示六种 Lain 表情、CPU / GPU / RAM / DISK、Git、Services、Tool Health 工具调用热力图和聊天。DISK 显示工作区所在文件系统的使用率。Git 每 5 秒刷新，显示分支与状态。Services 无服务时显示 `0 running`，有服务时显示名称、PID 和监听端口，`ns:` 标记沙箱内 PID。聊天区占据剩余高度，输入框固定在底部。可用鼠标左键拖选侧栏文字，松开时复制到剪贴板。

按 `Ctrl+→` 进入玲音聊天，`Enter` 发送，`Ctrl+←` 或 `Esc` 返回主输入；也可以输入 `/lain <问题>`。`/lain stats` 查看上轮工具明细。完整统计口径、Tool Health 规则、只读 RPC 权限边界与 GIF 渲染器对比见 [Lain Companion](companion.md)。

### `/status details` 性能统计口径

- **Token 来源**为服务端返回的 usage：Chat Completions 使用 `completion_tokens`，Responses 使用 `output_tokens`，经 LangChain 统一为 `output_tokens`。不按字符数或 chunk 数估算，也不包含输入 token。Thinking 已计入完整输出，不能再次相加；推理明细分别来自 `completion_tokens_details.reasoning_tokens` 和 `output_tokens_details.reasoning_tokens`，Answer token 数为完整输出减推理 token 数。
- **时间来源**为客户端 `on_llm_new_token()` 回调中的 `monotonic()`。同步路径先经过 `CancellableStreamBridge` 队列，再由消费者处理回调，因此记录的是内容被回调处理的时间，可能包含队列等待、线程调度和本地回调处理延迟，不是网络刚收到 chunk 的时间或服务端生成时间。
- **Total Output TPS** = 服务端完整输出 token 数 / 首个到最后一个有效生成 chunk 的时间。输出 token 已包含 Thinking，不再次相加；分母排除 TTFT、末尾 usage/完成事件、工具执行及失败重试的输出片段。
- **Reasoning / Answer TPS** 分别使用推理 token 与非推理 token（总输出减推理）除以各自首末 chunk 的时间。非推理输出包含生成的工具参数，不包含工具执行。阶段之间的空隙不计入阶段耗时，但仍包含在总输出跨度内。
- **TTFT** 为模型调用开始到首个有效生成 chunk 的时间；如果调用中发生重试，包含先前失败和退避等待。计时使用客户端单调时钟，网络、缓冲和 chunk 粒度会影响精度。
- 不按厂商或模型名猜测计数；usage 明细缺失时不计算拆分 token 与 TPS，完整输出计数仍保存在数据库中。
- 如果模型隐藏推理过程，或只返回推理摘要，无法从流中测出完整生成时间，不计算 Total Output / Reasoning TPS，该请求不计入汇总样本；有足够正文时间样本时内部仍可计算 Answer TPS。实际采用的 `additional_kwargs.reasoning_summary`、内容块 summary 和 Responses 摘要事件都视为摘要；Qwen 的完整 `reasoning_text` 仍按完整推理流识别。非流式、单 chunk 或零时间跨度同样不编造 TPS。
- **5% low / 5% top / Average TPS** 基于当前会话内同一模型与推理强度的有效 Total Output TPS 样本，前两者分别取最慢 / 最快 5% 样本的算术平均（样本数向上取整），Average 为全部有效样本的算术平均。工具执行、等待、失败或取消不清空统计；上下文压缩不参与。每次成功调用结束后将原始计数、计时和指标写入会话 SQLite 数据库；重启、恢复或切回会话时加载记录并重算各组统计，模型和强度之间不混算。`/clear` 仅清空显示，保留性能记录；新会话从空记录开始。更新前未采集的历史耗时无法补算。
- **TPS 仅供参考**，不等同于服务端解码 TPS。网络延迟、服务端缓冲、chunk 粒度及本地消费者延迟均可能影响结果，短请求尤其容易波动。Average 是逐次请求 TPS 的算术平均，不是总 token 数除以总耗时；不同 API 或测试环境的结果需核对口径后再比较。本次摘要识别修复不追溯修改已有历史样本。

## 会话

- 每个工作区一个 SQLite 文件，默认 `~/.deep-agent/sessions/<哈希>.sqlite3`。
- `/resume` 和 `deep-agent resume` 的列表优先显示中文摘要，没有摘要时显示 thread_id；选择项始终绑定真实会话 ID。同名摘要不会合并会话，`/resume <id>` 和 ID 前缀恢复保持可用。
- 新会话收到第一条用户提问后，后台立即使用当前模型请求一次中文标题，主任务同时开始。标题只根据第一条提问生成；优先动词加对象并保留技术名称。超时、失败、取消或标题校验不通过时，回退为脱敏后用户提问首行截断至 10 个字；提问本身也无法形成合法回退时，继续显示 thread_id。摘要只生成一次，后续轮次不自动改写。
- 标题生成使用独立客户端，不调用工具、不写入聊天历史、不计入主任务的 TPS，不阻塞下一轮输入。生成超时为 10 秒，不自动重试。退出时取消尚未完成的生成请求。
- 请求与结果在生成前先把配置中的模型密钥替换为 `$llm.…` 路径名，以及 `redaction.replacements` 中的字面量替换为自定义名称；公开网址与路径保留。沙箱/MCP 环境变量不自动脱敏。标题保存前再次校验（空标题、过长、控制字符、列表前缀、旧脱敏标记及其转述等）。不对自由文本中的 JWT / `sk-` 做猜测遮盖。摘要存入会话目录并绑定 thread_id，生成期间切换会话不会改写另一条会话。`/status` 可查看完整 ID 和摘要。
- 历史会话不自动补全摘要。会话数据库初始化时自动补齐可选摘要列并保留已有记录；没有摘要且无法回退提问截断时显示 thread_id。
- 新会话（启动时或 `/new`）先只在内存中，首次发送消息时才分配会话 ID 并写入。只启动、切换模型或退出不会留下空记录。
- `/new` 继承当前已生效的模型、推理强度、权限模式和沙箱模式；聊天记录、上下文用量和性能统计从空开始。原会话的配置保持不变，尚未生效的配置切换不带入新会话。
- 恢复只加载消息、计划和待处理的审批 / 提问，**不会自动调用模型或重新执行工具**。
- 恢复时，没有保存结果的工具调用显示为 `interrupted (completion unconfirmed)`。
- 如果上一轮异常结束、被取消或出错，恢复后的第一次模型请求会附带一段说明，提醒模型先检查工作区状态；这段说明不写入会话历史。
- 同一会话同一时间只能被一个进程打开。目标会话被其他窗口占用时，`/resume` 和 `deep-agent resume <id>` 会进入等待，`Esc` 取消等待。
- 会话保存其模型、推理强度和权限模式。若保存的模型已不在配置中，恢复时提示并改用 `llm.default`。

## 上下文压缩

- 自动：由 Deep Agents 处理，上下文接近窗口上限时摘要旧消息，见 [architecture.md](architecture.md#上下文压缩)。期间状态栏显示 `Compacting context`。
- 手动：`/compact`。要求最近一次模型 usage 达到自动阈值的一半（配置了 `context_window` 时约为窗口的 42.5%），未达到时显示当前占用。运行中或有待处理的交互时不可用。
