# HTTP 后端服务

服务复用 `AgentRunner`，用于现有系统后端调用。支持预配置项目、持久化会话、后台任务、SSE 进度、人工审批、补充回答和取消。CLI 继续使用原来的启动方式。

## 安装与启动

需要 Linux / WSL2、Python 3.12+ 和可用的 Bubblewrap。服务始终要求沙箱，不接受宿主执行回退。

```bash
pip install "deep-agent-cli[server]"
# 从源码安装：pip install -e ".[dev,server]"
```

先准备原有格式的模型配置，如 `~/.deep-agent/config.yaml`。创建服务器配置 `~/.deep-agent/service.yaml`：

```yaml
model_config: config.yaml
state_dir: server-state
# 从环境变量读取服务令牌，不写在 YAML 中。
token_env: DEEP_AGENT_SERVICE_TOKEN
max_runs: 2
projects:
  app: /srv/projects/app
  worker: /srv/projects/worker
```

路径相对于服务器配置文件解析，项目目录必须已经存在，项目目录之间不能重叠。模型配置、服务器配置、Agent 配置目录和服务状态目录必须位于所有项目工作区之外。服务状态目录应固定使用，项目 ID 与目录的映射在持久化会话存在期间应保持稳定。

```bash
export DEEP_AGENT_SERVICE_TOKEN='<设置一个随机服务令牌>'
deep-agent serve --config ~/.deep-agent/service.yaml --host 127.0.0.1 --port 8080
```

服务启动时检查模型配置和各项目沙箱。`sandbox.allow_unsandboxed` 在服务模式下强制关闭，权限固定使用 `ask`。

配置在启动时加载，修改模型或项目配置后需要重启服务。

第一版使用一个进程和本地 SQLite。状态目录的文件锁会拒绝第二个服务进程；请勿使用多个 Uvicorn worker，也不要为同一批项目启动使用不同状态目录的服务。调用方只提交项目 ID，不能传宿主工作区路径。

## API

除 `GET /health` 外，所有接口均需要 `Authorization: Bearer <服务令牌>`。`GET /openapi.json` 提供同样受令牌保护的 OpenAPI 文档。项目查询只返回 ID，不暴露服务器目录或凭据。

| 请求 | 用途 |
| --- | --- |
| `GET /v1/projects` | 列出项目 |
| `POST /v1/sessions` | 通过 `project_id` 创建会话 |
| `GET /v1/sessions?project_id=app` | 查询会话，最多返回最近 100 条 |
| `GET /v1/sessions/{session_id}` | 查询会话归属与创建时间 |
| `POST /v1/sessions/{session_id}/runs` | 提交 `text`，或用 `resume: true` 恢复中断 |
| `GET /v1/runs/{run_id}` | 查询任务状态、结果与待处理交互 |
| `GET /v1/runs/{run_id}/events` | SSE 事件流 |
| `POST /v1/runs/{run_id}/approvals` | 提交完整的一批工具审批 |
| `POST /v1/runs/{run_id}/human-input` | 提交补充回答 |
| `POST /v1/runs/{run_id}/continue` | 继续 `paused` / `runtime_config_boundary` 中断 |
| `POST /v1/runs/{run_id}/cancel` | 取消任务 |

创建会话返回 `201`。提交任务、交互或取消返回 `202`，执行结果通过查询和 SSE 获取。未知资源返回 `404`，请求格式错误返回 `422`，项目占用、交互过期或审批不完整返回 `409`，并发容量已满返回 `429`，关闭期间的任务提交返回 `503`。

任务状态包括 `running`、`waiting_confirmation`、`waiting_human`、`paused`、`runtime_config_boundary`、`cancelling`，以及终态 `completed`、`failed`、`cancelled`、`interrupted`。结果保存在 `result` 中，正常回答位于 `result.output`，错误位于 `result.error`。

同一项目同时只允许一个未结束任务，包括等待审批的任务；其他项目可并行，默认最多两个。服务器不自动排队，调用方需要处理 `409` 和 `429`。同一会话可以按顺序提交多次任务。

项目互斥只覆盖该服务中的任务；CLI 或其他程序对同一目录的修改需要由系统协调。每个任务结束后关闭 Runner 和沙箱，下一次任务恢复同一会话的 checkpoint，沙箱中的 `/tmp` 和后台进程不跨任务保留。

## 调用示例

以下 Python 示例可作为现有系统后端的接入参考，需要 `httpx`。`session_id` 和 `run_id` 都应由业务系统保存。

```python
import os
import httpx

client = httpx.Client(
    base_url="http://127.0.0.1:8080",
    headers={"Authorization": f"Bearer {os.environ['DEEP_AGENT_SERVICE_TOKEN']}"},
    timeout=30,
)

response = client.post("/v1/sessions", json={"project_id": "app"})
response.raise_for_status()
session_id = response.json()["id"]

response = client.post(
    f"/v1/sessions/{session_id}/runs",
    json={"text": "检查项目中的测试失败原因，并修复相关代码。"},
)
response.raise_for_status()
run_id = response.json()["id"]

# 在独立连接中订阅进度；审批请求由业务系统界面另行提交。
with client.stream("GET", f"/v1/runs/{run_id}/events", timeout=None) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        print(line)

response = client.get(f"/v1/runs/{run_id}")
response.raise_for_status()
print(response.json()["result"])
client.close()
```

等待审批时，查询响应包含 `interrupt_id` 和 `result.pending_tool_calls`。每个工具调用使用现有 Runner 的 `toolCallId` 字段。把这一批工具逐项展示给用户，再一次性提交所有决定，避免未审查的调用被隐式批准：

```json
{
  "interrupt_id": "从当前任务响应取得",
  "decisions": [
    {"tool_call_id": "call-a", "decision": "approve"},
    {"tool_call_id": "call-b", "decision": "reject", "message": "暂不执行此命令"}
  ]
}
```

将上述 JSON 提交到 `/v1/runs/{run_id}/approvals`。每个待审批调用必须恰好出现一次；重复、遗漏或过期的审批返回 `409`。

等待补充信息时，问题位于 `result.human_input`，向 `/human-input` 提交：

```json
{"interrupt_id": "当前交互标识", "values": {"text": "只修改测试目录"}}
```

有结构化字段时，`values` 按问题中字段的 `id` 填写。`/continue` 的请求体只需要当前 `interrupt_id`；`/cancel` 不需要请求体。每轮等待生成新的交互标识，旧页面中的回答不会误用于下一轮问题。

## 事件与恢复

SSE 包含 `id`、`event`、JSON `data`。主要事件：

- `run_started`：任务已接收。
- `delta`：`kind`、`text` 形式的回答或思考增量。
- `agent_event`：原有结构化 `RunEvent`，包含工具、输出和用量等信息。
- `interaction_requested`：任务快照，含服务层 `interrupt_id` 和问题/待审批调用。
- `interaction_submitted` / `interaction_rejected`：交互已接收或回答校验失败。
- `run_finished`：终态快照。

使用服务层 `interaction_requested` 驱动审批界面；原始 `agent_event` 也可能包含 Runner 的交互通知。事件 ID 在服务数据库内递增，不要求连续。断线后通过 `Last-Event-ID` 请求头续接，也可以用 `?after=<事件 ID>`；请求头优先。事件流每 15 秒发送心跳，终态事件读完后关闭连接。断开 SSE 不会取消任务。

任务、事件和会话 checkpoint 保存在服务状态目录中。重启后，未结束任务标记为 `interrupted` 并追加结束事件，旧任务不会自动重放。恢复审批或提问时，向原会话提交 `{"resume": true}` 创建新任务，再处理返回的当前交互。会话没有中断时，使用新的 `text` 继续对话。运行中崩溃后的代码、文件或外部副作用需要先检查；checkpoint 恢复不保证工具恰好执行一次。

正常关闭会请求取消并等待 worker 清理。清理失败时保留项目占用，拒绝新写入，需重启服务。

## 系统接入边界

服务令牌代表整个内部系统，第一版不区分最终用户。现有系统负责登录、项目权限、审批界面和会话归属；令牌保留在系统后端。需要远程接入时，通过现有网关提供 HTTPS，并关闭 SSE 响应缓冲。

工作区文件工具仍可修改工作区内的 `.git`、`.env` 等文件，沙箱沿用现有项目的隔离边界。服务未增加 CPU、内存或磁盘配额。任务与事件暂不自动清理，部署时监控状态目录磁盘用量。健康接口表示 HTTP 进程可用，不代表模型端点始终可用。
