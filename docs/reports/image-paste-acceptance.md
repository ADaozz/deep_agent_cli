# 真实图片复制 / 粘贴补测

日期：2026-10-09。用户在 v0.1.4 修复验收后追加，要求使用真实图片。此次只新增测试、图片 fixture 和证据，**没有修改生产实现**。

**结果：真实图片路径粘贴、附件保存、API 传输和真实 Qwen 识图 PASS；原生 Windows/WSL 位图复制→Ctrl+V 为 BLOCKED。** 当前 Debian 环境不是 WSL，现有 `ClipboardAdapter` 的图片读取仅实现 Windows PowerShell 通道。没有把图片路径粘贴或适配器替身算成原生系统剪贴板通过。

## 测试图片

使用此前真实浏览器生成的网页截图，复制为 [browser-page.png](../../tests/fixtures/browser-page.png)。不是 PNG 签名加假数据：验证了所有 PNG chunk 的 CRC、IHDR 和 IDAT 解压后的真实像素行长度。

- 尺寸：1280×720，8-bit RGB。
- 文件大小：7111 字节。
- SHA256：`87c970316696f626293222a3f50a5fef5fee3f4a0d5058af217fc729c5097596`。
- 图中左上角黑色文字：`persistent`。真实模型测试的提示未提供这个答案或文件内容，也没有允许工具读取文件。

## 结果和证据

| ID | 状态 | 实际检查 | 证据 |
|---|---|---|---|
| IMG-01 | PASS | 标准库检查真实 PNG CRC、1280×720 IHDR 和解压像素；AttachmentStore 保存后字节完全一致 | `test_fixture_is_decodable_real_png`、`test_real_png_storage_roundtrip`；[环境/图片 metadata](evidence/image-environment.json) |
| IMG-02 | PASS | 真 PTY 发送 bracketed-paste 图片文件路径；自动识别附件，输入行不留下路径，提交前不发模型请求；提交后待发送附件清空 | `test_real_png_pty_attach_submit_exact_wire_bytes[bracketed_path_paste]`；[终端](evidence/pty/image-test_real_png_pty_attach_submi0.txt) |
| IMG-03 | PASS | 真 PTY 的 `/image <path>` 使用同一真实 PNG；实际 HTTP 模型请求内有且仅有一个 image_url，base64 解码后逐字节等于原图 | 同测试 `[image_command]`；[终端](evidence/pty/image-test_real_png_pty_attach_submi1.txt) |
| IMG-04 | PASS | 真 PNG + 真 PTY Ctrl+V + bitmap 分支；系统剪贴板适配器使用明确的测试替身，export 一次、临时文件被删除，已保存的附件与 HTTP wire 图片仍完整 | 同测试 `[bitmap_clipboard_adapter_double]`；[终端](evidence/pty/image-test_real_png_pty_attach_submi2.txt)。这是应用分支测试，不是原生 OS 剪贴板验收 |
| IMG-05 | PASS | 非 WSL 真 PTY Ctrl+V 调用原生 ClipboardAdapter；明确提示 WSL 限制，保留草稿文本和光标，无假附件、不提交 API 请求 | `test_real_pty_native_clipboard_unavailable_keeps_draft`；[终端](evidence/pty/image-test_real_pty_native_clipboard0.txt) |
| IMG-06 | PASS | 真 PTY 图片路径粘贴→现有附件机制→真实 TokenPlan Qwen auto；HTTP 200，请求包含原图7111字节、SHA256一致；模型答 `persistent`，0次工具调用，成功后待发送附件为0 | [live-image.json](evidence/live-image.json)、[真实模型终端](evidence/live-image-terminal.txt)、[运行脚本](evidence/live-image-script.py) |
| IMG-07 | BLOCKED | 原生 Windows/WSL 剪贴板：在图片查看器复制位图→真实 powershell.exe inspect/export→Ctrl+V / Alt+V→提交。当前非WSL，无powershell.exe/wslpath，无法实际执行 | [image-environment.json](evidence/image-environment.json)；实现 `agent/cli/clipboard.py`；README 已声明平台限制 |

本地 HTTP server 的固定回复仅用于验证 UI 和实际 API 编码字节，不作为模型识图证据；IMG-06 单独请求了真实外部模型。真实模型沿用临时本机网关，通过环境代理访问同一 TokenPlan 端点，没有修改原客户端、TLS 验证或沙箱权限。网关已退出，Key 仅经 stdin 和进程环境提供，没有写入仓库、配置或证据。请求记录只保存图片大小、摘要和 HTTP 状态，不倾倒 base64。

## Agent 原生多模态调用链核查

用户特别确认：不得先调用独立识图服务，再把识别文字交给 Agent。IMG-06 使用的就是 Agent 自身的多模态请求链：

1. 真实 PTY 中粘贴图片路径，由 `CliApplication` 保存图片附件。
2. Enter 提交调用 `AgentRunner.invoke_with_attachment_refs()`，图片以附件引用随同用户消息进入原 Agent graph。
3. 现有 `AttachmentMaterializationMiddleware` 将引用还原为图片内容块；Agent 配置的 `openai-compatible` 模型客户端发出包含原 PNG 的同一模型请求。
4. TokenPlan 模型返回的流通过该客户端、Runner 和 TUI 的正常响应流程显示为 `persistent`。

临时本机网关只为了适配云环境出网代理，原样转发 Agent 的 HTTP 请求 body 和模型响应流；它只记录图片字节数/哈希，不识图、不做 OCR、不生成识别文字，也不把文字重新提交给 Agent。

核查依据：真实测试脚本创建 `AgentRunner(settings=Settings.load(), sandbox_config=config)`；网关转发调用使用 `content=body` 原始请求；[wire metadata](evidence/live-image.json) 中只有1次模型请求，含1个与原图一致的 image_url，HTTP200，提交前没有请求，工具调用为0。用户提示没有包含 `persistent`。本次没有新增独立识图服务、OCR工具或图像预处理实现。

## 自动回归

新增文件：[test_image_paste_real.py](../../tests/test_image_paste_real.py)，共 6 个测试用例（含参数化）。

实际运行：

```bash
TERM=xterm-256color .venv/bin/python -m pytest \
  tests/test_image_paste_real.py tests/test_attachments.py tests/test_cli.py \
  -q --basetemp=/tmp/deep-agent-image-tests
```

**217 passed，0 failed，0 skipped，27.20s**：[image-regression.txt](evidence/image-regression.txt)。追加图片测试后的完整套件已再次运行，结果见 [主验收报告](v0.1.4-acceptance.md) 和 [pytest.txt](evidence/pytest.txt)。没有新增第三方依赖；按用户后续授权准备中文提交和 PR。

未发现需要修改生产代码的问题。原生位图剪贴板的 Windows 图片复制、文件复制、Windows→WSL 路径转换、实际 PowerShell 导出与快捷键组合仍须在真实 WSL 机器验收；不可由当前替身测试推断 PASS。
