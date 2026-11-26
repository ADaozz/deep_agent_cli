# MCP 集成

Deep Agent CLI 支持通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 接入外部工具服务，支持 **stdio** 本地子进程与 **Streamable HTTP** 远程端点两种传输方式。

---

## 配置文件与全局开关

MCP 服务配置保存在 `~/.deep-agent/mcp.json`（与 `config.yaml` 同目录，支持通过 `DEEP_AGENT_CONFIG` 自动跟随配置目录）。

### 启用与关闭

在 `config.yaml` 中可通过全局开关控制是否加载 MCP（默认关闭或按配置启用）：

```yaml
mcp:
  enabled: true
```

当 `mcp.enabled: false` 时，不连接任何 MCP 服务，也不向模型暴露外部 MCP 工具。

### `mcp.json` 示例

```json
{
  "mcpServers": {
    "filesystem-readonly": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/docs"],
      "env": {
        "NODE_ENV": "production"
      }
    },
    "remote-docs": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_DOCS_TOKEN}"
      }
    }
  }
}
```

- **服务名规则**：必须匹配 `^[A-Za-z][A-Za-z0-9_-]{0,32}$`。
- **认证支持**：Streamable HTTP 支持静态 Header / Bearer Token（可引用 `host` 作用域的加密持久化凭据，见 [安全凭据管理](secrets.md#host-作用域mcp-与宿主服务)），也支持 OAuth 2.0 授权码流程（令牌保存在本地受保护存储中）。
- **Node 依赖建议**：使用 Node.js 编写的 stdio MCP 服务时，推荐使用 **Node.js 24** 最新补丁版本。

---

## 终端管理命令（`/mcp`）

在 TUI 中可通过 `/mcp` 管理当前会话的 MCP 服务状态：

- `/mcp` — 查看已配置的 MCP 服务、连接状态、认证状态与已发现工具数量。
- 支持交互式重连、刷新工具清单、触发 OAuth 登录或查看连接诊断信息。
- 内建管理类工具：`mcp_list`（免审批）、`mcp_add` 与 `mcp_remove`（在 `ask` 模式下需要审批）。

---

## 宿主执行与权限边界

1. **宿主进程内运行**：MCP 客户端连接与 stdio 子进程均运行在**宿主 `deep-agent` 进程**中，**不经过 Bubblewrap 沙箱隔离**。请仅接入你明确信任的 MCP 服务。
2. **权限模式约束**：
   - 在 `ask` 模式下，**所有外部 MCP 工具**（无论服务端是否声明了只读 hint）以及 `mcp_add` / `mcp_remove` 在调用前均须人工审批。
   - 在 `auto` 模式下按审批策略与 Flash 审查处理。
   - 在 `allow` 模式下跳过审批（与其他工具一致）。
3. **凭据消费绑定**：MCP 引用的 `host` 凭据必须是加密持久化（`persistent`）凭据，且绑定该服务的配置指纹 `fingerprint()`；修改服务 URL 或命令后需重新授权。

---

## 统一身份展示

为防止不同 MCP 服务间工具名冲突，内部统一以 `mcp__<server>__<remote>`（过长或含特殊字符时附带 SHA-256 短哈希）作为唯一注册名。

在 TUI 工具卡片、审批面板与 Lain Tool Health 中，外部 MCP 工具共用同一套身份解析与回退规则：

1. 优先使用真实握手获得的 `serverInfo.name` 与原始远程工具名；
2. 缺少服务名称时，仅显示原始远程工具名（例如 `MCP · search_docs`）；
3. 缺少原始远程工具名时，保留完整注册名；
4. **绝不**从 `mcp__` 注册名反拆或猜测真实服务名；
5. Tool Health 统计与历史记录始终以完整注册名为键，不因展示名称相同而错误合并不同工具。

---

## 后台日志与当前限制

- **后台日志**：MCP 连接与 stdio stderr 日志写入本地日志文件，避免干扰主终端 TUI 渲染。
- **已知边界**：
  - 不对 MCP 工具本身提供 Bubblewrap 进程隔离；
  - 仅在已配置并启用的服务上按需连接，不会为获取展示名称额外发起额外网络请求。
