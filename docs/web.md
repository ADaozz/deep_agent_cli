# 本地 Web 工作区

Vue3 + TypeScript 界面通过独立的同源网关访问原 HTTP Agent 服务。适用范围是 **本地单用户**，不是多租户或公网部署。服务令牌只在 Python 网关进程中；不要使用 VITE_* 变量、浏览器 Authorization 或 LocalStorage 保存令牌。

## 前置条件

源码环境：Python 3.12+、可用 Bubblewrap、Node（已验证24.19.0，Vite要求20.19+或22.12+）、npm。Python复用原有 `[server]` extra 的 FastAPI/Uvicorn/httpx，不新增 Python 项目依赖。生产服务需要用户已有的模型配置；测试使用脚本模型，不调用外部LLM。

```bash
python -m pip install -e '.[dev,server]'
cd web
npm ci
npm run typecheck
npm run lint
npm run test
npm run build
cd ..
```

## 开发：三个本地进程

先按 [服务文档](server.md) 配置一个专用测试项目与 state_dir。每个服务目录只开一个服务实例，不能复用生产数据。以下令牌占位符应由用户在自己的终端替换，代码不会自动写入配置文件或 `.env`。

```bash
# 终端1：原服务。准备好自己的 service.yaml。
export DEEP_AGENT_SERVICE_TOKEN='<本机随机服务令牌>'
deep-agent serve --config ~/.deep-agent/service.yaml --host 127.0.0.1 --port 8080

# 终端2：独立 BFF。使用与服务相同的令牌，只存在服务端环境中。
export DEEP_AGENT_SERVICE_TOKEN='<与终端1相同的服务令牌>'
export DEEP_AGENT_SERVICE_BASE_URL='http://127.0.0.1:8080'
python -m agent.web_gateway.main --host 127.0.0.1 --port 8765

# 终端3：浏览器只访问 Vite 的同源地址。
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite把 `/api/agent` 代理到网关，不注入服务令牌；网关只向 loopback 上游注入令牌。开发代理会传播上游中断和客户端关闭，避免遗留 SSE 连接。

## 构建后同源运行

```bash
cd web && npm ci && npm run build && cd ..
export WEB_DIST_DIR="$PWD/web/dist"
# 仍先运行原 Agent 服务，且在此终端配置服务令牌与上游地址。
python -m agent.web_gateway.main --host 127.0.0.1 --port 8765
```

访问 `http://127.0.0.1:8765`。网关不创建第二份 AgentRunner/RunManager，不访问服务状态库。省略 WEB_DIST_DIR 时为 API-only 网关，开发模式由 Vite 提供网页。

Python wheel 包含 `agent.web_gateway`，**不包含 web/dist**。从 wheel 安装时另行准备前端构建目录，并显式设置 WEB_DIST_DIR；不会假设安装目录附近存在源码 web/。

## 配置与安全

| 环境变量/参数 | 默认/作用 |
|---|---|
| DEEP_AGENT_SERVICE_TOKEN | 必填，原服务令牌，只供网关使用 |
| DEEP_AGENT_SERVICE_BASE_URL | http://127.0.0.1:8080，只允许loopback HTTP/HTTPS原点，不允许地址含用户名/路径/查询 |
| WEB_DIST_DIR | 可选，已构建的静态目录；StaticFiles阻止目录穿越 |
| DEEP_AGENT_WEB_ORIGINS | 可选，逗号分隔的本地原点；默认网关的127.0.0.1/localhost/IPv6端口及Vite5173原点 |
| --host / --port | 默认127.0.0.1 / 8765，只允许loopback host |

如果修改Vite开发端口，必须同步网关允许的原点；不要配置通配CORS。POST必须同源且为application/json，请求体上限1MiB。网关显式映射白名单，不能用它访问任意上游路径、openapi、外部URL或主机文件。只读端点也需要原服务认证。503表示无法连接Agent服务，401通常是两个进程令牌不一致，403表示Host/Origin不匹配。

浏览器掉线不代表任务失败；它按Run保存已应用游标，先GET对账，再退避续接。取消202只表示取消中。交互409清空旧决定并重新同步，422保留表单等待修改。服务重启后旧Run为interrupted，用户确认副作用后才能发resume:true新Run。暂停/配置边界支持继续；取消/失败后可能还保留checkpoint，手动恢复由服务进一步验证，不自动重放。

## 功能与协议缺口

支持项目/最近会话、创建会话、文本任务、摘要分页与跨浏览器发现、SSE/工具日志、整批审批、五类输入/Other、暂停继续、取消与显式恢复。原始prompt未写入服务Run表；刷新/新设备时只显示“原始指令不可用”，会话以ID片段命名。无模型/分支来源时不显示猜测值。

右侧代码变更展示本Run成功的write_file/edit_file/delete工具事件：文件路径、新建/覆盖/删除记录、写入内容或替换前后片段。新建与覆盖分类只使用工具结果artifact.operation；缺少分类时明确标记未确认。失败、拒绝及未完成的调用不计入；刷新通过原SSE回放恢复。编辑片段没有全文件上下文/实际行号，覆盖与删除的旧内容也未记录，不能当作完整Git Diff。命令/外部进程引起的改动无法由现有事件可靠追踪。

无HTTP模型/权限/Skills切换、上传、多模态、steering、队列、完整工作区Diff、结构化测试统计或二进制截图下载；测试/预览面板明确空态。日志默认折叠，DOM显示窗口有限，完整已接收内容保留在本轮状态/服务日志。没有把最终回答中的文字解析成Diff或测试通过数。

支持按任务历史轮次进行代码回退和撤销回退，包含记录成功的execute净文件改动；具体范围、冲突及快照上限见 [轮次代码回退](code-restore.md)。代码回退不撤销对话或模型checkpoint，旧轮次没有完整快照时不可用。

Unix socket/文件/FIFO间接通信和 OUTPUT-PERSIST-01 仍是 [服务已知边界](server.md)，本轮不修改沙箱或事件持久化策略。

## 自动化与截图

```bash
TERM=xterm-256color REQUIRE_BWRAP_TEST=1 .venv/bin/python -m pytest -q -ra
cd web
npm ci && npm run typecheck && npm run lint && npm run test && npm run build
# 使用已安装Chromium；按本机路径调整。未自动下载浏览器。
PLAYWRIGHT_EXECUTABLE_PATH=/usr/lib/chromium/chromium npm run test:e2e
```

浏览器测试启动Vite、独立网关、真实HTTP服务和真实AgentRunner/SQLite/Bubblewrap，使用受控脚本模型与临时项目，不访问用户模型配置。8766仅是测试进程控制端口，用于崩溃/恢复和密钥审计；不在生产网关白名单中、不进入Python wheel。端口5173/8080/8765/8766必须空闲，测试不会接管已有进程。

原有 Playwright CLI 沙箱测试额外需要其CLI和浏览器。最终报告明确列出实际命令与前置条件；浏览器启动失败不算通过。见 [验收报告](frontend-integration-test-report.md)、[接口补充](frontend-http-api.md)、[实施记录](frontend-integration-implementation.md)。
