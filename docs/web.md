# 本地 Web Console

当前 `web_beta` 已用 `deep-agent-web-20261018.tar.gz` 的 Vue 3 浅色界面替换旧 Stitch 工作区。首页负责选择 workspace、Git 分支并启动任务；线程负责回答、工作日志、内联审批、人工输入；首页和右侧变更栏读取对应 workspace 的实际 Git 信息。旧界面的验收报告是历史记录，当前验证见 [新前端适配验收](frontend-console-migration.md)。

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

- workspace 左侧菜单显示当前页面最近选择的四个历史目录，最后一项“选择路径”打开目录浏览器。可输入路径、浏览子目录或返回上一级；选中目录会保存到服务目录索引并用于新会话。
- 右侧菜单显示所选 workspace 的实际本地 Git 分支，支持原生分支切换。无 Git 时隐藏，活动任务期间禁用；遇到 Git 冲突时保留现有修改并提示失败。
- 创建会话、文本任务、五类人工输入与 Other、工具批量审批、暂停继续、取消和中断恢复。
- 从服务发现活动任务和运行历史；刷新后从服务端回放日志与最终回答。任务提交时会保存原始提问，重启后侧栏标题和线程正文仍使用这段文字。更早、尚未保存提问的运行显示不可用。每次加载 20 轮，可继续加载更早记录。
- 同一项目已有未结束任务时，再次从首页发送会得到 409，输入保留，并且不会留下没有任务的空会话。带 `#thread=` 打开页面时，连接完成前发送会追加到该会话。
- 浏览器仅在页面内存中保留 SSE 游标，刷新后从服务日志重新回放，断线先校正状态再重连，轮询兜底。服务重启后旧 Run 为 `interrupted`，点击“恢复”会创建新 Run，并可能重新出现尚未处理的审批/提问。
- 取消是 `cancelling` 到终态的协作过程，不撤销文件改动；关闭页面也不取消任务。
- 首页卡片和变更侧栏直接读取 workspace 的 Git 分支、diff 统计与文件状态。无 Git 时隐藏相关卡片信息；“已合入”只依据当前 HEAD 在默认分支中的实际祖先关系。
- 变更侧栏可在整体、已暂存和未暂存之间切换。点击文件展开服务端返回的行级差异，可折叠差异区块、查看完整变更并刷新工作区。未跟踪文件只在未暂存中列出，不出现在整体差异和已暂存列表。
- 已移除每轮代码快照、回退/撤销回退以及对应接口；任务执行、交互与取消复用 main。
- localStorage 只保存输入框中未发送的草稿；旧版会话缓存、设置、最近目录和最后会话键会自动移除。会话定位使用 URL 中的 thread ID，其余界面状态仅放在页面内存中。
- 草稿仅存于浏览器 localStorage，按页面上的会话 ID 隔离；切换会话、刷新后恢复。发送成功仅清除原会话尚未变化的草稿，发送失败保留。首页未创建 thread 时按 workspace 路径暂存独立草稿。
- Web 会话 ID 就是 CLI 的 thread ID，两边读写同一份 workspace 会话库。`deep-agent resume <id>` 可以打开页面上的会话，页面列表也会出现 CLI 已经保存的会话。HTTP 运行记录和事件流仍在服务自己的状态库里。
- 同一个 workspace 不能同时被 CLI 和 Web 操作。CLI 开着时提交任务会得到 409；Web 任务未结束时 CLI 启动会退出。TUI 配置里的 `workspace:` 会覆盖当前目录，和 Web 一起用时请准备一份不改写 workspace 的配置。
- 加号菜单仅显示“上传文件”和“Skills”。文件入口可添加普通文件或图片路径，按 TUI 的 `.png/.jpg/.jpeg/.webp/.gif` 后缀规则判断图片（不区分大小写）；普通文件不受图片能力限制。添加图片时前端先检查当前模型 `input` 是否含 `image`，后端提交接口对 `file_paths` 再校验实际会话模型，不支持则返回 422。两端共用规则和“请切换到支持图片理解的模型”文案，并通过同一个文件提示组件显示。
- 路径块只记录路径，不读取、编码或传输文件内容。正文及 Skill 指令放入 `text`，路径字符串放入 `file_paths`；后端校验后逐行拼入 Agent 提示词。支持仅发送文件路径，中文和路径内空格保持完整。
- 加号中的“Skills”通过扩展接口复用 TUI `/skill` 的原生加载器，选择后显示可移除 Skill 块；提交时使用 CLI 的同一条技能读取指令。Skill 块随线程草稿保存在本地。
- 独立权限按钮只显示当前 `ask` 或 `allow`，点击展开切换菜单。权限切换复用 Runner 的 `ask / allow`：`allow` 仅限 SANDBOXED，展示 TUI 风险说明并输入 `ALLOW` 确认。首页选择在首轮任务前应用，线程内选择写入原生会话设置，重启后恢复。当前扩展接口要求 workspace 空闲且会话无待处理交互，忙时返回 409。
- 已删除扩展市场与语音入口。
- 新会话默认模型采用配置的 `llm.default`，已有会话遵循 TUI 保存的模型设置。模型下拉读取服务配置的组织、模型与可用思考强度，按该顺序选择；收起只显示模型名称。首页选择在首轮提交前应用，会话内选择立即保存，后续运行及重启恢复使用该设置。运行中或等待交互时不能切换。新增 workspace 继续使用 main 的目录边界与沙箱校验。HTTP 不传输附件内容，不提供语音、桌面、终端交互、PR 或提交操作。会话目录和 workspace 占用沿用 CLI 的 `SessionStore`：启动 CLI 与受理 HTTP 任务时各占一次同一把锁。TUI 界面本身没有改。

## 本机配置

| 配置 | 说明 |
| --- | --- |
| `DEEP_AGENT_SERVICE_TOKEN` | 服务及网关使用，浏览器不接收 |
| `DEEP_AGENT_SERVICE_BASE_URL` | 网关上游，默认 `http://127.0.0.1:8080`，限 loopback 原点 |
| `WEB_DIST_DIR` | 可选，前端构建目录；不设置时网关仅提供 API |
| `DEEP_AGENT_WEB_ORIGINS` | 可选，逗号分隔的本机原点，会替换默认列表。不设置时自动包含网关自己的端口和 Vite `5173`，例如网关在 `8775` 时允许 `http://127.0.0.1:8775`。自定义时写完整原点：`DEEP_AGENT_WEB_ORIGINS=http://127.0.0.1:8775,http://127.0.0.1:5173` |
| `--host / --port` | 网关默认 `127.0.0.1:8765`，只允许 loopback |

修改 Vite 端口时需同步网关允许的原点。网关保留 Host/Origin 检查、同源 JSON POST、1 MiB 请求体上限和接口白名单。503 请检查服务/网关进程，401 检查两端令牌是否一致，403 检查 Host/Origin。

## 自动验证

```bash
TERM=xterm-256color REQUIRE_BWRAP_TEST=1 .venv/bin/python -m pytest tests/test_server_models.py tests/test_server.py tests/test_web_gateway.py tests/test_server_gitinfo.py tests/test_server_workspaces.py tests/test_gitinfo.py -q
cd web
npm ci
npm run typecheck
npm run lint
npm run test
npm run build
PLAYWRIGHT_EXECUTABLE_PATH=/usr/bin/chromium npm run test:e2e
```

浏览器验收启动真实服务、网关和 AgentRunner，以脚本模型在临时项目内执行审批、文件修改、Git 查询、重启等操作，不访问用户模型配置；8766 为测试控制端口，不进入生产网关白名单。
