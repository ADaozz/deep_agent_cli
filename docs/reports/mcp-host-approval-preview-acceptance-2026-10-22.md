# MCP host approval preview acceptance — 2026-10-22

Historical snapshot: this report records PR #22 at commit `55af2b1`. The subsequent explicit-secret simplification removes credential-key/text guessing and UI redaction; screenshots below document the earlier behavior.

Implementation baseline: latest `origin/main`, `e73d616` (`fix: unify async MCP safeguards and stabilize remote tool aliases (#21)`), confirmed before creating `fix/mcp-host-approval-preview`.

## Changes

- `agent/cli/interactions.py`: bounded, indented MCP argument previews, recursive credential-key redaction, then-existing MCP configuration text-redaction rules, ANSI/control filtering, and independent arguments for each tool call. `mcp_confirm` renders plain Rich `Text`; other non-approval interactions retain Markdown.
- `agent/cli/rendering.py`: collapsed and Ctrl+O expanded external MCP cards use the same safe argument renderer, including paths that previously became generic tool targets.
- `tests/test_mcp_cli.py`: seven additional tests cover structure, multiple calls, redaction, bearer strings, bounded expansion, controls, cycles/depth, and multiline confirmation.
- `tests/test_tui_pty.py`: five additional actual PTY cases cover approval/rejection in ASK and ALLOW, safe Ctrl+O details, unchanged raw execution arguments, and narrow-screen add/cancel/connect.

All argument processing is display-only. Approval decisions, toolCallId mapping, checkpoint data, and MCP requests retain their original parameters. No MCPManager, SyncAsyncBridge, Runner, sandbox, native permission, or CI workflow changes.

## Preview behavior and limits

The summary allows 8 content lines, 700 characters, nesting depth 4, and 100 characters per string. Expanded details allow 80 content lines, 8,000 characters, nesting depth 8, and 1,200 characters per string. An additional omission notice identifies truncated content. Existing terminal wrapping and transcript scrolling remain available.

For a pending approval with omitted content: Esc dismisses the card without deciding, Ctrl+O expands safe transcript details, and F2 reopens the same approval. Expanded details are still bounded and redacted.

Normalized credential keys include `api_key`/`apiKey`, `token`, `access_token`, `refresh_token`, `authorization`, `password`, `secret`, `client_secret`, `private_key`, `cookie`, and `credential` (including relevant plural/header variants). Ordinary `nonce`, `path`, and `token_count` remain visible. String values additionally use the existing MCP configuration text-redaction rules. This does not claim detection of arbitrary secrets in unknown formats or innocuously named fields.

## Automated verification

| Command | Result |
| --- | --- |
| `pytest -q tests/test_mcp_*.py` | 87 passed; 2 existing FastMCP in-memory OAuth token-store warnings |
| `pytest -q tests/test_cli.py tests/test_tui_pty.py` | 319 passed, 1 skipped |
| `TERM=xterm-256color pytest -q` | 1440 passed, 5 skipped, 2 existing warnings (337.79s) |
| `python examples/sandbox_probe.py --require-sandbox` | supported; actual isolation confirmed |
| `python -m build` | wheel and sdist built |
| `python -m twine check dist/*` | both distributions passed |

All local commands used the repository Python 3.12 virtual environment.

Coverage U01–U06 and U09–U11 includes ordinary/nested/array/scalar values, key and bearer redaction, per-call isolation, oversized/deep/cyclic inputs, terminal controls, multiline confirmation, unchanged other-panel Markdown, and safe expanded cards. Actual PTY cases cover U07, U08, U11, and U12 through the production approval handlers and a real FastMCP stdio process. Approval output and the complete terminal byte stream are checked for both fake credential literals.

## Real TUI verification

Captured from a running xterm on Xvfb, using the production CliApplication, real Bubblewrap restricted backend, scripted model, MCPManager, official MCP adapter, bridge, and a real local FastMCP stdio service. These are actual terminal screenshots, not reconstructed UI mockups. Only fake test credentials were used.

The earlier Grok Bot script was unavailable in this workspace; the equivalent reproducible `write_marker(nonce, path, auth)` service is included in the existing PTY test module. No real model provider or API key was used.

| Scenario | Evidence |
| --- | --- |
| R01: nonce visible before approval; approval writes exactly `TUI_E2E_APPROVE_002` outside the sandbox workspace | [Approval card](evidence/mcp-host-approval-preview/01-host-approval.png), [completed host call](evidence/mcp-host-approval-preview/03-approved-host-call.png) |
| R02: default Reject leaves both host marker and argument-record files absent, in ASK and ALLOW | Four parameterized PTY approval/rejection cases |
| R03: nested fake API key and Authorization hidden in card and Ctrl+O; original credential arguments arrive unchanged after approval | [Expanded safe details](evidence/mcp-host-approval-preview/02-safe-expanded-details.png), PTY raw-argument equality assertions |
| R04: name, transport, and complete quoted command retain separate lines and wrap at 60 columns; Cancel creates no configuration; Add saves and connects | [Narrow confirmation](evidence/mcp-host-approval-preview/04-mcp-add-narrow-confirmation.png), [connected server](evidence/mcp-host-approval-preview/05-mcp-add-connected.png) |

## Remaining limits

- Known credential-key/text patterns are protected; arbitrary secrets under unrecognized keys cannot be guaranteed detectable.
- Both preview modes deliberately omit excessive content and identify the omission. They are not a raw-argument export.
- This acceptance uses a scripted model with a real MCP service and TUI; it does not repeat live-provider inference or external network service testing, which is outside this display-only change.
- No additional MCP features or refactors were added. The PR remains open for review before merge.
