# 本地 Web Console

当前 `web_beta` 已用 `deep-agent-web-20261018.tar.gz` 的 Vue 3 浅色界面替换旧 Stitch 工作区。首页负责选择项目和启动任务；线程负责回答、工作日志、内联审批、人工输入与轮次代码回退；右侧变更栏展示成功文件工具调用的实际内容。旧界面的验收报告是历史记录，当前验证见 [新前端适配验收](frontend-console-migration.md)。

## 安装与开发

需要 Python 3.12+、Node.js 20.19+ 或 22.12+，以及可用的 Bubblewrap。先从仓库根目录安装：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,server]"
```

终端 1，按 [HTTP 服务文档](server.md) 准备服务 YAML 并启动：

```bash
export DEEP_AGENT_SERVICE_TOKEN='<本机服务令牌>'
python -m agent.server.main --config /path/to/server.yaml
```

终端 2，使用相同令牌启动网关：

```bash
source .venv/bin/activate
export DEEP_AGENT_SERVICE_TOKEN='<与终端1相同的服务令牌>'
export DEEP_AGENT_SERVICE_BASE_URL='http://127.0.0.1:8080'
python -m agent.web_gateway.main --host 127.0.0.1 --port 8765
```

终端 3：

```bash
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`。浏览器固定访问同源 `/api/agent`，Vite 转发到网关。网关只向本地服务注入令牌并透传 SSE；页面无需填写地址或令牌。底部“连接设置”可测试连接及修改本机显示偏好。

## 构建后运行

```bash
cd web
npm ci
npm run build
cd ..
export WEB_DIST_DIR="$PWD/web/dist"
# 在配置好服务令牌与上游地址的终端执行：
python -m agent.web_gateway.main --host 127.0.0.1 --port 8765
```

仍需独立运行 Agent HTTP 服务。访问 `http://127.0.0.1:8765` 即可。Python wheel 包含 `agent.web_gateway`，不包含 `web/dist`；从 wheel 安装时需另行准备构建目录并设置 `WEB_DIST_DIR`。`npm run preview` 只预览静态资源，完整功能请通过上述网关运行。

## 功能与恢复

- 服务配置的项目选择、创建会话、文本任务、五类人工输入与 Other、工具批量审批、暂停继续、取消和中断恢复。
- 从服务发现活动任务和运行历史；缺少本机记录时仍能回放日志与最终回答，但原始提问会显示不可用。每次加载 20 轮，可继续加载更早记录。
- 浏览器为每轮保存已应用的 SSE 游标，断线先校正状态再重连，轮询兜底。服务重启后旧 Run 为 `interrupted`，点击“恢复”会创建新 Run，并可能重新出现尚未处理的审批/提问。
- 取消是 `cancelling` 到终态的协作过程，不撤销文件改动；关闭页面也不取消任务。
- 每个结束轮次的“本轮代码”支持快照列表、回退和撤销回退；保留对话，遇到后续文件变更则报告冲突。范围和限制见 [轮次代码回退](code-restore.md)。
- 变更侧栏按成功的文件写入/编辑/删除工具事件展示路径及内容片段，不包含完整 Git Diff 或命令引起的全部改动；命令改动可通过轮次快照检查。
- 模型、权限、Skills、MCP 和项目工作副本由服务配置；模型显示名只改变本机标签。HTTP 不支持上传、语音、桌面、终端交互、PR 或提交操作，对应入口说明能力边界。

## 本机配置

| 配置 | 说明 |
| --- | --- |
| `DEEP_AGENT_SERVICE_TOKEN` | 服务及网关使用，浏览器不接收 |
| `DEEP_AGENT_SERVICE_BASE_URL` | 网关上游，默认 `http://127.0.0.1:8080`，限 loopback 原点 |
| `WEB_DIST_DIR` | 可选，前端构建目录；不设置时网关仅提供 API |
| `DEEP_AGENT_WEB_ORIGINS` | 可选的本机原点列表；默认包含本地网关和 Vite 5173 |
| `--host / --port` | 网关默认 `127.0.0.1:8765`，只允许 loopback |

修改 Vite 端口时需同步网关允许的原点。网关保留 Host/Origin 检查、同源 JSON POST、1 MiB 请求体上限和接口白名单。503 请检查服务/网关进程，401 检查两端令牌是否一致，403 检查 Host/Origin。

## 自动验证

```bash
TERM=xterm-256color REQUIRE_BWRAP_TEST=1 .venv/bin/python -m pytest tests/test_server.py tests/test_web_gateway.py tests/test_code_history.py -q
cd web
npm ci
npm run typecheck
npm run lint
npm run test
npm run build
PLAYWRIGHT_EXECUTABLE_PATH=/usr/bin/chromium npm run test:e2e
```

浏览器验收启动真实服务、网关和 AgentRunner，以脚本模型在临时项目内执行审批、文件修改、回退、重启等操作，不访问用户模型配置；8766 为测试控制端口，不进入生产网关白名单。
