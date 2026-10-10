# 统一模型 API 适配层验收

日期：2026-10-10

## 交付行为

- `ModelProfile.api` 是唯一运行时协议选择字段，允许 `responses`、`chat_completions`。默认仍为 Responses，不通过模型名或端点自动选择协议。
- 公共入口 `build_chat_model()` 显式指定 LangChain `use_responses_api`，统一 HTTP 超时、重试、同步/异步客户端和 `trust_env=False`。
- 两个协议适配器使用 `ResponsesChatOpenAI` 和 `ChatCompletionsChatOpenAI`。旧厂商 Python 类名保留为弃用别名。
- Responses 的已知 reasoning_text 事件在 SDK stream 入口规范化；LangChain 原有流式解析、工具调用拼接及异常映射继续复用。标准事件保持原样。
- Chat Completions 读取 reasoning_content 或 reasoning 字符串，统一 Thinking 输出和历史回传，避免重复合并别名字段。
- 两种协议统一在模型请求前解析附件引用。checkpoint 不保存 Base64；保留存储层校验。附件 middleware 使用能力标记，不检查厂商类。
- Chat Completions 固定请求 include_usage。拒绝该参数直接报告协议错误，不做降级重试。Responses 从完成事件读取 usage。
- TUI Context 仅使用 API 实际返回的 input_tokens。缺失或无法归属时显示 Unknown；零输入显示 0.0%。不累计调用，不使用 Tokenizer、字符估算或 Token Count API。
- usage-only 流式 chunk 立即刷新 TUI。新调用、取消/失败、模型切换、压缩完成时清理旧用量。checkpoint 中记录模型来源，恢复只读取有效上下文最后一次调用的同来源用量。
- 旧会话仍可读取；无法确认来源的历史 usage 显示 Unknown，下一次调用后恢复准确值。
- Deep Agents 自动压缩、工具定义、沙箱、审批和恢复决定未调整。新增 usage middleware 只记录模型身份。

## 配置要求

只接受 `api: responses` 或 `api: chat_completions`。`provider` 与 `stream_usage` 已直接移除，不再映射或忽略；来源级和模型级出现这些字段都会报错，不能与 api 共存。

来源级与模型级继承保留；模型级协议覆盖来源级。不自动改写用户文件。含旧字段的配置必须改用 api 并删除 stream_usage，程序不会自动迁移。当前本地配置已按用户后续明确要求完成迁移。

## 验证结果

先运行旧适配层、配置、Thinking 和附件基线：94 passed。补充标准事件不变、工具参数拼接和 usage 的旧行为测试后，适配层基线：21 passed。随后进行重构。

此前适配层重构的完整回归（移除旧配置兼容前）：

```text
.venv/bin/pytest -q
762 passed, 1 skipped in 84.06s
```

唯一跳过项为 `test_real_pty_native_clipboard_unavailable_keeps_draft`：该测试覆盖非 WSL 平台不支持剪贴板的行为，当前是 WSL，按原有测试条件跳过。

新增 SDK 内存传输测试覆盖两种 API 的同步/异步流式 Thinking、标准与非标准事件、Tool Call ID、分段 JSON 参数、图片序列化、零输入、缺失完整 usage 或缺失输入计数、include_usage 被拒绝，以及各种端点 extra_body 的标准 SDK 序列化。

SQLite 回归验证了完整调用 usage 的跨进程恢复、取消后的 Unknown，以及相同模型名但不同来源时不复用旧用量。既有沙箱、审批、会话恢复、PTY 和 Playwright 测试纳入完整回归。

真实端点最小流式冒烟（使用已有配置，不把密钥测试加入 CI）：

| 来源/模型 | API | 结果 | API input_tokens |
|---|---|---|---|
| token-plan/auto | chat_completions | 成功 | 64 |
| local/qwen-plus | responses | 成功 | 49 |

后续按用户要求移除了网关 extra_body 嵌套分支，所有地址统一使用 SDK 标准序列化。

`git diff --check` 通过。

## 显示含义

input_tokens 表示最近一次请求的输入量。生成过程中没有服务端 usage 时显示 Unknown，不推测实时 token；它也不代表下一次请求的精确上下文大小。自动压缩自身的内部估算不作为 TUI Context 数据来源。


## 后续配置更新

按用户要求直接删除 provider 映射和 stream_usage 忽略逻辑。llm 顶层、来源级、模型级出现旧字段均报错，即使同时提供有效 api。配置、模型切换、适配器回归：142 passed in 3.85s。

按用户明确授权更新 `~/.deep-agent/config.yaml`，保留其他设置和默认模型 token-plan/auto。旧字段全部移除，协议改为 api。原文件备份到 `~/.deep-agent/config.yaml.before-api-and-local-models.bak`，文件权限保持 0600。

查询 gateway 的 /v1/models，并核对其模型配置后，local 新增 qwen3.8-max、qwen3-vl-flash、qwen3.8-27b、qwen3.8-flash、qwen3.7-flash-2026-07-15。当时 local 的 7 个模型均使用 responses，配置加载和请求构造验证通过。后续迁移已将 local 替换为 bailian 官方来源，其中 qwen3-vl-flash 显式使用 chat_completions。

新模型图片能力和窗口核对了官方文档：[qwen3-vl-flash](https://help.aliyun.com/zh/model-studio/qwen3-vl-flash)、[qwen3.8-27b](https://help.aliyun.com/zh/model-studio/qwen3-8-27b)、[qwen3.7-flash](https://help.aliyun.com/zh/model-studio/qwen3-7-flash)。qwen3-vl-flash 窗口为 262144，其余新增模型配置为 1m。


## 模型接入边界验收

用户仅声明 api、端点/凭据、模型 ID 和能力，其余协议格式转换由适配层负责。新增符合这两种协议的模型应仅增加配置，非标准差异也只扩展适配层，不修改 Agent 核心。

新增回归覆盖两种 api、三种模型命名和两种端点地址，验证协议选择仅依赖 api。另用配置中新引入的未知模型，通过现有 AgentRunner 完成两种协议的流式请求，验证回答、真实 API input_tokens、图片能力声明、窗口和推理参数转换。

```text
.venv/bin/pytest -q tests/test_protocol_api.py
41 passed in 3.05s
```


## 百炼迁移与后续修复

已将本地配置 `local` 来源替换为 `bailian`，端点为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，密钥仅引用 `${ALIYUN_API_KEY}`。默认模型 `token-plan/auto` 及其他设置保持原值，迁移前备份为 `~/.deep-agent/config.yaml.before-bailian.bak`。七个模型采用直接模型 ID；`qwen3-vl-flash` 使用 Chat Completions、窗口 262144，其余六个使用 Responses、窗口 1m。Qwen3.8 三个型号声明原生强度 `[none, low, medium, xhigh]`，其余型号未声明未经确认的枚举。

已删除本地网关请求嵌套分支。不同主机、端口和路径统一使用 SDK 标准序列化。Chat Completions 保留响应思考字段名并用于下一轮请求，流式合并和序列化恢复不丢失标记；旧消息无标记时继续使用 reasoning_content。图更新缺少有效输入 Usage 时不覆盖当前流式值；新调用仍清空旧值。latest_usage 恢复期间的压缩私有接口异常返回 Unknown。Responses 结构化输出的同步、异步及原始响应 parse 共用 Usage 规范化，保留解析结果和响应头。

修复前相关基线 100 passed；新增并更新回归后：

```text
.venv/bin/pytest -q tests/test_protocol_api.py tests/test_llm.py tests/test_runner.py tests/test_stream.py
133 passed in 5.57s
```

示例配置解析与本地七个模型构造校验通过。当前环境没有 ALIYUN_API_KEY，未执行本次百炼真实端点冒烟；上文旧网关冒烟仅为历史证据，不作为当前官方端点的验证结果。

规格依据：[官方 Responses 端点、支持模型与推理强度](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)、[Qwen3-VL-Flash](https://help.aliyun.com/zh/model-studio/qwen3-vl-flash)。本地知识库核查：`raw/model-api-reference/qwen-api-reference/openai-compatible-responses/qwen-api-via-openai-responses.md`、`models/groups/qwen3-vl-flash.json` 及相应 Qwen 家族规格。


最终完整回归（首次启动测试已适配新模板的环境变量密钥）：

```text
.venv/bin/pytest -q
830 passed, 1 skipped in 86.90s
```

唯一跳过项仍为 WSL 环境下不适用的非 WSL 原生剪贴板场景。`git diff --check` 通过。


## 业务空间真实端点冒烟

收到用户提供的业务空间地址后，本地 bailian 的 base_url 更新为 `https://ws-se0hlnu75b2d90hn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，其余配置保持原值。更新前备份为 `~/.deep-agent/config.yaml.before-workspace-endpoint.bak`。密钥仅通过关闭回显的标准输入传入测试进程，并赋给该进程的 ALIYUN_API_KEY；配置、脚本和报告均未保存此次密钥，宿主环境也未持久化该变量。

针对 qwen3.8-flash 使用 low 推理强度，只发送固定连通性测试文本；没有发送仓库文件、图片、会话历史或工具数据。

| API | 流式回答 | 思考内容 | 实际 input_tokens |
|---|---|---|---|
| responses | 通过 | 收到 | 92 |
| chat_completions | 通过 | 收到 | 60 |

包含仓库图片的冒烟被自动审批拒绝，因此本次图片、工具和结构化输出仍以此前 SDK 模拟回归结果为依据，不宣称已在真实端点验证。启动 CLI 前仍需在其进程环境中设置 ALIYUN_API_KEY。
