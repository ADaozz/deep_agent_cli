## v0.1.7 更新内容

### 正则搜索与沙箱复用

- `grep` 默认使用正则表达式，支持 `fixed_string=true` 字面量匹配，以及 `files_with_matches`、`content`、`count` 三种输出模式。
- 随 Python 依赖安装 ripgrep，优先使用当前 Python 环境中的 `rg`；支持否定 Glob、组合过滤和有界输出。
- 沙箱内搜索复用现有持久 Worker，以 argv 直接运行 rg；取消、超时或截断只清理当前搜索进程。无 rg 或非沙箱模式使用可终止的 Python 回退。
- 使用 NUL 分隔文件名结果，正确处理包含换行符的路径；补充性能、安全与过滤规则回归测试。

### 消息排队与终端交互

- 修复追加指令未继续调用模型、任务结束时队列滞留的问题，按入队顺序处理消息。
- 状态栏显示排队内容，支持 `Ctrl+B` 展开长消息与队列，并滚动查看排队消息。
- 修复 edit 差异背景在 256 色终端下变灰的问题。
- 每道选择题最多提供 3 个模型选项，系统单独添加 `Other:` 自定义输入，并提供参数纠错提示。

### 接口与文档整理

- 清理旧接口别名、历史兼容逻辑与冗余 TodoListMiddleware；Python API 使用方升级前请检查调用方式。
- 精简 README，将搜索、Skills、终端操作和沙箱说明集中到对应文档，校正显示行数、快捷键与开发环境说明。
- 补充 Harness 架构、HTTP 服务契约及沙箱网络边界说明。

### 升级注意

- `grep` 已从旧版字面量匹配改为默认正则匹配；搜索含 `.`, `*`, `+`, `[` 等字符的普通文本时，请设置 `fixed_string=true`。
- Python 回退与 ripgrep 的正则语法和 ignore 行为存在差异，详见 [搜索文档](https://github.com/ADaozz/deep_agent_cli/blob/v0.1.7/docs/search.md)。

### 安装与升级

要求 Python 3.12+，Linux 或 WSL2。默认沙箱需要 Bubblewrap 和可用的 user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
# 需要 HTTP 服务：
pipx install --force 'deep-agent-cli[server]'
```

GitHub Release 与 PyPI 使用同一次 CI 构建的 wheel 和源码包。更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[HTTP 服务](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/server.md)及[开发与发布](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/development.md)。
