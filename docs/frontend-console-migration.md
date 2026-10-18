# 当前 Web 适配验收（2026-10-18）

界面采用上传的 `deep-agent-web-20261018.tar.gz`，SHA-256 为 `957834e1fa12fea71d6ae2c69f6178d5b6f4d0b15b2db6677c4e4318432439a7`。前端是 main 的另一种呈现方式；当前 main 基线为 `73605f2`。

## 当前行为

- 首页/线程共用输入框；加号菜单只有“上传文件”和“上传图片”两个入口，本次调整菜单呈现。
- 模型下拉按组织 → 模型名称 → 思考强度选择，收起只显示实际模型名称。会话选择复用 CLI `/model` 的 Runner 配置应用与持久化，运行或等待交互时不可切换。
- 首页卡片和变更侧栏从对应 workspace 读取 Git 分支、diff 行数与路径状态；无仓库或 Git 不可用时不展示卡片的 Git 信息。
- diff 与 HEAD 比较，包含暂存和未暂存修改；未跟踪、二进制及无提交仓库不编造行数。“HEAD 已合入”只代表当前提交已包含在默认分支历史中，不代表未提交修改或 PR 状态。
- 所有每轮文件修改汇总、前后文件快照、回退/撤销回退入口、接口、恢复锁和持久化逻辑均已删除。工具日志继续显示 main 原有的事件数据，不另建文件修改/回退记录。
- 会话摘要分页、原有 SSE 日志回放、审批、人工输入、取消、暂停继续与服务重启后的显式会话恢复继续可用；缺少本机提问时显示原始提问不可用。
- 网关注入令牌，浏览器不保存服务凭据。Git 查询只读，不 fetch、checkout 或写 Git 索引/提交。

## main 一致性核对

通过 AST 比较确认 `RunManager.submit`、`respond`、`cancel`、`_interruption`、`_work` 和 `close` 与 `origin/main` 完全一致。Runner、CLI、会话、控制、沙箱等核心文件与 main 无差异。Web 添加的模型接口调用 CLI 已有的模型切换方法，Git 元数据复用 main 的 `agent.cli.gitinfo.parse_status`；执行流程没有另一套实现。

## 验证

| 检查 | 结果 |
| --- | --- |
| `npm run typecheck` | 通过 |
| `npm run lint` | 通过 |
| `npm run test` | 4 个文件，10 项通过 |
| `npm run build` | 通过 |
| `npm run test:e2e` | 7 项通过（24.7 秒） |
| `TERM=xterm-256color REQUIRE_BWRAP_TEST=1 .venv/bin/python -m pytest tests/test_server.py tests/test_server_models.py tests/test_server_gitinfo.py tests/test_web_gateway.py tests/test_gitinfo.py -q --tb=short` | 72 项通过（14.10 秒） |

浏览器测试使用 Chromium、真实服务、网关、AgentRunner、SQLite 和 Bubblewrap；模型是受控脚本，不访问外部模型。验证了两种输入框的加号菜单、真实 Git 的分支/行数、无 Git 隐藏信息、无轮次回退入口、模型/强度实际执行及重启持久化、审批/人工输入、跨浏览器历史、断连恢复、取消和窄屏。

Git 测试建立真实临时仓库，覆盖暂存加未暂存修改、未跟踪文件、未合并/合并分支、detached HEAD、无提交仓库、无 Git、认证、旧回退接口 404，以及 Git HEAD/索引不被查询改写。

## 截图与启动

- [首页 Git 信息](frontend-evidence/console-home-git-1440.png)
- [线程](frontend-evidence/console-thread-1440.png)
- [Git 变更侧栏](frontend-evidence/console-changes-1440.png)
- [模型思考强度菜单](frontend-evidence/console-model-effort-1440.png)
- [窄屏](frontend-evidence/console-mobile-390.png)

启动配置见 [Web 文档](web.md)，接口见 [HTTP 服务文档](server.md)。先前 Stitch 报告仅供历史参考。
