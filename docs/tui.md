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

输入 `/` 显示候选，按连续子串匹配（例如 `/elp` 匹配 `/help`），完整匹配和前缀匹配优先。方向键选择，`Tab` 或 `Enter` 填入，退格后重新显示符合当前输入的候选。`/model `、`/permission ` 和 `/sandbox ` 后面的参数也按连续子串补全。

| 命令 | 作用 |
|---|---|
| `/help` | 命令和快捷键列表 |
| `/status` | 运行状态、模型与推理强度、输入能力、工具数及其名称、按模型与强度分组的会话 TPS 统计 |
| `/session` | 当前会话信息与数据库路径 |
| `/new` | 新建会话（首次发送消息后才保存） |
| `/resume` | 列出有内容的会话并选择；`/resume <id前缀>` 直接切换 |
| `/model` | 依次选择来源、模型、推理强度；`/model <来源>` 或 `/model <来源/模型>` 跳过前几步 |
| `/skill` | 打开技能选择器，把技能块插入输入框；不接受参数 |
| `/permission ask\|allow` | 切换权限模式，见 [sandbox.md](sandbox.md#权限模式) |
| `/mcp` | 查看 MCP 全局开关和宿主服务。`/mcp enable` 打开并自动连接；`/mcp disable` 关闭连接和工具，但保留配置与 OAuth 缓存。`/mcp add` 逐步填写；`/mcp remove <name>` 删除；`/mcp test <name>` 重连；`/mcp tools <name>` 查看工具。关闭期间只能查询和 `/mcp enable`。工具审批统一由 `/permission` 控制；服务在宿主运行，不受 Bubblewrap 限制 |
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
| `Esc` | 取消当前任务，并把未发送的 steering 还原到输入框；转录区已上滚时先回到底部 |
| `Alt+Up` | 取回尚未生效的 steering / follow-up |
| `F2` | 重新打开待处理的审批或提问 |
| `Ctrl+O` | 展开 / 收起工具详情 |
| `Ctrl+T` | 展开 / 收起思考内容 |
| `Ctrl+B` | 展开 / 收起排队消息和已发送的长消息 |
| `Ctrl+R` | 查看 `write_file` / `edit_file` / `delete` 的完整改动 |
| `Ctrl+P` / `Alt+P` | 切换到下一个 / 上一个模型 |
| `Ctrl+V` / `Alt+V` | 粘贴图片或文本 |
| `Ctrl+C` | 清空输入；空闲时提示约 1 秒后消失，运行和待审批状态保持；1 秒内再按一次退出 |
| `Ctrl+D` | 输入为空时退出；非空时删除光标后的字符 |
| 滚轮、`PgUp` / `PgDn`、`Ctrl+Home` / `Ctrl+End` | 滚动转录区；`Ctrl+End` 回到底部并恢复跟随 |
| 鼠标左键拖动 | 选择转录内容，松开后复制到系统剪贴板；拖到边缘会自动滚动 |

按键可在 `keybindings.json` 中修改，见 [configuration.md](configuration.md#按键keybindingsjson)。部分终端（如 Windows Terminal）会占用 `Ctrl+V`，此时用 `Alt+V`。

## 运行中的输入

- **Steering**（`Enter`）：在下一次模型调用之前或模型提出的工具调用开始之前交给 Agent。若模型返回时已有 steering 在排队，这批尚未进入审批或执行的工具调用会被丢弃，模型带着新输入重新推理。已经在等待审批的调用仍需处理，已经开始执行的工具会先完成。
- **Follow-up**（`Alt+Enter`）：当前任务结束后作为新一轮输入执行。
- 已有排队消息时，后续输入继续追加到队尾；steering 不会越过更早入队的 follow-up。
- 排队消息显示在输入框上方，例如 `queued 2: 第一条 | 第二条`；多行内容合并预览，超宽截断。按 `Ctrl+B` 在提示下方展开完整队列，展开时用滚轮或 `PgUp/PgDn` 滚动查看，再按一次收起。实际取用后才进入对话区。任务结束交接期间收到的消息也会自动继续处理。
- **取消**（`Esc`）：终止正在运行的命令（向进程组发送 `SIGKILL`），结束本轮。
- 切换或新建会话时，未生效的 steering / follow-up 会退回输入框。

## SubAgent

只有问题能拆分为多个独立、互不干扰的子问题时，主 Agent 才应通过原生 `task` 派生 SubAgent。
默认最多同时运行 2 个任务，主 Agent 同步等待结果；共享文件有修改冲突或存在前后依赖时应顺序处理。

运行期间，输入框上方现有空白行右侧显示 `1 subagent` / `2 subagents`，零时隐藏，不新增状态面板。
不展示子任务名称、进度、模型、Token 或内部日志；普通 task 卡片在实时及会话恢复后均隐藏。
子任务使用沙箱 `allow`，没有审批和主动提问入口，也不装载 MCP 或再次委派。
Esc 取消当前 Run 时会传播到全部关联子任务；退出沿用既有 Ctrl+C、Ctrl+D 和 `/exit` 规则。

主模型切换只影响下一次派生使用的模型 Profile；子任务默认强度使用自身模型默认，
不受主 Agent 当前 high/low 影响。独立日志及按需查询见 [配置参考](configuration.md#subagent-委派)。

## 模型切换

- `/model` 只能在空闲时打开。`Esc` 逐步返回上一级。
- `Ctrl+P` / `Alt+P` 在运行中也可用：切换会排队（提示 `Model queued: …`），在当前工具流程结束、下一次模型调用之前生效。
- 有待处理的审批或暂停时，切换同样排队。
- 有未发送的图片附件时不能切换模型。
- 底栏按 `来源 · 模型 · 推理强度` 显示当前模型。模型列表显示实际 API 模型名，命令参数使用配置中的 `来源/模型键`。

## 审批与提问

- `ask` 模式下，需要审批的工具调用会弹出面板，选择 Run 或 Reject。正在输入草稿或有待发送附件时，只显示待审批提示，不打断输入；按 `F2` 打开审批，处理或关闭后恢复草稿和光标。等待审批时提交的新消息进入队列，原审批仍需明确处理。
- Agent 调用 `request_human_input` 时弹出提问面板，字段类型有单行文本、多行文本、单选、多选、布尔。每道选择题最多 3 个模型提供的选项，系统另行附带 `Other:` 用于自定义输入，不计入这 3 个。超过上限时工具参数校验失败，模型需精简后重新提交。
- 关掉面板后，待处理提示显示在原 Working 状态栏位置，不单独追加到聊天记录；按 `F2` 重新打开。
- 正在输入 `ALLOW` 时，新的审批不会替换确认栏。`F2` 打开审批，`Esc` 回到确认；确认结束或取消后，审批面板再出现。

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
- **思考区**：默认折叠，保留按当前终端宽度折行后的最后 4 行正文（`ui.tail_lines`），标题和隐藏提示另计；`Ctrl+T` 展开。
- **用户长消息**：发送后默认显示前 3 个终端显示行，超出时显示隐藏行数和 `Ctrl+B` 提示。`Ctrl+B` 统一切换长消息与队列的展开状态；只改变显示，发送给模型和会话中保存的内容保持完整。
- **命令输出**：运行中显示 `Running <命令>`，成功后变为 `Ran <命令>`。命令正文默认保留前 4 个显示行（`ui.head_lines`），实时输出保留最后 4 行（`ui.tail_lines`），`Ctrl+O` 展开。失败时显示退出码和简短错误。
- **探索类工具**（`ls`、`read_file`，以及历史会话里的 `glob`、`grep`）连续调用时合并显示：运行中为 `Exploring N items`，全部成功后为 `Explored N items`；等待、失败或中断时显示中性 `Explore` 并附状态提示。当前模型工具表不再提供 `glob` 和 `grep`。
- **文件变更**：运行中显示 `Editing`、`Writing`、`Deleting`，成功后分别显示 `Edited`、`Created` / `Wrote`、`Deleted`。连续成功新建合并为 `Created N files`；覆盖写入和差异预览默认保留前 4 行（`ui.head_lines`）；`Ctrl+R` 查看完整内容。
- 每轮结束显示 `Worked for <耗时> · <结束时间>`，时区由 `ui.timezone` 决定。耗时写入会话数据库，恢复会话时重建历史回合的耗时行；升级前未记录耗时的旧回合不补算。

所有折叠行数可在配置的 `ui` 部分调整，见 [configuration.md](configuration.md#界面ui)。

## 状态栏与底栏

- `/status` 按“模型 + 推理强度”分组，例如 `deepseek-flash-low`；每组下方仅缩进显示 5% low / 5% top / Average TPS 三项。状态栏不显示性能指标，任务完成后照常隐藏。
- 输入框上方的状态栏显示运行状态（如 `Working…  Esc to cancel`）、排队数量和提示；运行时旁边轮换显示 `working_messages.yaml` 中的文案。
- 权限或模型切换排队、输入清空、复制成功等临时提示在该位置显示 1 秒后恢复原状态；工作中、取消中、交互选择和待审批状态持续显示。临时提示自动消失不会删除聊天记录中的相关消息。
- 底栏第一行：工作区路径、当前模型、会话 id；右侧为 Git 分支和改动文件数。当前用户家目录缩写为 `~/`，例如 `/home/neo/project` 显示为 `~/project`；仅改变显示，不改变实际访问路径。
- 底栏第二行：运行状态、执行模式、权限模式；右侧为上下文占用。
- 上下文占用 = 模型最近一次返回的 token usage / `context_window`。端点不返回 usage 时只显示窗口大小；`context_window` 为 0 时不显示。
- Git 状态每 5 秒在后台刷新，单次探测最长 10 秒。可能的显示：`⎇ checking git`、`⎇ no git`、`git unavailable`、`git error`、`git timeout`。


### `/status` 性能统计口径

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
- 新会话（启动时或 `/new`）先只在内存中，首次发送消息时才写入。只启动、切换模型或退出不会留下空记录。
- 恢复只加载消息、计划和待处理的审批 / 提问，**不会自动调用模型或重新执行工具**。
- 恢复时，没有保存结果的工具调用显示为 `interrupted (completion unconfirmed)`。
- 如果上一轮异常结束、被取消或出错，恢复后的第一次模型请求会附带一段说明，提醒模型先检查工作区状态；这段说明不写入会话历史。
- 同一会话同一时间只能被一个进程打开。目标会话被其他窗口占用时，`/resume` 和 `deep-agent resume <id>` 会进入等待，`Esc` 取消等待。
- 会话保存其模型、推理强度和权限模式。若保存的模型已不在配置中，恢复时提示并改用 `llm.default`。

## 上下文压缩

- 自动：由 Deep Agents 处理，上下文接近窗口上限时摘要旧消息，见 [architecture.md](architecture.md#上下文压缩)。期间状态栏显示 `Compacting context`。
- 手动：`/compact`。要求最近一次模型 usage 达到自动阈值的一半（配置了 `context_window` 时约为窗口的 42.5%），未达到时显示当前占用。运行中或有待处理的交互时不可用。
