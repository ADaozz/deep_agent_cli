# 优化前后对比（2026-11-06）

同一台机器空闲时依次运行 `python examples/benchmark.py cli --blocks N --repeat 100`。改动前取 `c3cb3ec` 的干净 worktree，改动后为本分支。单位毫秒，P50 / P95。

## 保留的改动：转录单元增量复用

`TranscriptRenderer` 为每种工具展开模式保留上一帧的 `TranscriptUnitMemo`，只从第一个新增、修改（revision 变化）或仍在运行的块开始重建单元，其余单元对象原样复用；文档缓存改为按单元身份比较。渲染缓存、指纹与分组规则不变。

| 块数 | 改动前一帧 | 改动后一帧 | 峰值内存 MiB（前 / 后） |
|---|---|---|---|
| 500 | 1.88 / 3.35 | 1.09 / 2.23 | 11.1 / 11.1 |
| 2000 | 7.45 / 151.04 | 2.33 / 4.77 | 43.2 / 43.3 |
| 5000 | 30.47 / 219.60 | 10.25 / 18.18 | 108.1 / 108.0 |

其余 TUI 指标（流式增量、工具查找、`run_completed`、底部状态栏）前后差异在测量噪声内。Agent 外围与沙箱路径没有代码改动，数字见 [性能基线](perf-baseline.md)。

## 验证

- Python 3.12 与 3.13 本地完整回归：均为 1789 passed、6 skipped，含 `sandbox` 与 `pty` 标记。
- 新增 `test_incremental_units_match_a_fresh_render_through_mutations`：追加、合并分组、修改早期块、替换整个 `blocks` 列表、切换展开模式、删除尾部块和改变宽度后，增量结果与全新渲染器逐字一致。
- 新增 `test_unchanged_transcript_prefix_reuses_unit_objects` 与 `test_run_metrics_splits_wall_time_without_double_counting_parallel_tools`。

本地运行注意：shell 若带 `TERM=dumb` 与 `NO_COLOR=1`，`test_execute_output_tail_counts_wrapped_terminal_rows` 的 6 个宽度 100 用例会失败，与本改动无关；去掉这两个变量后通过。
