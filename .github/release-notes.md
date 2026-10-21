## v0.1.8 更新内容

### 终端状态与显示

- 统一工具状态文案：命令运行时显示 `Running`，成功后显示 `Ran`；文件操作运行时显示 `Editing`、`Writing`、`Deleting`，成功后显示 `Edited`、`Created` / `Wrote`、`Deleted`。
- 探索分组运行时显示 `Exploring`，仅全部成功后显示 `Explored`；等待、失败和中断时保留明确状态，避免提前暗示完成。
- 新会话与 `/new` 后的上下文占用显示 `0.0% used`；已有对话缺少有效用量时仍显示 `Unknown`，清屏不会误判为新会话。
- 顶部标题改为 `DeepAgentCLI v<版本号>`，与 `--version` 共用包元数据读取逻辑；移除第二行快捷键提示，完整操作仍可通过 `/help` 查看。
- 底栏将当前用户家目录缩写为 `~/`，不改变实际工作区路径或文件访问行为。

### README 与设计文档

- 调整 README 为简洁的开发者工具首页，接入真实终端截图，精简首次使用步骤，折叠模型示例、架构图与进阶说明。
- 保留文件工具与沙箱命令的不同访问边界，以及默认审批、网络和已知限制。
- 在架构文档中明确“运行中用进行式，成功后用过去式”的统一状态文案规范，同步终端与配置文档。
- 补充运行状态切换、新会话占用、会话恢复、标题版本和家目录缩写的回归验证。

### 运行入口

- 使用入口是交互式终端 `deep-agent`、`deep-agent resume`，以及 Python API。

### 安装与升级

要求 Python 3.12+、Linux 或 WSL2，以及可用的 Bubblewrap / user namespace。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

GitHub Release 与 PyPI 由同一个发布工作流构建并发布。更多说明见 [README](https://github.com/ADaozz/deep_agent_cli#readme)、[终端界面](https://github.com/ADaozz/deep_agent_cli/blob/v0.1.8/docs/tui.md)和[架构设计](https://github.com/ADaozz/deep_agent_cli/blob/v0.1.8/docs/architecture.md)。
