# 开发

## 环境

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

需要 Python 3.12+。依赖只在 [`pyproject.toml`](../pyproject.toml) 中维护；`requirements.lock` 是某个 Python 3.12 开发环境的 `pip freeze` 快照，仅供参考。升级依赖后，在干净环境中安装 `.[dev]`、跑通测试，再更新快照。

用 pipx 安装过本项目时，修改源码后运行 `pipx install --force .` 并重启 CLI。

修改 `agent/config.example.yaml` 只会影响首次生成的用户配置。已有配置不会被升级或重装覆盖，验证新模板时使用临时 HOME 或独立测试配置。Node、Rustup、X11 等默认工具挂载的说明见 [配置参考](configuration.md#默认工具挂载)。

## 测试

```bash
pytest -q                          # 全部自动测试，不调用真实模型
pytest -m "not sandbox"            # 未标记与集成测试；部分 SubAgent 用例仍启动沙箱
pytest -m integration              # 只跑本地模拟模型端点的集成测试，需要能监听 127.0.0.1
pytest -m pty                      # 真实 PTY 快捷键；这些测试同时带 sandbox 标记
REQUIRE_BWRAP_TEST=1 pytest -q     # bwrap 不可用时让沙箱测试失败，而不是跳过
```

- 未标记测试主要使用脚本模型和内存 Backend；部分 SubAgent 专项也直接启动本地 HTTP 端点或真实沙箱，不能仅凭无标记判断其环境需求。
- `integration` 测试启动一个本地模拟的 OpenAI Chat Completions 服务，覆盖流式回答、工具调用、审批、暂停、恢复和 SQLite 重启恢复。
- `sandbox` 标记的测试需要真实可用的 Bubblewrap；不可用时默认跳过。跳过不代表当前环境兼容。
- `pty` 标记的测试通过真实伪终端发送按键。输入、取消、退出和会话租约的当前契约见 [重构基线](refactor-baseline.md)。
- CLI 测试按渲染、交互、布局和运行控制拆在 `tests/test_cli_render.py`、`tests/test_cli_interaction.py`、`tests/test_cli_layout.py` 和 `tests/test_cli.py`。共用绘制与按键辅助函数在 `tests/cli_support.py`。
- `/sandbox mounts` 的文本来自当前模式筛选后的启动策略，测试不把它当成内核实时挂载表。

默认挂载模板的验证分为两个目标：配置在可选目录缺失、HOME 不同的机器上仍可加载；指定 PATH 后，已挂载的工具能在真实沙箱中运行。修改模板时运行 `pytest tests/test_config.py tests/test_bootstrap.py -q`，并用临时安装目录验证多版本 Node 的手动选择、Rustup 路径、只读工具目录与读写缓存。启动时的 `true` 预检只验证沙箱本身，不替代工具运行验证。

## SubAgent 开发契约

只有当问题能分解为多个独立、互不干扰的子问题时，主 Agent 才应派生 SubAgent；
这条条件位于 `agent/factory.py` 中 `general-purpose` 的描述，由主模型判断，
不增加原生 `task` 参数，也不自动检测共享文件的修改冲突。实现边界见 [架构](architecture.md#subagent-委派边界)。

修改相关逻辑后运行现有回归：

```bash
pytest -q tests/test_subagents.py tests/test_subagent_lifecycle.py tests/test_subagent_logs.py tests/test_config.py tests/test_model_switch.py
pytest -q tests/test_cli_layout.py tests/test_tui_pty.py -k subagent_counter
```

覆盖原生委派、上下文与工具隔离、真实沙箱并发/取消、两种 API 的独立思考强度与客户端回收，
以及日志精确替换、分页和重启查询。依赖仍为 Deep Agents `>=0.7.17,<0.8.0`，
兼容性验证应在独立环境安装 0.7.17 后运行相关测试。
仅改说明文案时复用现有测试并检查实际 task 描述，避免把提示词条件误写成运行时安全校验。

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

发布时先确保 README、Release Notes、`pyproject.toml` 的版本一致，且对应提交的 CI 已通过；标签必须指向包含这些文件的 `main` 提交。例如发布 `0.2.0`：

```bash
git switch main
git pull --ff-only origin main
git tag v0.2.0
git push origin v0.2.0
```

通过 GitHub Actions `release` 工作流确认 `tests`、`build`、`github`、`pypi` 都成功后，再到 GitHub Releases 和 PyPI 核对 `0.2.0` 的 wheel 和源码包。若发布失败，应检查失败步骤后修复，**不要对已经发布到 PyPI 的版本号重新上传覆盖**。

## 贡献

- 修改行为时附带测试。
- 不要提交 `config.yaml`、`.venv/`、会话数据库或 `.deep-agent/` 目录。
