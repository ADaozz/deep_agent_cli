# Web 接口补充（M3 已实现）

本文件补充用户提供的 `reference/DeepAgentCLI_HTTP_前端对接接口文档.md`，输入原件不修改。原件将两个读查询列为建议；本轮已实现，其他 API 与错误码保持兼容。完整浏览器/BFF映射见 [实施记录](frontend-integration-implementation.md)。

| 新增服务路由 | 浏览器同源路由 | 响应 |
|---|---|---|
| GET /v1/sessions/{session_id}/runs | GET /api/agent/sessions/{session_id}/runs | `{runs:RunSummary[],next_cursor:string\|null}` |
| GET /v1/projects/{project_id}/active-run | GET /api/agent/projects/{project_id}/active-run | `{run:RunSummary\|null,blocked?:boolean}` |

RunSummary只有 `id,session_id,project_id,status,created_at,updated_at`。任务详情、工具参数及回答通过原 GET run 和 SSE 读取，不在列表重复长文本。

- 认证：两接口需要 Bearer；BFF在服务端注入，浏览器不能持有令牌。未知/移除项目、会话404；未授权401。
- 分页：`limit=50`，1～100；游标由最后一项时间/ID/会话编码生成，作为opaque值传回，错误或跨会话游标422。排序 `(created_at DESC,id DESC)`，同时间以ID稳定排序，使用键集而非offset。
- 活跃：持 RunManager 锁读取实际 worker 槽位及已提交的任务状态。running/waiting_confirmation/waiting_human/paused/runtime_config_boundary/cancelling均可返回；completed/failed/cancelled/interrupted不作为活跃。无任务run=null；清理失败残留占用返回run=null且blocked=true。
- 新浏览器先发现 active-run，再定位其 Session 和 Run；选择普通会话后读取摘要、详情和保留的SSE事件。标题只用会话ID片段，没有原始prompt时明确显示不可用。
- 原始协议差异：实际 Runner 的 `delta.kind=assistant/reasoning` 是累计文本；前端assistant替换，reasoning不展开。参考 `answer` 增量例子独立兼容。终态 output 权威覆盖，未知kind不猜为回答。
- Other：当前 Runner 返回自由文本本身，multi_select包含原选项值与自由文本；不会发送 `__other__` marker 代替用户输入。已由真实 Runner 集成测试确认。

只新增读端点和一个索引；没有会话命名/删除、prompt持久化、Diff、模型/权限切换、多模态或文件下载API。部署仍限本地单用户，Unix socket 间接通信及 OUTPUT-PERSIST-01 限制见原服务文档。

## 用户追加：按轮次回退代码

GET `/v1/runs/{run_id}/code-checkpoint`、POST `/v1/runs/{run_id}/code-restore`及同源BFF映射已实现。POST只接受action(undo/redo)和当前revision，禁止客户端schema/路径/内容/force；成功200、冲突409。完整契约及限制见 [code-restore.md](code-restore.md)。代码快照状态独立于Run/SSE，操作后GET对账，不自动重试POST。
