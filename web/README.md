# Deep Agent Web Console

Vue 3 + TypeScript + Vite。采用 `deep-agent-web-20261018.tar.gz` 的浅色双屏界面，替换原 Stitch 前端：侧栏会话导航、首页项目选择与输入框、会话线程、内联审批/人工输入、可展开工作日志和 Git 变更侧栏。

## 启动

从仓库根目录启动 HTTP 服务和本地网关；完整配置见 [Web 启动说明](../docs/web.md)。

```bash
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`。浏览器仅访问 `/api/agent`，Vite 转发到 `127.0.0.1:8765` 的 Python 网关，由网关向 `127.0.0.1:8080` 服务注入令牌。连接设置用于测试连接、修改本机项目显示名和清除本地记录；不需要在浏览器填写服务令牌。模型下拉复用 CLI `/model` 的组织 → 模型名称 → 思考强度流程，收起后只显示当前模型名称；选择会实际应用并持久化到会话。

```bash
npm run typecheck
npm run lint
npm run test
npm run build
npm run test:e2e
```

端到端测试使用系统 Chromium（默认依次查找 `/usr/bin/chromium`、`/usr/bin/chromium-browser`、`/usr/bin/google-chrome`、`/usr/local/bin/google-chrome`，也可设置 `PLAYWRIGHT_EXECUTABLE_PATH`），启动真实 Python 服务、网关、AgentRunner、SQLite 和 Bubblewrap，以脚本模型执行临时项目任务，不使用真实模型。需要先安装根目录的 `.[dev,server]` 到 `.venv`，且 5173、8080、8765、8766 端口空闲。测试会覆盖仓库里已跟踪的 `docs/frontend-evidence/*.png`。

## 行为

- 首页左侧选择 workspace：最近四个目录按最近选择排序，最后一项“选择路径”可浏览并选择实际目录。目录名称下方显示完整路径，重复选择不占多个历史位置；刷新后恢复最近目录。
- 右侧选择 workspace 内实际 Git 分支；分支切换使用原生 Git，不强制覆盖本机修改，活动任务时禁止切换。无 Git 的目录不显示此控件。
- 首次提交在所选 workspace 创建会话，线程内追加任务。
- 模型下拉依次选择组织、模型和思考强度。首页选择用于首轮，会话内切换立即保存；刷新和服务重启后恢复。运行或等待交互时禁止切换，切换失败保留原模型。
- 审批按整批提交；人工输入支持 text、textarea、boolean、单选、多选和服务声明的自定义选项；暂停后可继续。
- SSE 使用每轮独立游标、累计快照替换、断线重连和 GET 轮询。关闭页面不取消任务，取消不回滚文件。
- 服务端运行摘要分页和项目活动任务用于跨浏览器发现；终态也回放事件恢复日志。原始提问随任务保存，刷新后用于标题和正文；更早没有保存提问的运行会提示不可用。每次加载 20 轮，可点击“加载更早运行”。
- 首页和变更侧栏读取对应 workspace 的 Git 状态与 diff 统计；没有仓库时隐藏分支/diff/合并信息。合并标记只表示 HEAD 已包含在默认分支中。
- 前端仅负责呈现，执行、审批、取消和清理沿用 main；已移除独立的每轮代码快照及回退机制。
- 输入框加号菜单只含“上传文件”和“Skills”。文件入口可以添加普通文件或图片路径。
- `localStorage` 只保存尚未发送的输入框草稿。提问、游标和会话列表在服务端；刷新后从服务恢复。旧版独立前端配置中的令牌和直连地址会移除。

## 目录

- `src/api`：网关 API 类型、请求与 SSE 解码。
- `src/store`：连接、历史分页、事件恢复和本机记录。
- `src/lib`：日志/文件变更展示模型及转义的 Markdown 渲染。
- `src/components`：新界面组件。
- `e2e`：真实服务联调，包含审批、输入、跨浏览器历史、重启恢复、真实 Git 呈现和窄屏。
- `scripts/mock-server.mjs`：上传包附带的可选开发 Mock。生产和自动验收使用真实服务；如需使用 Mock，应另行用同源网关接入。
