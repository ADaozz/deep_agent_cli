# DeepAgentCLI Web 交付验收报告

## 基线与交付范围

- 本地及只读核对的远程 main：`f9d1e028bc92ee18f4efd462fbd2c50191c78087`；版本仍0.1.3。
- 实施分支：`feat/stitch-web-workspace`。开始时工作区干净；未覆盖用户文件。上传包完整解压到指定 `.codex-input`，MANIFEST全部校验通过，原件未修改、未提交。
- M0→M4依次完成。源码是可启动的 Vue3/TypeScript 前端、独立同源 BFF 和两个认证只读 API，不是静态原型或仅建议。
- 本地中文功能提交：`a99c407`（网关/只读API）、`07e55b4`（Web界面/回归）；验收报告和证据另以docs提交保存。
- 未推送、未合并、未创建PR或Release。远程CI未运行（遵守不推送限制）。下述测试为本机实际执行。

## 里程碑与变更

| 里程碑 | 修改文件/目录 | 对应 API | 实际阶段验证 |
|---|---|---|---|
| M0 | 本实施文档、web测试/工具配置、.gitignore | 核对原14类操作与SSE字段 | 原server/human基线52项；前端骨架3项 |
| M1 | web/src/components、assets、api、state | 原projects/sessions/run | 类型/Lint/Build；13单测、1浏览器；两尺寸空态与窄屏 |
| M2 | agent/web_gateway、tests/test_web_gateway.py、tests/test_web_human_input.py；审批/动态表单 | 显式/api/agent→/v1映射 | Python20、前端24、浏览器2；真实Runner/BFF/bwrap整批审批 |
| M3 | agent/server/{app,runtime,store}.py；tests/test_server_frontend_queries.py；前端history/discovery；docs/server.md、frontend-http-api.md | 新增GET session runs、project active-run | Python49；浏览器3，含新客户端发现和刷新 |
| M4 | 前端状态/安全/分页日志/响应式细化，web/e2e，docs/web.md、本报告与frontend-evidence | 全链路 | 最终Python928、单测35、浏览器14；类型/Lint/构建通过 |

使用轻量composable，无Pinia/UI库；运行依赖只有Vue。Python复用已有server extra，没有增加项目Python依赖。工具层增加Vue/Vite/TS、Vitest、ESLint、Playwright及锁文件。

## M0–M4 原始执行与证据（追加结果见文末）

| 检查 | 结果 | 命令/证据 |
|---|---|---|
| 全量Python | PASS：928 passed，0 failed，0 skipped，141.23s | [python.txt](frontend-evidence/python.txt) |
| npm ci | PASS，锁文件安装 | [npm-ci.txt](frontend-evidence/npm-ci.txt) |
| TypeCheck | PASS | [typecheck.txt](frontend-evidence/typecheck.txt) |
| Lint | PASS，0错误/警告 | [lint.txt](frontend-evidence/lint.txt) |
| Vitest | PASS：35项、6文件 | [web-unit.txt](frontend-evidence/web-unit.txt) |
| Web Build | PASS | [build.txt](frontend-evidence/build.txt) |
| Playwright | PASS：14项，50.3s | [browser.txt](frontend-evidence/browser.txt) |
| Python wheel | PASS；含gateway，未打包测试控制器或web/dist | [wheel.txt](frontend-evidence/wheel.txt)；安装后显式WEB_DIST_DIR |
| 安全及边界diff | PASS | [final-safety.json](frontend-evidence/final-safety.json) |
| PNG实像素与CSS | PASS：8张桌面PNG的主区黑/两侧灰；按钮透明、布局/抽屉断言通过 | [visual-audit.json](frontend-evidence/visual-audit.json) |

实际Python命令（为原有CLI沙箱浏览器测试临时准备CLI，不安装Node/Chrome/Chromium/Xvfb）：

```bash
TERM=xterm-256color PATH=/tmp/deep-agent-cli-tools/bin:$PATH \
 PLAYWRIGHT_SANDBOX_EXECUTABLE=/usr/lib/chromium/chromium REQUIRE_BWRAP_TEST=1 \
 .venv/bin/python -m pytest -q -ra
```

Web命令：`npm ci; npm run typecheck; npm run lint; npm run test; npm run build`，再以已有系统Chromium执行 `PLAYWRIGHT_EXECUTABLE_PATH=/usr/lib/chromium/chromium npm run test:e2e`。浏览器测试真实启动Vite、网关、HTTP服务、AgentRunner/LangGraph、SQLite和Bubblewrap；模型为脚本模型，所有工作区/凭证临时生成。

## 完整验收矩阵

下面PASS仅指列出的自动化层级。真实外部LLM、原生OS输入法/屏幕阅读器等单独列为NOT RUN，不把脚本模型当真实LLM验收。

| ID | 状态 | 实际证据与覆盖 |
|---|---|---|
| UI-01 | PASS | workspace浏览器夹具空项目会话态；真实空SQLite第一次创建 |
| UI-02 | PASS | workspace.spec单测旧Run事件不污染新视图；实际跨浏览器/项目切换与发现 |
| UI-03 | PASS | components.spec的Enter、Shift+Enter、合成事件；原生IME单独未验收 |
| UI-04 | PASS | 运行时禁用composer、不重POST；真实同项目409 |
| UI-05 | PASS | 1600×1280/1440×900截图；390×844两个真抽屉、焦点循环/Esc与恢复 |
| UI-06 | PASS | 生产bundle无测试fixture、Qwen Flash等；API失败不回退demo |
| SSE-01 | PASS | reducer/stream.spec独立游标、跳号、重复id、跨Run |
| SSE-02 | PASS | 一字节UTF8分块、多行data、keepalive、原提供replay fixture |
| SSE-03 | PASS | 真实杀死BFF→续接正游标；GET对账、无额外POST/取消；idle读超时单测 |
| SSE-04 | PASS | authoritative final替换、旧replay不重开审批；实际刷新后单份正文 |
| SSE-05 | PASS | 页面关闭后真实任务仍waiting；Python实时代理disconnect释放上游 |
| SSE-06 | PASS | agent_event不追加正文；tool_call_id归并和重复日志去重 |
| APP-01 | PASS | 单工具真实审批、网络参数缺失明确未提供；字段映射单测 |
| APP-02 | PASS | 两工具各自明确决定，仅一批POST；不会继承Object原型决策 |
| APP-03 | PASS | 真实旧id409、重新GET并清空；busy防重复提交 |
| APP-04 | PASS | 同Run第二批审批按新id/新列表未选状态出现 |
| HUM-01 | PASS | 五类动态表单；Other原值编码真实Runner round-trip；unknown禁用 |
| HUM-02 | PASS | 空/null/空白/列表校验；真实422保留填写值；boolean false提交成功 |
| HUM-03 | PASS | 受控注入Runner异步ValueError→interaction_rejected→新waiting→重新回答 |
| RUN-01 | PASS | 真实HTTP202→SSE→终态和最终回答；不是仅看模型文本 |
| RUN-02 | PASS | 等实际stdout started，取消HTTP返回cancelling，再验证cancelled |
| RUN-03 | PASS | 杀死真实服务，旧Run interrupted；确认风险后新Run恢复原checkpoint提问 |
| RUN-04 | PASS | 同项目真实409、project占用与客户端禁用、无假队列 |
| RUN-05 | PASS | 两项目同时waiting，第三个真实429及可辨识反馈 |
| API-01 | PASS | 123条同时间键集翻页无丢重，scope游标/边界/旧库；summary无output |
| API-02 | PASS | running/waiting/paused/config边界/cancelling；finish锁竞争；终态null、清理失败blocked |
| API-03 | PASS | 原服务HTTP认证/格式校验测试与BFF401/404/409/422/429/503码保留；浏览器实测409/422/429 |
| SEC-01 | PASS | 真实随机测试令牌扫描浏览器请求/HTML/storage/bundle零命中；无Authorization；转义令牌错误体也隐藏 |
| SEC-02 | PASS | Host/Origin/JSON/白名单/路径/请求大小；已有父目录私有文件不可读；不支持开放代理 |
| SEC-03 | PASS | 真实TCP首帧未等任务完成、keepalive透传、实际15秒心跳、Header cursor优先 |
| SEC-04 | PASS | 原原子终态发布/交互先提交后唤醒并发回归保持；写路径AST与基线完全相同 |

长日志附加PASS：单窗约2万UTF16单位、早期/后续窗口可翻页、来源完整保留、Unicode边界不切坏。前端内存与服务SQLite仍可能随大量输出增长，不宣称解决 OUTPUT-PERSIST-01。

## 截图与视觉差异

| 状态/来源 | 实际截图 |
|---|---|
| 空态（测试级API夹具） | [1600×1280](frontend-evidence/m1-empty-1600.png)、[1440×900](frontend-evidence/m1-empty-1440.png) |
| 两工具审批（真实受控链路） | [approval-multiple-1600.png](frontend-evidence/approval-multiple-1600.png) |
| 单工具审批（真实受控链路） | [approval-single-1600.png](frontend-evidence/approval-single-1600.png) |
| 五类提问 | [human-1440.png](frontend-evidence/human-1440.png) |
| 真正前台进程运行/取消前 | [running-before-cancel.png](frontend-evidence/running-before-cancel.png) |
| 完成 | [completed-1600.png](frontend-evidence/completed-1600.png) |
| 脚本模型明确失败 | [failed-1440.png](frontend-evidence/failed-1440.png) |

保留原两段CSS优先级、黑色主区、两侧#222224、218/flexible/252列宽、54px顶栏、字体/间距、深灰审批嵌套层与透明19px向上箭头。所有颜色检查同时读取实际PNG像素，不只DOM/title。

**不宣称与原PNG逐像素一致。** 原1600×1280 PNG的侧栏约272px、顶栏约67px，而同一原稿CSS明确218px/54px，约有1.25比例差异；原采集缩放/DPR未提供，无法确定原因。本轮按plans/07明确的CSS值和实际CSS视口验收，不擅自整体放大。内容必然不同：会话无标题使用ID、模型/分支隐藏、虚构Diff统计移除，参数来自真实调用；批量审批需独立选择后唯一提交，多了一行任务历史与稳定的取消/恢复控件。宽屏修正原稿消息与composer不对齐的内部padding，保留850px内容上限。

窄屏抽屉不是原稿的toast，具备真正显示、遮罩、aria-modal、键盘焦点和Esc退出；审批卡始终在任务流内，无额外审批浮层。精确的参考截图采集比例与原生屏幕阅读器仍待独立确认。

## 实际失败的复查与修复

- M2首次浏览器联调发现原始对象绕过Vue响应式setter，SSE在状态内变化但画面不更新。改为读取响应式字典的代理对象；新增watchEffect/跨Run单测与真实联调通过。
- M4初次断线断言混入历史Run请求/时点早于SSE游标，先等待真实工具事件、按当前Run过滤；开发代理传播aborted/close，恢复用例通过。
- slow测试命令使用沙箱不存在的python名，实际工具127导致任务提前结束、取消409。改用已有/usr/bin/python3，并等待真实stdout started；不以模型slow done文字判成功。取消控件也置于标题稳定位置。
- 旧的非隔离wheel构建环境缺hatchling，按项目原build-system隔离构建后成功，没有新增项目依赖。
- BFF配置规范化、转义JSON凭据隐藏、静态build目录校验、原型名称tool ID等检查均补测试后通过。
- 最终diff无代码尾随空格；原replay.sse保留末帧必需的空行分隔符，Git默认blank-at-eof检查对此有一条预期提示，不删除协议分隔符。

## 未运行、协议缺口与剩余风险

| 项目 | 状态/说明 |
|---|---|
| 外部真实LLM真机验收 | NOT RUN：不读取用户/生产模型配置或钥匙；本轮受控脚本模型。不能等同真实Qwen验收 |
| 原生IME/屏幕阅读器、有头X11 | NOT RUN：组合事件/键盘/浏览器PNG已测；无相应原生环境证据 |
| 远程CI、PR、推送/合并 | NOT RUN：用户明确禁止自动推送/合并；仅本地构建与测试 |
| 历史原始指令/会话标题 | KNOWN LIMITATION：服务未存储，不能凭输出推断；不新增敏感文本字段 |
| Model/Branch/完整GitDiff/测试统计/二进制资源/上传 | 无对应数据，隐藏或空态；文件工具变更记录现已实现，见追加验收 |
| Human-input类型/选项强验证 | KNOWN LIMITATION：后端仅required空值校验，前端体验规则不是权限防护 |
| 公网/多用户身份与项目ACL | 不支持；loopback同源限制不等于多用户认证体系 |
| Unix socket间接联网 | KNOWN LIMITATION，不算PASS，也未作为沙箱逃逸修复 |
| 长输出持久化与内存 | OUTPUT-PERSIST-01未治理；DOM窗口限制不降低已持久化数据量或全部来源缓存 |
| 精确视觉采集比例 | 原PNG与CSS几何不一致，如需逐像素匹配需原始缩放/DPR；已明确差异 |

是否可供本地PR审阅：**是**，核心自动化和受控HTTP链路通过；外部LLM/原生环境与部署安全限制如上，不包装为全环境PASS。没有自动创建或发布PR。独立启动命令见 [web.md](web.md)。

## UI 收尾：图标与紧凑交互框

补充信息标题左侧复用alert图标；审批/补充信息卡统一限制760px宽度，内距22→18px，收紧工具框、表单间距、按钮与图标尺寸。TypeCheck/Lint/Build通过，前端35项通过；针对真实批量审批、Human-input及截图流程重新执行2项浏览器测试，2 passed（13.1s）。本次未改Python代码，未重复执行全量Python或其余12项浏览器测试。上方批量/单工具/补充信息/完成/失败PNG及哈希更新为本轮截图。

## 追加功能：任务产出栏的代码变更

用户明确要求增加代码变更后，实现 `CodeChangesPanel.vue` 和 `state/fileChanges.ts`，在原reducer保留已有 `tool_completed.artifact`，没有新增API或读取宿主Git。只展示成功文件工具操作；每个工具调用独立记录，同路径多次操作不覆盖历史。`write_file`的新建/覆盖标签来自真实artifact.operation，未知时明确标记；编辑展示真实old/new字符串、replace_all范围；覆盖/删除没有旧内容，明确说明。失败/拒绝/未完成调用和execute结果不计入。长内容复用TextWindow窗口，路径和代码按文本转义。

| ID | 状态 | 覆盖与证据 |
|---|---|---|
| ART-01 | PASS | 单测8项：SSE分类与重复去重、失败/拒绝/运行中排除、执行输出不推断、新建/覆盖/未知、空编辑片段、路径缺失、XSS、跨Run清空 |
| ART-02 | PASS | 真Runner/沙箱依次执行新建、编辑、覆盖、失败编辑、execute及删除；右栏4次成功操作、1个路径，失败不计入 |
| ART-03 | PASS | 编辑前后before/after、写入内容、删除提示与真实结果一致；刷新SSE回放仍4条，切换空项目清空 |
| ART-04 | PASS | 右栏命令输出折叠区实际展开，断言stdout/stderr标记及cat读到真实覆盖后文件。现backend合并两流并以stream=stdout转发，不伪称独立stderr |
| ART-05 | PASS | 右栏测试结果与预览/截图均实际展开，验证协议缺口空态；不声称实现对应业务接口 |
| ART-06 | PASS | TypeCheck/Lint/Build；前端43项、7文件；完整浏览器15项（53.3s）通过 |
| ART-07 | PASS | 增补截图滚动定位及390×844真实文件记录抽屉、展开片段、Esc和焦点恢复后，定向1项再次通过（9.3s），[日志](frontend-evidence/file-changes-targeted-browser.txt) |

证据：[完整浏览器日志](frontend-evidence/file-changes-browser.txt)、[本轮检查汇总](frontend-evidence/file-changes-checks.json)、[代码变更截图](frontend-evidence/code-changes-1600.png)。首次联调发现测试locator同时匹配顶部按钮和aside，改为complementary角色；另一次误假设stdout/stderr分流，与实际backend合流契约不符，按真实事件修正断言；不是用模型最终回答判断文件成功。

限制：这是本Run文件工具操作记录和片段，不是完整Git Diff，也不是最终工作区快照；shell/外部进程改动、整文件旧快照/实际行号均不可由当前事件确认。没有新增后端写入路径、依赖、配置或权限机制，没有修改Runner/沙箱。生产Python未改，本轮未重复全量Python；前次928项证据保持。
