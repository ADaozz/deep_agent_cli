## 更新内容

- `/model` 支持模型来源、具体模型、推理强度三段选择。YAML 可配置来源统一推理强度，子模型配置覆盖来源配置；配置枚举后仅显示实际选项。
- 模型列表显示 API 模型名称，统一来源、模型、强度的展示顺序；空闲时立即切换，运行时在安全边界应用。当前选项的 `· current` 使用与菜单标题一致的颜色。
- CLI 启动与新建会话仅创建内存草稿，首次发送消息后才持久化，避免反复启动留下空白会话。
- 关闭事件循环前等待后台 Git 任务结束并清理子进程管道，修复退出时 `Event loop is closed` 异常。
- 改善 Git 状态诊断与慢速工作区探测，区分检查中、超时、不可用和真实非 Git 工作区。
- 增加非交互终端的明确退出、独立 `--help` / `--version`；保留首次启动初始化行为。
- 区分 Bubblewrap 未安装、namespace 权限受限及普通预检失败，提供 AppArmor/userns 排查提示，保持默认沙箱隔离和显式 UNSANDBOXED 授权。
- 增加 Python 3.13、本地 OpenAI 兼容服务集成回归及未放宽 Ubuntu 安全策略的环境诊断；真实模型冒烟单独运行。
- 更新模型厂商配置模板、pipx 安装、Python 3.12+、Ubuntu/WSL2 和会话使用说明。

## 安装与升级

要求 Python 3.12+；运行环境为 Linux 或 WSL2，默认沙箱还需要 Bubblewrap 及允许 user namespace 的系统策略。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
```

本次发布提供 wheel 与源码包；GitHub 与 PyPI 使用同一组构建产物。
