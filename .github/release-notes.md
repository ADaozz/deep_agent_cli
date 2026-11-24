## v0.4.0 更新内容

### 权限与自动审批

- 保留默认的 **Ask** 手动审批和沙箱专用的 **Allow**，新增 **Auto**。Auto 直接放行工作区内的文件变更、网页工具和 restricted 沙箱内的离线命令。DeepSeek Flash 审查联网命令、full-access 命令及工作区外的文件变更。
- Flash 根据真实用户请求、工具调用和执行环境判断授权。请求异常或超时时转人工确认。会话记录审批决定及执行状态，环境改变或执行结果未知时需要重新发起。

### SubAgent 与 TUI

- 新的 `start_task` 统一同步与后台委派。根 Agent 可启动后台任务，同步子任务可继续同步委派。默认每个父节点最多并行 4 个直接子任务，全树同时最多 10 个子任务，支持两层嵌套。
- 子任务继承启动时的权限。Ask 下可查看和取消已有任务。通过 `read_task` 查看直接子任务及有界日志，通过 `cancel_task` 请求停止根 Agent 的后台任务。
- TUI 在会话中展示嵌套任务树和任务终态。代码块、文件读取与 Diff 共用 VS Code Dark+ 语法高亮，多文件 Diff 可在原卡片展开。

### 架构调整与移除

- 主 Agent 与子 Agent 共用图执行和结果判定。系统提示词、委派工具说明和 Flash 审查规则分别维护。
- Lain 保留本地监控与原始事件采集。主动模型观察和本地诊断结论已移除，只有用户提问才调用模型。
- 移除旧的后台任务工具及 `read_task_log`，删除未使用的原生 `grep` worker 和基准脚本。代码搜索继续通过沙箱内的 `rg` 执行。网页搜索与抓取仍使用 DeepSeek 服务。

### 配置迁移

- DeepSeek 主模型、子模型、网页工具、Lain 与 Auto 审批统一读取 `llm.models.deepseek.api_key`。请把旧 `web_tools.api_key` 或 DeepSeek 单模型密钥移到来源级并删除旧字段。旧 `COMPANION_DEEPSEEK_API_KEY`、`companion.key` 与 Lain 观察提示词配置停止读取。新字段可引用环境变量，例如 `${DEEPSEEK_API_KEY}`。
- 缺少来源级凭据时，网页工具不加载，Auto 回退 Ask，Lain 仍可显示本地监控。已知模型密钥和 `redaction.replacements` 中的字面量按名称替换。

要求 Python 3.12+，运行环境为 Linux 或 WSL2，并具备可用的 Bubblewrap。已安装的用户可运行 `pipx upgrade deep-agent-cli`。

[配置参考](https://github.com/ADaozz/deep_agent_cli/blob/v0.4.0/docs/configuration.md) · [沙箱与权限](https://github.com/ADaozz/deep_agent_cli/blob/v0.4.0/docs/sandbox.md) · [终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.4.0/docs/tui.md)
