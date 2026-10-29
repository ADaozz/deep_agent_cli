## v0.2.1 更新内容

### 原生 SubAgent（PR #30）

- SubAgent 运行数量移至 `Working…` 所在状态行，输入框顶部恢复为空白，数量为零时隐藏。
- 每次模型请求在系统提示词中动态注入当前日期（`yyyy-mm-dd`）和用户时区，主 Agent 与 SubAgent 均使用 `ui.timezone`，跨天后自动更新。

- 接入 Deep Agents 原生同步 `task` 和隔离 `CompiledSubAgent`。仅当问题可拆成多个独立、互不干扰的子问题时才应派生；默认最多同时运行 2 个子任务。
- 子任务共享已验证的沙箱 Backend/Pool，使用 `allow`，不装载 MCP、人工输入或递归委派工具；父任务取消传播到子任务，任务结束不保留后台 SubAgent。
- 模型 Profile 可继承主模型，思考强度 `default/null` 使用子模型原生默认；支持显式能力校验和独立客户端回收。
- 每个 task 独立显示 SubAgent 卡片，展示任务描述、状态及可展开的完整返回结果，支持失败、取消和历史恢复；内部流仍隔离；独立 JSONL 执行摘要只记录任务起止、工具操作/状态/耗时及有界最终回答，不保存模型 Token、思考或原始工具输出；仅精确替换已知模型和 Tavily Key，支持有界、按父会话隔离的查询。
- 现有用户配置不会自动覆盖；配置项和派生条件见 [配置参考](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.1/docs/configuration.md#subagent-委派)。

### TUI 输出与差异预览

- 修复长 CSS、中文等工具输出自动换行后超过折叠行数的问题。命令输出和普通工具的原始输出按终端实际显示行数保留末尾 4 行，标题与隐藏提示另计，`Ctrl+O` 仍可展开完整输出。
- 连续编辑同一文件的合并 diff 以 `Edit N` 标明各次编辑，并注明行号相对于各自的替换片段；完整审阅也注明相对行号，避免将每次重新计数误读为文件行号倒序。
- 补充自动换行、中文宽字符、流式输出及多次编辑的渲染回归测试，更新折叠规则文档。

### 会话与默认配置

- 新建会话继承当前模型、推理强度、权限和沙箱配置。
- 示例默认模型改为 DeepSeek Flash，同步首次启动测试的配置断言。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

已有用户配置不会自动覆盖。

更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.1/docs/tui.md)和[配置参考](https://github.com/ADaozz/deep_agent_cli/blob/v0.2.1/docs/configuration.md)。
