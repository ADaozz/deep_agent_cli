## 更新内容

本版本修复恢复会话提示遗漏思考强度的问题：模型信息现在显示为 `Model: deepseek · deepseek-flash · low`，使用当前会话已保存的思考强度。未显式设置时显示 `default`。此前会话中的思考强度没有丢失，此次补充的是显示字段。

## v0.1.4 功能回顾

- 新增可选 HTTP Agent 服务 `deep-agent serve`，复用 CLI 的 AgentRunner、工具和沙箱，提供会话、任务、审批、人工输入、持久化 SSE 和恢复接口。服务使用独立状态目录；CLI 和服务同时修改同一项目文件时仍需协调。
- 修复任务终态与 SSE 结束事件的原子发布、交互提交事件顺序、必填人工输入校验，以及服务按项目工作区校验配置的行为。
- 统一模型配置，按来源和模型选择 Chat Completions 或 Responses API；改进思考内容回传、输入 token 用量识别和图片附件处理。旧版模型配置需按新模板迁移，详见配置文档。
- 右下角 Context 保留上一轮有效读数，直到返回新的 `input_tokens`；开始生成、取消、失败、压缩及空用量不再清空显示。切换模型或会话时重置，首次尚未收到用量时显示 `Unknown`。
- 模型及思考等级切换提示仅显示在输入框上方，不再插入对话窗口。
- 默认模板允许传入 `DISPLAY`，可选挂载 WSLg X11 文件 socket；复用宿主已有显示服务，无需安装 Xvfb。已验证无网沙箱的有头浏览器启动、页面截图及关闭；访问互联网仍需网络权限。已有配置需手动合并并重启 CLI。
- 改进沙箱长输出、取消及终端审批行为，补充相关回归与使用文档。Unix socket 间接联网仍是已知隔离边界，长输出持久化放大列为后续任务。

## 安装与升级

要求 Python 3.12+；运行环境为 Linux 或 WSL2，默认沙箱还需要 Bubblewrap 及允许 user namespace 的系统策略。

```bash
pipx install deep-agent-cli
# 已安装：
pipx upgrade deep-agent-cli
# 需要 HTTP 服务：
pipx install --force 'deep-agent-cli[server]'
```

本次发布提供 wheel 与源码包；GitHub 与 PyPI 使用同一组构建产物。

配置与服务接入说明：[配置文档](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/configuration.md)、[HTTP 服务](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/server.md)、[有头浏览器与沙箱](https://github.com/ADaozz/deep_agent_cli/blob/main/docs/sandbox.md#playwright-与有头-x11)。
