# 开发

## 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,server]"
```

需要 Python 3.12+。依赖只在 [`pyproject.toml`](../pyproject.toml) 中维护；`requirements.lock` 是某个 Python 3.12 开发环境的 `pip freeze` 快照，仅供参考。升级依赖后，在干净环境中安装 `.[dev]`、跑通测试，再更新快照。

用 pipx 安装过本项目时，修改源码后运行 `pipx install --force .` 并重启 CLI。

## 测试

```bash
pytest -q                          # 全部自动测试，不调用真实模型
pytest -m integration              # 只跑本地 HTTP 集成测试，需要能监听 127.0.0.1
REQUIRE_BWRAP_TEST=1 pytest -q     # bwrap 不可用时让沙箱测试失败，而不是跳过
```

- `integration` 测试启动一个本地模拟的 OpenAI Chat Completions 服务，覆盖流式回答、工具调用、审批、暂停、恢复和 SQLite 重启恢复。
- `sandbox` 标记的测试需要真实可用的 Bubblewrap；不可用时默认跳过。跳过不代表当前环境兼容。
- `tests/test_server.py` 验证 HTTP 服务接口、审批、并发、事件续接和会话恢复；未安装 `server` 依赖时跳过，CI 安装该依赖并执行这些测试。

## MCP 工具接入

MCP 内置依赖为 `langchain[mcp]>=1.4.0,<2.0`，继续使用 `deepagents>=0.7.17,<0.8.0`。`agent/mcp/client.py` 仅通过官方 `langchain.mcp.MCPAdapter` 发现、转换工具，传输显式使用 FastMCP 的 HTTP 或 SSE Transport；不解析协议或启动本地服务。

`bridge.py` 每套已发现的工具拥有一个稳定 asyncio 循环，完整委托原工具 `ainvoke()`，参数校验和结果格式化只执行一次。取消通过现有 `ToolCancelContext`（Run Generation、Tool Call ID）绑定单次 Task；等待 Task 的收尾完成才返回。`Runner.close()` 在关闭桥接资源之前不会释放 Session Lease。模型/权限重建复用 `AgentSpec.tools`，不把 Client 放进 Checkpoint。

切换流程先发现、校验和构建候选 Graph，再原子保存独立 `mcp.yaml`，最后提交内存状态并关闭旧资源。原有同步 `graph.stream()`、SQLite、HITL 和模型取消链路均保留。服务模式只读启动快照，详见 [MCP 接入](mcp.md)。

`tests/test_mcp_client.py` 启动真实 HTTP/SSE FastMCP 测试进程，经官方 Adapter 和真实同步 LangGraph ToolNode 验证 Schema、文本、图片、文件、结构化 artifact 和错误状态。其他 MCP 测试覆盖配置原子保存、并发取消、Runner 审批/拒绝、HTTP Cancel、SQLite 恢复、模型/权限/会话切换及 `/mcp` 交互。测试服务只在测试目录存在，不属于产品接口。

## 真实模型冒烟

以下脚本会使用配置中的 API key 并消耗额度，不在 CI 中运行：

```bash
python examples/e2e_live_smoke.py [--model 来源/模型] [--workspace 空目录] [--network]
python examples/stream_smoke.py [--effort 强度]
python examples/sandbox_probe.py --require-sandbox [--report report.json]
```

- `e2e_live_smoke.py`：通过 CLI 应用对象执行斜杠命令、`ask` 审批、`allow` 模式和一个编写并运行单元测试的任务。默认创建并清理临时工作区；指定 `--workspace` 时必须是空目录。`--network` 额外测试访问 example.com。
- `stream_smoke.py`：验证 Responses 的思考流。要求当前模型 `api: responses`、端点支持 Responses 且模型会输出思考内容；`--effort` 必须来自配置中的列表且不能是 `none`。Chat Completions 端点不适用。
- `sandbox_probe.py`：不调用模型，只检测沙箱是否可用，输出 JSON 报告。

## CI

[`.github/workflows/tests.yml`](../.github/workflows/tests.yml) 在 Ubuntu 24.04 上运行：

- Python 3.12 和 3.13：为 CI 专用的 bwrap 副本添加 AppArmor userns 授权，要求沙箱可用，运行全部测试，构建 wheel，并在干净虚拟环境中验证首次启动和非 TTY 退出码。
- 默认安全策略：不修改 AppArmor 和 sysctl，记录沙箱是否可用，并验证不可用时的诊断信息。

## 发布

推送 `v*` 标签触发 [`.github/workflows/release.yml`](../.github/workflows/release.yml)：先跑测试，检查标签与 `pyproject.toml` 中的版本一致，构建后发布到 GitHub Releases（说明取自 `.github/release-notes.md`）和 PyPI（trusted publishing）。**只合并 PR 或推送 `main` 不会触发 PyPI 发布**。

发布时先确保 README、Release Notes、`pyproject.toml` 的版本一致，且对应提交的 CI 已通过；标签必须指向包含这些文件的 `main` 提交。例如发布 `0.1.6`：

```bash
git switch main
git pull --ff-only origin main
git tag v0.1.6
git push origin v0.1.6
```

通过 GitHub Actions `release` 工作流确认 `tests`、`build`、`github`、`pypi` 都成功后，再到 GitHub Releases 和 PyPI 核对 `0.1.6` 的 wheel 和源码包。若发布失败，应检查失败步骤后修复，**不要对已经发布到 PyPI 的版本号重新上传覆盖**。

## 贡献

- 修改行为时附带测试。
- 不要提交 `config.yaml`、`.venv/`、会话数据库或 `.deep-agent/` 目录。
