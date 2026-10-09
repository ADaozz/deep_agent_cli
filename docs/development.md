# 开发

## 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
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

## 真实模型冒烟

以下脚本会使用配置中的 API key 并消耗额度，不在 CI 中运行：

```bash
python examples/e2e_live_smoke.py [--model 来源/模型] [--workspace 空目录] [--network]
python examples/stream_smoke.py [--effort 强度]
python examples/sandbox_probe.py --require-sandbox [--report report.json]
```

- `e2e_live_smoke.py`：通过 CLI 应用对象执行斜杠命令、`ask` 审批、`allow` 模式和一个编写并运行单元测试的任务。默认创建并清理临时工作区；指定 `--workspace` 时必须是空目录。`--network` 额外测试访问 example.com。
- `stream_smoke.py`：只验证 Qwen Responses 的思考流。要求当前模型 `provider: qwen-responses`、端点支持 Responses 且模型会输出思考内容；`--effort` 必须来自配置中的列表且不能是 `none`。Chat Completions 端点不适用。
- `sandbox_probe.py`：不调用模型，只检测沙箱是否可用，输出 JSON 报告。

## CI

[`.github/workflows/tests.yml`](../.github/workflows/tests.yml) 在 Ubuntu 24.04 上运行：

- Python 3.12 和 3.13：为 CI 专用的 bwrap 副本添加 AppArmor userns 授权，要求沙箱可用，运行全部测试，构建 wheel，并在干净虚拟环境中验证首次启动和非 TTY 退出码。
- 默认安全策略：不修改 AppArmor 和 sysctl，记录沙箱是否可用，并验证不可用时的诊断信息。

## 发布

推送 `v*` 标签触发 [`.github/workflows/release.yml`](../.github/workflows/release.yml)：先跑测试，检查标签与 `pyproject.toml` 中的版本一致，构建后发布到 GitHub Releases（说明取自 `.github/release-notes.md`）和 PyPI（trusted publishing）。

## 贡献

- 修改行为时附带测试。
- 不要提交 `config.yaml`、`.venv/`、会话数据库或 `.deep-agent/` 目录。
