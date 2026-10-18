# Stitch Web / HTTP 集成实施记录

> 历史记录：该 Stitch 前端已于 2026-10-18 被替换。当前界面与验证见 [新前端适配验收](frontend-console-migration.md)。

## M0 基线

本轮基线：`main` / `f9d1e028bc92ee18f4efd462fbd2c50191c78087`，开始时工作区干净，无 web/、无 AGENTS.md。实施分支 `feat/stitch-web-workspace`。输入保留在 `.codex-input/DeepAgentCLI_Stitch_Codex`，20 个文件的 MANIFEST 校验全部通过；不修改附件。未执行 reset/clean/rebase/stash；不推送、合并或发布。

已读取 CODEX_TASK、README、START_HERE、plans/01–07、参考接口文档、类型和三个 fixture，核对两段 Stitch CSS、原稿截图和审批参考图。实现按 M0→M4 推进。

## 范围与架构

- `web/`：Vue3 + TypeScript + Vite，原生 CSS、原稿内联 SVG；轻量 composable 保存 runId→快照/游标/输出/工具，不引入全局状态依赖或 UI 库。
- `agent/web_gateway/`：独立 FastAPI BFF，不创建 RunManager，不访问服务 SQLite，只白名单转发既有服务。令牌仅来自 BFF 服务端环境；浏览器永远使用同源 `/api/agent`。
- M3 仅增两项只读查询：`agent/server/{app,runtime,store}.py`；旧写接口、审批顺序、finish 事务、Runner 和 bwrap 不改变。
- 输入资料、node_modules、构建与测试临时产物忽略；截图和小体积测试摘要归档到 docs/frontend-evidence，不引入大段原始 PTY 转储。

## 实际协议核查与文档差异

1. `agent/stream.py::StreamDeltaCallback` 发送 `assistant` / `reasoning` **累计文本**，不是附件 `answer` 示例的逐片追加。实际 assistant 更新替换；测试文档 answer 增量以兼容方式单独归并。reasoning 不展开正文。最终 `result.output` 权威覆盖。
2. `agent_event` 是工具/计划等结构化事件，不能再追加 Assistant 文本；只有服务顶层 interaction_requested 的 Run 快照带当前 interrupt_id。
3. Human-input 已由共享函数基于 Runner payload 校验 required 空值，HTTP 入队前 422；false 有效，可选可省略。后端未强制字段类型/枚举成员。Other 的实际 CLI 编码是自由文本直接替代 marker，多选用原选项值加自由文本；须用真实 Runner 测试确认后才开放。
4. Run 表现保存提交文本，会话列表返回截断标题。更早没有该字段的运行仍提示指令不可用。保存前会按服务密钥表打码。
5. M0 时两个只读 API 尚不存在；M3 实现前不使用虚构响应。模型/分支/Diff/测试统计/截图均无可信 HTTP 来源，隐藏或明确空态。

## API 映射

浏览器前缀 `/api/agent`，上游 `/v1`（health 除外）。全部上游 /v1 需认证；BFF 不透传浏览器 Authorization。

| 浏览器路径 | 方法 | 上游 | 用途 |
|---|---|---|---|
| /health | GET | /health | HTTP 存活 |
| /projects | GET | /v1/projects | 真实项目 ID |
| /sessions | GET/POST | /v1/sessions | 最近100会话/创建 |
| /sessions/{id} | GET | /v1/sessions/{id} | 归属 |
| /sessions/{id}/runs | POST | 同名 /v1 | text 或显式 resume:true |
| /runs/{id} | GET | 同名 /v1 | 权威状态 |
| /runs/{id}/events | GET | 同名 /v1 | SSE；after、Last-Event-ID |
| /runs/{id}/approvals | POST | 同名 /v1 | 整批一项一决策 |
| /runs/{id}/human-input | POST | 同名 /v1 | 五类动态字段 |
| /runs/{id}/continue | POST | 同名 /v1 | 当前 interrupt_id |
| /runs/{id}/cancel | POST | 同名 /v1 | 异步取消 |
| /sessions/{id}/runs | GET（M3新增） | 同名 /v1 | keyset分页summary |
| /projects/{id}/active-run | GET（M3新增） | 同名 /v1 | 项目活跃快照 |

## 页面状态与可靠性

running / waiting_confirmation / waiting_human / paused / runtime_config_boundary / cancelling；终态 completed / failed / cancelled / interrupted。202 不算完成；掉线不算失败；取消202先显示 cancelling。interrupted 明确用户确认后新建恢复 Run；不自动重放。审批决定初始未选，必须整批明确选择。409 拉快照且清空旧决定；422 保留表单。每 Run 独立幂等游标，解析成功才推进；切换关闭旧连接，重连 GET 对账且不重发 POST。

日志保留在状态内，DOM 限量显示并可展开；不治理 OUTPUT-PERSIST-01。未知事件不伪造动作。使用 Vue 转义，无 v-html/innerHTML。

## 视觉冻结

按原稿两层级联 CSS保留：中心 #000000、两侧 #222224、218px / flexible / 252px、消息850px上限、composer #050505、透明向上发送箭头。保留原字号/间距，新增动作低调；审批在任务流内，工具日志默认折叠。测试1600×1280、1440×900和窄屏侧栏/Inspector真抽屉、焦点/Esc/键盘/IME/减少动效。

## 测试设计与启动

M0 先跑现有 server/human-input 基线。M1 Vitest状态/表单/组件、typecheck、lint、build；M2 BFF安全与实际HTTP流式/断线测试；M3旧SQLite、100+分页、权限与并发查询；M4现有全量pytest、Playwright前端→BFF→真实service（受控Runner/模型，不冒称外部LLM）、截图和安全扫描。

覆盖清单沿用 plans/06：UI-01～06、SSE-01～06、APP-01～04、HUM-01～03、RUN-01～05、API-01～03、SEC-01～04。每项在最终报告绑定具体测试；未具备真实模型/原生环境的场景明确 NOT RUN。

开发三进程均 loopback：现有 `deep-agent serve --config <测试配置> --port 8080`；M2提供 `python -m agent.web_gateway.main --port 8765`；`cd web && npm ci && npm run dev`（5173）。部署构建后由BFF同源托管，安装wheel后须显式WEB_DIST_DIR，不假设存在源码路径。
