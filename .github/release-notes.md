## 更新内容

- 新增 `ui` 配置项，可调整思考区、命令输出、工具预览、输入框和补全菜单的行数上限；未配置的字段沿用原默认值。
- 任务运行时在状态栏轮换显示 `~/.deep-agent/working_messages.yaml` 中的文案；首次启动复制默认文件，已有文件不会被覆盖。
- 自动压缩上下文期间状态栏显示 `Compacting context`，压缩摘要不再作为回复展示。
- 状态栏移到输入框上方，空闲时隐藏；调整“回到底部”提示与交互面板的位置。
- 重写 README，配置、沙箱、终端界面、库用法、架构和开发说明移入 `docs/`。

## 安装与升级

要求 Python 3.12+；运行环境为 Linux 或 WSL2，默认沙箱还需要 Bubblewrap 及允许 user namespace 的系统策略。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

本次发布提供 wheel 与源码包；GitHub 与 PyPI 使用同一组构建产物。
