# 重构基线

本文固定重构开始前的行为，供后续改动对照。它描述当前代码实际做的事。计划中的目标行为若与这里不一致，必须在改变行为的那个 PR 里同时改测试和本文。

支持的运行环境是 Linux 与 WSL2 上的 Python 3.12 和 3.13。WSL1 不支持。CI 在 Ubuntu 24.04 上跑 Python 3.12 与 3.13：为 bwrap 单独授权 user namespace 后要求沙箱可用，再跑全部测试并检查 wheel。另一条作业使用发行版默认 AppArmor 策略，只记录沙箱是否可用，并验证不可用时失败关闭。本地开发与 CI 的安装说明见 [开发指南](development.md)。剪贴板图片读取只在 WSL 中通过 `powershell.exe` 实现。

## 测试分层

| 层 | 标记 | 证明什么 | 不证明什么 |
|---|---|---|---|
| 进程内 | 无 | 用脚本模型和 `StateBackend` 固定决策、状态和返回值 | 真实终端时序、Bubblewrap |
| 集成 | `integration` | 本地 HTTP SSE 上的模型流取消、关闭和 checkpoint | 外部模型 API |
| 沙箱 | `sandbox` | 真实 Bubblewrap 的路径、挂载、取消和超时 | 未安装或被策略拦住的环境 |
| 伪终端 | `pty`（同时带 `sandbox`） | 真实 PTY 上的按键、补全和退出 | 见 `tests/test_tui_pty.py` |

`pytest -m "not sandbox"` 可重复跑进程内契约。`pytest -m pty` 复验快捷键。沙箱不可用时 `sandbox` / `pty` 默认跳过；设置 `REQUIRE_BWRAP_TEST=1` 后跳过变为失败。

## 三种不同动作

这三件事不能互相代替：

| 动作 | 当前入口 | 做的事 | 不做的事 |
|---|---|---|---|
| Run Cancel | `Esc` 在运行中，或 `AgentRunner.request_cancel()` | 取消当前 Run 的模型流、可取消工具和图 | 不退出 TUI，不释放 Session 租约，不回滚已发生的副作用 |
| UI Exit | `CliApplication.exit()` → `AppLifecycle.request_exit()` | 忽略重复调用；停掉鼠标、状态计时和选择滚动；通知一次 Session 等待、待连接的 MCP 和运行中的 Run；然后离开全屏 | 不关闭 Executor，不调用 `Runner.close()`，不释放租约 |
| UI Shutdown | `AppLifecycle.shutdown()`，在 TUI 循环的 `finally` 中 | 用同一个期限取消并等待界面辅助任务，并等待已经通知过的 Run、MCP 和 Session 操作自己结束；超时记下任务名和阶段；已经失败的任务进入一次失败摘要；最后才关闭 Executor | 不取消 OBSERVE 任务，不把“Executor 已关闭”或“Task 已超时”当成底层线程已经停止，不释放租约。循环结束后仍未完成的 Task 会被事件循环取消，不能假定它们还活着 |
| 进程 Shutdown | `main()` 在 `app.run()` 返回后调用 `Runner.close()` | 禁止新操作，排空或取消仍在进行的 Run，确认模型流关闭后释放租约并关闭 MCP 与沙箱 | Run 未排空或模型流未确认关闭时保留租约并抛出错误 |

`Ctrl+D` 在输入为空时，以及 `/exit`，都进入 `CliApplication.exit()`。`Ctrl+D` 在输入非空时删除光标后面的字符；光标已经在末尾时文本不变，也不会退出。审批界面开着时同样如此。

## 输入决策的当前优先级

按键映射在 `agent/cli/input.py` 的 `Keymap`。`Esc`、`Ctrl+C`、`Ctrl+D` 和 `/exit` 的优先级只在 `InputActionRouter` 决定；`CliApplication.apply_input_action()` 执行界面操作。双击退出窗口只有路由器的 `last_ctrl_c` 一处，长度是 `EXIT_WINDOW_SECONDS`（1 秒），`CliApplication._last_ctrl_c` 只是它的只读视图。打开中的交互被 `Ctrl+C` 关掉时不武装这个窗口。退出请求已经发出后，不再创建剪贴板、斜杠命令这类界面辅助任务，也不再调度新的 Run、Session 等待或 MCP 操作；重复的退出请求不会再走一遍资源通知。此时 `Esc` 仍按原来的优先级处理，运行标记还在时可以再请求一次 Run Cancel。

`Ctrl+C`（`clear_or_exit`）：

1. 有交互界面：取消该界面，不批准、不拒绝、不退出。
2. 距上次清空不足 1 秒：调用 `exit()`。
3. 否则清空输入、粘贴块、技能块和待发送附件，并记录时间。空闲时状态栏显示清空提示；运行中、压缩中、`Waiting for input` 和 `still pending · F2 to decide` 保持原状态。

`Esc`（`interrupt`）按下面的顺序只走一条：

1. 没有交互且补全菜单开着：关掉菜单，保留当前文档。
2. 没有交互且整行是斜杠命令：清空这行。
3. 转录区不在底部：滚回底部。
4. 补全仍开着：取消补全。
5. 正在等待被占用的 Session：取消等待，不退出进程。
6. 正在看 diff：只关闭 diff。审批界面还开着时，下一次 `Esc` 才关闭审批。
7. 交互的自定义输入：退回选项。
8. 其他交互：按该界面返回或取消，审批和提问只关掉面板。
9. 正在压缩：提示等待，不取消 Run。
10. 运行中：把未发送队列放回输入框，并请求一次 Run Cancel。同一次 Run 里再次 `Esc` 只重复提示。
11. 仅 MCP 后台操作：提示仍在后台，不请求 Run Cancel。运行中的 MCP 操作先落在第 10 条。
12. 其余情况：不动作。

退出已经开始后，再次 `Ctrl+C`、`Ctrl+D` 或 `/exit` 会再次进入 `exit()`，由 `_exiting` 直接返回。`exit()` 本身不设置 `_cancel_sent`。因此运行标记还在时，退出过程中的 `Esc` 仍会再调用一次 `request_cancel()`。这是当前行为，不是疏忽的测试漏洞。

## 退出矩阵

| 场景 | Ctrl+C | Esc | 契约测试 |
|---|---|---|---|
| 普通编辑 | 首次清空，1 秒内再次退出；满 1 秒则重新清空 | 关闭补全或斜杠行；上滚时先回底部 | `test_ctrl_c_exit_window_is_one_clock`，`test_ctrl_c_at_window_boundary_clears_again`，`test_escape_prefers_slash_line_over_scroll`，`tests/test_cli.py` 的补全与窗口边界，`tests/test_tui_pty.py` 的 PTY 窗口 |
| Agent 运行中 | 首次清空且保持运行状态，第二次退出并取消 | 请求取消当前 Run，重复按键不重复发送 | `test_running_ctrl_c_then_exit_cancels_once_per_exit`，`test_repeated_escape_sends_one_cancel_per_run` |
| 审批或选择 | 关闭界面且不武装退出窗口；不自动批准 | 先关 diff，再关界面；选择器按层返回 | `test_ctrl_c_dismisses_interaction_without_arming_exit`，`test_escape_closes_review_before_approval` |
| Session 等待 | 与普通编辑相同，第一次不取消等待 | 取消等待 | `test_session_wait_ctrl_c_uses_the_shared_exit_window`，`test_cli_esc_cancels_wait_and_returns_to_picker` |
| MCP 后台 | 与普通编辑相同 | 只提示，除非 Run 也在运行 | `test_escape_during_mcp_background_does_not_cancel_a_run` |
| 正在退出 | 重复退出被忽略 | 运行标记仍在时会再请求一次取消 | `test_repeated_exit_is_ignored`，`test_escape_after_exit_requests_cancel_again_while_running` |
| 空输入 Ctrl+D、`/exit` | — | — | `test_ctrl_d_edits_nonempty_input_and_exits_when_empty`，`test_slash_exit_uses_exit_entry` |

## 退出后的准入

`AppLifecycle` 的阶段是 `ACTIVE → EXIT_REQUESTED → SHUTTING_DOWN → STOPPED`。离开 `ACTIVE` 之后：

- 新的界面辅助任务（`CANCEL`）被拒绝，协程会关掉，不进入关闭快照。
- 新的 Run、Session 等待和 MCP 操作（`OBSERVE`）被拒绝。`spawn()` 关掉协程并抛出错误，不用一个已完成的空任务假装操作已经成功。
- `_start_queued_run()` 在 `pop_steering()` / `pop_follow_up()` 之前返回，所以退出不会消费或标成已执行尚未运行的队列。`_start_run()` 在 `state.running`、`reserve_run()` 和任何可见状态变化之前返回。
- 已经开始的 Run 仍由 `RunController` 取消并收尾。未退出时，完成后仍先取 steering，再取 follow-up。
- Session 等待被 `Esc` 取消时回到选择器。被 CLI Exit 取消时不打开选择器，也不在底层切换晚到之后改写界面。已经拿到的租约仍由 Runner / SessionRuntime 持有。
- 新的 MCP 操作在 `MCPController.try_begin()` 之前被拒绝。已经开始的操作仍按原取消路径收尾；它的 `finally` 只清自己的 `operation_id`。退出后的完成回调不发布工具表，也不写转录。
- 任务在关闭前就以异常结束时，失败摘要按仍被引用的 Task 对象记一次，不用会被回收复用的 `id()`。任务对象回收后不再占着这条记录。取消成功的界面辅助任务不是失败。再次 `shutdown()` 在 `STOPPED` 直接返回。
- 用户取消「等待 MCP 操作结束」时仍提示操作还在后台。CLI 已经退出时不再写这条提示。

`asyncio.to_thread()` 用的是事件循环默认 Executor，和 CLI 的 `_io_executor` 不是同一个。Shutdown 超时只说明等待结束，不说明那条阻塞线程已经退出。`Runner.close()` 仍是租约、模型流、MCP 和沙箱的最终所有者。

## 模型流、工具取消和 Session 租约

这些行为已有测试，重构时继续以它们为准：

| 契约 | 测试 |
|---|---|
| 取消关闭模型流；重复取消幂等；完成后到达的取消不改写完成结果 | `tests/test_model_stream_cancel.py` |
| 模型流未确认关闭时，`Runner.close()` 保留租约 | `test_runner_close_keeps_lease_while_model_stream_unclosed` |
| 工具取消回调只调用一次；`execute` 取消终止该命令进程组 | `tests/test_streaming_cancel.py`，`test_runner_cancels_running_execute_tool` |
| 取消保留持久沙箱，不自动重放命令 | `test_cancel_preserves_sandbox` |
| Run 未排空时 `close()` 超时且不提前释放租约 | `test_close_timeout_never_releases_the_lease_early` |
| 同一次超时同时保留 MCP bridge 和租约 | `test_runner_close_timeout_keeps_mcp_bridge_and_session_lease` |
| TUI 退出本身不释放租约；`Runner.close()` 成功后才释放 | `test_ui_exit_keeps_the_session_lease` |
| 一个 Session 的取消不影响另一个 Session 的模型流 | `test_cancelling_one_session_leaves_concurrent_streams_intact` |

会话恢复只加载已有消息和待处理交互，不自动调用模型。快照展示只在 `SessionController.apply_snapshot`。`CliApplication._apply_session_snapshot` 只转发给它。恢复、等待和租约见 `tests/test_session_switch.py`、`tests/test_session_lock.py` 和 `tests/test_cli.py` 中的 resume 用例。模型、权限、沙箱和 MCP 工具表的空闲应用走 `AgentRunner.apply_pending_config_if_idle()`，CLI 不再自己拿运行锁。MCP 操作是否进行中、操作代号、开关锁，以及工具表发布的 revision 和结果都在 `MCPController`。添加服务的草稿仍留在界面上。同一 revision 失败或已经发布后不会自动再发；更新的 revision 可以再发。

中断分类、工具事件、usage 和终态 `RunResult` 在 `agent/run_events.py`。`RunEvent` 与 `RunResult` 仍从 `agent.runner` 导入。图循环、取消和租约仍在 `AgentRunner`。同一 tool call id 只发一次 `tool_started`。`ToolMessage.artifact` 原样进入 `tool_completed`。只有带 `input_tokens` 的 usage 才算一次输入用量。当前 Run 没有请求取消时，`ModelStreamCancelled` 是失败，不是取消。最终回答已经进入 checkpoint 后，迟到的取消不改写完成结果。

沙箱没有再拆。`agent/sandbox.py` 拥有路径、挂载参数、文件工具路由和单次命令的收集与取消。`agent/sandbox_pool.py` 拥有有网/无网实例的复用和 Runner 关闭时的清理。`agent/sandbox_worker.py` 拥有沙箱内按 request id 执行的一条命令；`kill` 只作用于该命令。这三处的回归仍是 `tests/test_sandbox.py` 和 `tests/test_persistent_sandbox.py`。权限模型、默认挂载模板和工作区路径没有在这一轮改动。
