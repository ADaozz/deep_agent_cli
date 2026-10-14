# 远程 MCP Tools

MCP 是普通 LangChain 工具的一个来源。DeepAgentCLI 不提供 MCP Server，也不会运行 `npx`、stdio 服务或安装 Server 推荐的依赖。首次使用无需额外插件。

## 配置与命令

`/mcp` 打开选择菜单，高亮当前 Profile，`Enter` 选择、`Esc` 取消。`none` 是内置禁用状态；`+ Add MCP` 或 `/mcp add` 打开名称、传输方式和 URL 输入，再确认 `Save and activate`、`Save only` 或取消。确认页可选择 `Edit` 修正输入；连接失败会保留输入和原工具集。

`/mcp github` 选择已有配置；重复选择当前配置不重新发现工具。`/mcp none` 禁用，但不删除 Profile 或会话历史。`/status` 显示当前服务、脱敏 URL 和工具数。

文件位于 `DEEP_AGENT_CONFIG_DIR/mcp.yaml`（若设置），否则在 `paths.config_dir` 或 `~/.deep-agent` 下，与模型配置分开：

```yaml
current: none
profiles:
  github:
    url: https://example.com/mcp
    transport: streamable_http
  legacy:
    url: https://example.com/sse
    transport: sse
```

缺少文件等价于 `none`。名称以字母开头，只能包含字母、数字、`_`、`-`，最长 64 字符；`none` 和命令关键字 `add` 保留。一个 Profile 对应一个 Server，同时只启用一个。保存采用权限 0600 的临时文件和原子替换，不创建 YAML 备份。

仅接受 HTTP(S)，默认 `streamable_http`；`sse` 显式使用 Legacy SSE Transport，不根据 URL 自动推断传输。首版不支持认证配置/OAuth；URL 中禁止用户密码及 Token 类查询参数。状态及审批中的 URL 不展示查询串、用户信息或 fragment。

启动时恢复选中配置并发现工具。服务不可用时 CLI 保留内置工具、显示启动错误，并保留 Profile 供 `/mcp` 重试，不覆盖已保存的当前配置。

## 调用、审批与取消

工具通过 `AgentSpec.tools` 与普通工具一起注册到现有 Harness。与文件工具、`execute`、`web_search`、Todo、压缩或自定义工具重名会拒绝启用。发现、Schema、Graph 构建或配置保存失败均保留原环境。

`ask` 对全部 MCP 工具审批，不信任服务声明的只读标记。现有审批卡显示 Tool、Profile、Server 及参数摘要；疑似凭据会脱敏。拒绝审批不会调用远端。`allow` 使用同一自动批准路径，**Bubblewrap 不隔离远端 Server，它可能拥有更大的权限**。

Esc 和 HTTP Cancel 使用现有 RunController。单次取消仅作用于该调用的 Future，等待本地 Task 收尾才结束；远端是否停止或撤销副作用由 Server 决定。若本地调用无法收尾，Runner 关闭超时会保留会话租约，避免新执行路径同时写 Checkpoint。

工具保留官方 Client 引用，发现连接结束后按官方可重入 Client 生命周期执行请求，不维护额外心跳或长期 HTTP 会话。模型/权限切换、新建/恢复会话均保留已发现工具，不重新连接。Client 和事件循环不进入 SQLite Checkpoint。

官方 Adapter 负责结果转换，同步桥完整委托 `ainvoke()`，保留它提供的 content、artifact、status 和 Tool Call ID。文本、图片、文件以及结构化内容已验证；官方 Adapter 不支持的内容类型（例如当前版本的音频结果）仍会报错，不额外解析 MCP 结果补齐。首版不实现 Resources、Prompts、Sampling 或 Elicitation；服务发起 Elicitation 时返回 decline，不进入另一套中断交互。

## HTTP 服务

服务默认禁用 MCP。需要启用时，在服务 YAML 中增加：

```yaml
mcp_config: /home/user/.deep-agent/mcp.yaml
```

该路径必须在项目工作区外。启动时读取配置快照；TUI 随后修改文件不会改变服务中已确定的 Profile，重新启动服务才读取新配置。每个服务 Runner 在初始化时发现工具，模型/权限重建继续复用。HTTP 服务未增加 MCP 管理接口，工具仍使用原审批和 Cancel 接口；审批数据附带 `mcp.profile` 和脱敏 `mcp.url`。
