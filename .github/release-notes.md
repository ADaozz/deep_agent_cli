## 更新内容

- 每个会话按需保留无网和有网两个沙箱：`/tmp`、`/home` 和后台进程在后续命令中仍然存在，切换会话或退出时销毁。取消和超时只结束当前命令。
- 新增 `/skill`，从 `~/.deep-agent/skills/` 选择技能；技能目录以只读方式挂到 `/skills/`。
- 默认配置增加可选工具链挂载（Node.js、Cargo、系统 Chrome、Playwright 浏览器缓存）。`optional: true` 的源目录不存在时会跳过，不会阻止启动。已有的 `~/.deep-agent/config.yaml` 不会被覆盖。
- 命令补全改为连续子串匹配。空闲时 `Ctrl+C` 的提示约 1 秒后消失；关掉审批或提问面板后，按 `F2` 重新打开。

## 安装与升级

要求 Python 3.12+；运行环境为 Linux 或 WSL2，默认沙箱还需要 Bubblewrap 及允许 user namespace 的系统策略。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

本次发布提供 wheel 与源码包；GitHub 与 PyPI 使用同一组构建产物。
