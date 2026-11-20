## v0.3.0 更新内容

### Lain Companion 侧栏

- 终端达到 **120 列 × 32 行** 时，右侧显示 Lain：时间与运行环境、CPU / GPU / RAM / 磁盘、Git 分支与工作区服务。
- **Tool Health** 按完成顺序展示当前会话最近 60 次有明确结果的工具调用（每 3 次一列）；颜色表示次数，失败显示 `×`。
- **Talk To Lain**：`Ctrl+→` 进入，`Enter` 发送，`Ctrl+←` 返回主输入；回复流式显示并以 Markdown 渲染（与主转录区共用 Rich 路径），聊天区支持滚轮与文本选择。
- 本地统计可触发主动观察；也可在侧栏或 `/lain …` 中查询失败、复盘与项目问题。对话可用 `inspect_current_session`（`recent_tools` / `tool_detail` / `recent_messages`）只读核对当前主 Session。无 Companion 密钥时，监控与动画仍可用。
- 人设、观察提示词、聊天提示词与各状态 GIF 可在 `config.yaml` 的 `companion` 段配置；首次启动写入 `~/.deep-agent/companion/`。详见 [Lain 侧栏配置](https://github.com/ADaozz/deep_agent_cli/blob/v0.3.0/docs/configuration.md) 与 [统计口径](https://github.com/ADaozz/deep_agent_cli/blob/v0.3.0/docs/companion-phase2.md)。

### 动画与提示词

- 五个非待机动画以 `assets/lain-sprites/gifs/` 为事实源，打包进 `agent/companion/assets/`（体积约数十 KB，替代原先数 MB 的旧 GIF）。
- 内置 `persona.md` 精简为核心设定；`observation.md` / `chat.md` 约定观察与对话边界。

### 发布前修复与后续修补

- **工具结果判定**：主 Agent 与子 Agent 共用 `classify_tool_result`。成功读取以 `Error handling…` 开头的文件不再记为失败；正文仅认 `Error:` 信封，`execute` 仍以 artifact / 退出码为准。
- **初始化补齐**：已有 `config.yaml` 时仍会补齐缺失的 Companion 提示词与 GIF，不覆盖用户已编辑文件。
- **Lain 回复**：去掉句数 / 汉字兜底与固定等待超时；模型原文展示（控制字符剔除后约 1600 / 80 字上限）。
- **会话标题**：放宽标题校验；模型失败时回退为提问首行截断 10 字，再不行才显示 thread_id。

### 其他

- 默认模型模板改为 `deepseek/deepseek-flash`（仍可通过配置切换其他 OpenAI 兼容端点）。
- README 增加 Lain Companion 说明与终端截图；架构、TUI、配置与 Companion 文档已同步。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
# 或指定版本：
pipx install deep-agent-cli==0.3.0
```

已有用户配置不会被覆盖。若在旧配置中按文档加入了 `companion/prompts/…` 路径，升级后重启一次即可自动补齐缺失默认文件；若本地已有自定义 `persona.md`，不会被替换。

更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.3.0/docs/tui.md) 和 [配置参考](https://github.com/ADaozz/deep_agent_cli/blob/v0.3.0/docs/configuration.md)。
