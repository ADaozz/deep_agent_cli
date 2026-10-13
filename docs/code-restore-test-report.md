# 轮次代码回退追加验收

基线：本地 `feat/stitch-web-workspace` 的 `5d4e4aa`。用户明确追加「根据对话轮次回退/撤销回退代码修改」，因此扩展原只读接口范围。实现后没有推送、合并或发布，开始时工作区干净。功能本地中文提交：`34e7238`（服务快照/回退）、`f77db8a`（Web轮次交互）；本报告及证据另以docs提交保存。

## 实际修改

| 部分 | 文件 | 内容 |
|---|---|---|
| 快照/恢复 | agent/server/code_history.py | 运行前后净变化、私有去重对象、版本、全路径预检、目录FD/NOFOLLOW、失败日志恢复与项目锁 |
| 服务边界 | agent/server/runtime.py、app.py | Worker开始/清理后记录；新增GET code-checkpoint与POST code-restore；复用项目互斥，阻止待审批/活动期间回退 |
| BFF | agent/web_gateway/app.py | 只增加上述两个显式映射，继续服务端令牌注入和同源JSON限制 |
| 界面 | CodeRestoreControls.vue、InspectorPanel.vue、WorkspaceView.vue、api/types.ts、api/http.ts、state/workspace.ts、assets/workspace.css | 历史选中Run的确认式回退/撤销，冲突显示及GET对账，操作后同步项目恢复锁；初始化稳定前禁用编辑；历史选中旧Run补入列表并分页去重 |
| 回归 | tests/test_code_history.py、web/src/__tests__/codeRestore.spec.ts、workspace.spec.ts、web/e2e/code-restore.spec.ts、service_fixture.py | 安全、失败、重启和真实沙箱文件字节验证 |

## 最终结果

| 检查 | 状态 | 证据 |
|---|---|---|
| 全量pytest | PASS：946 passed，0 failed，0 skipped，139.36s | [code-restore-python.txt](frontend-evidence/code-restore-python.txt) |
| 前端单元 | PASS：51 passed，8 files | [code-restore-unit.txt](frontend-evidence/code-restore-unit.txt) |
| TypeCheck/Lint | PASS | [类型](frontend-evidence/code-restore-typecheck.txt)、[Lint](frontend-evidence/code-restore-lint.txt) |
| Web Build | PASS | [构建](frontend-evidence/code-restore-build.txt) |
| 完整浏览器 | PASS：16 passed，59.1s | [浏览器](frontend-evidence/code-restore-browser.txt) |
| 界面说明文字/最终截图 | PASS：仅澄清工具详情与快照回退范围；再次Build及定向真实回退1项通过 | [定向](frontend-evidence/code-restore-targeted.txt) |
| Python wheel | PASS，含code_history、无e2e/dist | [wheel](frontend-evidence/code-restore-wheel.txt) |
| 保护边界/diff | PASS | [核查](frontend-evidence/code-restore-safety.json) |

Python实际命令：

```bash
TERM=xterm-256color PATH=/tmp/deep-agent-cli-tools/bin:$PATH \
 PLAYWRIGHT_SANDBOX_EXECUTABLE_PATH=/usr/lib/chromium/chromium \
 PLAYWRIGHT_SANDBOX_EXECUTABLE=/usr/lib/chromium/chromium REQUIRE_BWRAP_TEST=1 \
 .venv/bin/python -m pytest -q -ra
```

Web按序执行 `npm run typecheck`、`npm run lint`、`npm run test`、`npm run build` 和 `PLAYWRIGHT_EXECUTABLE_PATH=/usr/lib/chromium/chromium npm run test:e2e`。使用已有浏览器，不安装Node/Chrome/Xvfb。CLI辅助依赖仍在/tmp，未增加项目依赖。

## 针对性验收

| ID | 状态 | 实际验证 |
|---|---|---|
| UNDO-01 | PASS | Python新18项涵盖普通文件/二进制/权限，新建目录文件与删除恢复；保留不相关手改及Git索引 |
| UNDO-02 | PASS | 同文件两个轮次：旧轮直接undo冲突；逆序undo、正序redo，字节一致 |
| UNDO-03 | PASS | 手动修改任一路径，整组预检拒绝，其他路径不先改变 |
| UNDO-04 | PASS | 目录内新增普通文件或忽略的node_modules拒绝删除；父目录改成指向私有目录的符号链接不跟随 |
| UNDO-05 | PASS | 文件/目录类型变换可往返；变化的链接不可用；单文件/总量/条目/对象容量超限不冒称有完整快照 |
| UNDO-06 | PASS | 模拟第二文件写失败，已写文件回滚；状态/revision保留，项目未被误锁 |
| UNDO-07 | PASS | 模拟进程中途死亡，重开数据库恢复未提交操作；第三种手改状态锁定对应项目，其他项目可继续 |
| UNDO-08 | PASS | 鉴权401、extra路径字段422、无Run404；同nonce并发提交200/409；待human操作期间禁止回退 |
| UNDO-09 | PASS | 已撤销状态跨服务重启保留，可redo；老轮次无快照明确不可用；快照失败不使原任务失败 |
| UNDO-10 | PASS | 损坏对象/替换成FIFO快速503，不覆盖当前文件，不因读取FIFO挂住 |
| UNDO-11 | PASS | 前端两步确认、取消不POST、当前revision、undo→redo；409先对账不自动重POST；活动/审批/无快照禁用；切换轮次忽略旧响应 |
| UNDO-12 | PASS | 真实HTTP/BFF/AgentRunner/LangGraph/SQLite/Bubblewrap，三轮shell任务：后轮冲突→逆序回退→恢复删除文件/移除新二进制→杀死重启服务→redo恢复0/255/1字节→redo后轮 |
| UNDO-13 | PASS | 延迟历史请求时编辑器禁用，恢复完成后保持新输入；旧选中Run不在首分页时仍可选，分页去重 |

UNDO-12使用独立测试控制端口读取实际临时项目文件，断言code/deleted/binary原始值，不靠模型最终文本。模型为脚本模型；**外部真实LLM NOT RUN**。[真实界面截图](frontend-evidence/code-restore-1600.png)与[像素/哈希记录](frontend-evidence/visual-audit.json)可复核。

## 失败复查

- 新权限用例最初假设默认文件644，环境umask实际077。改为显式创建640基线，验收回退恢复该权限；不修改实现降低断言。
- 恢复锁用例最初只新建文件，undo是删除而不是进入原子替换，故故障注入未触发。补上旧文件基线后，真实进入替换故障路径并验证项目锁/跨项目隔离。
- 初次浏览器重启后断言未展开的details中的role=status，角色不可见；先展开再验证，未改生产行为掩盖失败。
- 收尾曾出现2个浏览器超时：会话恢复期间过早允许输入，随历史选择变化清空draft。以初始化禁用、两个同层组件使用独立key修复，没有加sleep或放宽30秒测试超时。新增初始化回归还发现旧selected Run缺少option，真实补入历史并分页去重。最终51/16全部通过。

## 限制与边界

完整范围、限额、私有内容存储和恢复流程见 [使用说明](code-restore.md)。仅恢复代码净变化，非Git/对话/模型checkpoint回退；快照含运行期间同时发生的写入，不是逐工具归因。旧轮次、无完整结束快照、忽略目录及不支持的特殊文件无法回退。内容对象512MiB限额不等于SQLite元数据总容量限额。

多文件不是文件系统原子事务；任意外部并发写入仍有竞态，部分崩溃点无法安全自动恢复时会保留日志并锁定，不强制覆盖。非UTF8文件名、外部真实LLM、原生屏幕阅读器未验证。原Unix socket间接联网边界仍是KNOWN LIMITATION，没有作为PASS或沙箱逃逸修复。

AgentRunner、沙箱实现/双持久生命周期、--unshare-ipc/--unshare-net、ask权限、审批respond/cancel和ServiceStore终态原子事务均未修改。服务Worker仅加两端快照；submit额外拒绝恢复未完成的项目。无新增项目依赖，官方Playwright Skill未改，无新明文生产API Key。远程CI/PR/推送/合并NOT RUN，遵守用户限制。
