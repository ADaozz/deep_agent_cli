# 沙箱与权限

本项目有两层相互独立的控制：

- **沙箱**（Bubblewrap）决定 `execute` 运行的命令能访问什么。
- **权限模式**（`ask` / `allow`）决定工具调用前是否需要你确认。

## 执行模式

启动时程序启动一次沙箱并运行 `true` 作为预检（随后立即销毁），结果决定执行模式：

| 模式 | 条件 | 命令在哪里运行 |
|---|---|---|
| `SANDBOXED` | 找到 `bwrap` 且预检成功 | Bubblewrap 沙箱 |
| `UNSANDBOXED` | 预检失败，且配置了 `sandbox.allow_unsandboxed: true` 或在启动提示中输入 `UNSANDBOXED` | 宿主机，当前用户权限 |
| `CUSTOM` | 作为库使用时显式传入 `backend` | 由该 backend 决定 |

预检失败且没有上述授权时，CLI 拒绝启动。非交互环境下不会出现 `UNSANDBOXED` 提示，直接失败。`/status` 显示当前模式。

## 沙箱内可见的内容

`execute` 命令运行在当前 Agent 会话的持久 Bubblewrap 沙箱中（见下文“沙箱生命周期”）：

| 路径 | 内容 |
|---|---|
| `/workspace` | 工作区，读写 |
| `/skills` | `~/.deep-agent/skills/`，只读（目录存在时） |
| `/usr`、`/bin`、`/lib`、`/lib64` | 宿主机对应目录，只读 |
| `/etc` | 只有 `passwd`、`group`、`nsswitch.conf`、`ld.so.cache`、`localtime`、`alternatives`；开放网络时另加 `hosts`、`resolv.conf`、`ssl` |
| `/tmp`、`/home` | 空的 tmpfs；`HOME=/home/agent` |
| `/proc`、`/dev` | 沙箱自己的实例 |

其他宿主路径（家目录其余部分、`/opt`、`/var` 等）不可见。安装在 `~/.local`、`~/.nvm`、`~/.cargo`、`/opt` 等位置的工具链在沙箱中找不到，需要通过 `sandbox.extra_read_only_mounts` 挂载，并在 `env_set` 中调整 `PATH`。

额外挂载的限制：`destination` 必须是沙箱内的绝对路径，不能是 `/workspace`、`/tmp`、`/home/agent`，也不能与 `/skills` 重叠。`source` 必须存在，除非该项写了 `optional: true`；可选挂载的源目录不存在时会被跳过。

其他隔离设置：

- 独立的 PID、IPC、UTS 命名空间；`deep-agent` 退出（包括被强制结束）时沙箱进程随之结束。
- 沙箱内需要 `/usr/bin/python3`（或 `/usr/local/bin/python3`、`/bin/python3`）来运行常驻 Worker；Worker 脚本只读挂载在 `/run/deep-agent/`，额外挂载不能覆盖该目录。
- 环境变量先清空，再设置 `PATH=/usr/local/bin:/usr/bin:/bin`、`HOME`、`PWD`、`TMPDIR`，并传入宿主的 `LANG`、`TERM`、`COLORTERM`、`NO_COLOR`、`TZ` 和 `LC_*`。其他变量需要列入 `sandbox.env_allowlist` 或写在 `sandbox.env_set` 中。
- 不限制 CPU、内存、磁盘或进程数。

## 沙箱生命周期

每个 Agent 会话最多有两个持久沙箱，都在第一次需要时才启动：

| 沙箱 | 何时使用 | 网络 |
|---|---|---|
| 无网沙箱 | `network=false` 的 `execute` | `--unshare-net`，只有自己的 `lo` |
| 有网沙箱 | `network=true` 或 `allow` 模式下的 `execute` | 共享宿主网络 |

- 每次 `execute` 都启动一个新的 `/bin/sh -lc`：`cd`、shell 变量等状态不保留；保留的是沙箱环境本身——`/tmp`、`/home` 中的文件和后台进程（例如 `nohup cmd > log 2>&1 &`）在后续调用中仍然存在。
- 两个沙箱只共享 `/workspace`（以及只读挂载）；`/tmp`、`/home`、PID 命名空间互相独立，交替使用不会销毁对方。
- 沙箱的主进程是一个常驻 Worker，宿主只通过它继承的管道下发命令。这个控制通道不出现在文件系统或网络中，Worker 本身设为不可 dump，沙箱内的进程不能通过 `/proc` 接管它；无网沙箱看不到有网沙箱的进程、`/tmp` 中的 socket，也连不上它的抽象 Unix socket（抽象 socket 随网络命名空间隔离）。
- 取消和超时只杀掉当前命令的进程组（包括它启动的后台进程），沙箱继续可用。
- Worker 异常退出时，正在执行的命令返回 `sandbox_lost` 错误，不会自动重试；下一次 `execute` 启动新的沙箱。
- 沙箱无法启动时，`execute` 返回错误，不会改在宿主机上运行。
- 切换会话（`/resume`、`/new`）、关闭会话或退出 CLI 时，两个沙箱及其中所有进程都被销毁。切换模型或权限模式不会重建沙箱。
- `/status` 显示两个沙箱是否在运行。

## 网络

- 默认使用 `--unshare-net`：命令及子进程不能直接访问宿主 IP 网络，但仍可在自己的网络命名空间使用回环接口。
- 模型在调用 `execute` 时传 `network=true`，该条命令在有网沙箱中运行，能访问互联网、`localhost` 和局域网。`ask` 模式下这条命令仍需审批，审批界面会显示该参数。
- `allow` 模式下，每条 `execute` 都在有网沙箱中运行，单次调用无法关闭。

`network=false` 隔离的是**直接 IP 网络访问**，不保证完全没有间接网络通信。例如：有网沙箱中的后台服务在共享 `/workspace` 下创建文件系统 Unix Socket；无网命令连接这个 Socket 发出请求；有网后台服务代为访问互联网，并把响应返回无网命令。共享文件或 FIFO 也可能构成类似通信路径。抽象 Unix Socket 与这里的工作区文件系统 Socket 不同，前者仍受网络命名空间隔离。

经其他有网进程实现的间接联网属于 **KNOWN LIMITATION**，不是沙箱逃逸。本轮不增加 Unix Socket 防护、Seccomp 或 IPC 配置开关，不改变共享工作区或双持久沙箱设计。

两个沙箱都按需启动、会话内复用；权限模式只决定下一条命令要不要审批，不决定沙箱是否销毁。

- `ask`：每条 `execute` 都要审批；`network=true` 走有网沙箱，`network=false` 走无网沙箱。
- `allow`：自动批准，且每条 `execute` 都走有网沙箱。
- 已经在跑的后台进程不受模式切换影响。需要停掉它们时，让 Agent 结束进程，或新建 / 关闭会话。
- `web_search` 在宿主进程中运行，与沙箱网络无关；`ask` 模式下每次搜索均需审批。

## 文件工具

`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`delete` 不经过 Bubblewrap，由 `deep-agent` 进程直接读写宿主文件。它们只接受 `/workspace/...` 和 `/skills/...` 路径：前者映射到工作区，后者只读；其他路径返回权限错误。单个文件读写上限 10 MB。

工作区内的所有内容对 Agent 都可读写，包括 `.git/`、`.env` 等。需要保护的文件不要放在工作区里。

## 权限模式

| 模式 | 需要确认的工具 | 可用条件 |
|---|---|---|
| `ask`（默认） | `execute`、`web_search`、`write_file`、`edit_file`、`delete` | 所有执行模式 |
| `allow` | 无；所有工具自动批准，所有 `execute` 开放宿主网络 | 仅 `SANDBOXED`；开启时需输入 `ALLOW` |

- `/permission ask|allow` 切换。空闲时立即生效；有任务运行或有待处理的审批、暂停时，在下一次模型调用前生效。切到 `allow` 不会自动批准已经在等待的审批。
- 会话保存其权限模式，恢复会话时沿用。
- 作为库使用时，自定义工具如果有副作用，需要通过 `create_agent(interrupt_on=...)` 显式加入审批规则；内置工具的审批规则不能被覆盖。

## 命令输出与日志

- 输出实时显示在对应的工具块中。
- 超过 `sandbox.max_output_bytes`（默认 100000 字节）时，返回给模型的结果只保留末尾部分，完整输出写入 `<工作区>/.deep-agent/logs/exec/<时间>-<id>.log`。工具结果同时给出宿主路径和 Agent 可读的 `/workspace/.deep-agent/logs/exec/...` 路径。
- 默认没有超时。设置 `sandbox.timeout_seconds` 后作为上限；超时返回退出码 124，取消返回 130。
- 父 shell 退出后，若后台进程仍持有输出管道，会继续读取到管道关闭或固定 100 毫秒排空窗口结束为止，窗口不会因新输出而延长；之后的输出不进入本次结果，后台进程再写这些管道会收到 `SIGPIPE`。需要长期运行的后台进程应把输出重定向到文件。

程序不会修改项目的 `.gitignore`。建议加入：

```gitignore
.deep-agent/
.deepagents/
.playwright-cli/
```

`.deepagents/` 用于上下文压缩时保存被摘要掉的历史，见 [architecture.md](architecture.md#上下文压缩)。

## Playwright 与有头 X11

浏览器应使用当前 CLI 的依赖版本安装，在宿主机手动运行 `playwright-cli install-browser --help`，再运行 `playwright-cli install-browser chromium`；不要用可能解析到不同版本的 `npx playwright install chromium`。默认 `playwright-cli open` 使用系统 Chrome；`--browser=chromium` 使用与该 CLI 匹配的缓存 Chromium。本轮核对 CLI 0.1.22 的帮助及 `install-browser chromium --dry-run`，实际下载仍需用户准备，不锁定项目版本。

Node 可以来自 `/usr/bin/node`，CLI 则可以在自定义 npm prefix 下。系统 Node 在默认 `/usr` 挂载中；额外 prefix 需要挂载其可执行文件及 npm 包整体（包含 `lib/node_modules`），再调整 PATH。精简示例：

```yaml
sandbox:
  extra_read_only_mounts:
    - source: ~/.local  # 替换为 npm config get prefix 的实际结果
      destination: /opt/npm-prefix
  env_allowlist:
    - DISPLAY
  env_set:
    PATH: /opt/npm-prefix/bin:/usr/local/bin:/usr/bin:/bin
    PLAYWRIGHT_BROWSERS_PATH: /opt/ms-playwright
```

此示例只新增 npm prefix 挂载，浏览器缓存沿用默认模板的可写 `/opt/ms-playwright` 挂载。若只使用已安装浏览器并希望只读缓存，先从 `extra_read_write_mounts` 移除缓存项，再加入 `extra_read_only_mounts`，同一目标不能同时存在于两个列表。

合并进现有配置，不覆盖其他挂载和允许的变量；上述源目录必须存在。系统 Chrome 还需要 `/opt/google` 的只读挂载；位于系统目录外的 Node 本体需要独立挂载并加入 PATH。不要把个人机器的 Node 目录当成通用要求。

有头模式需要允许传入 `DISPLAY`，并确保相应 X Server 可访问。用户已测试的 X11 抽象 Socket 场景中，`network=false` 的网络命名空间无法连接宿主 X Server，当前有头流程应使用有网沙箱；ask 模式需要明确传 `network=true` 并批准。Xauthority 认证可能要求额外挂载认证文件，目前未验证；Wayland 未覆盖。

保留 `--unshare-ipc`，在专用 Xvfb 中关闭 MIT-SHM：

```bash
Xvfb :2 -screen 0 1600x900x24 -extension MIT-SHM
# 宿主启动 CLI 前：export DISPLAY=:2
```

无需增加 Chrome 启动参数。不要为了适配浏览器删除 IPC 隔离，也不要擅自修改宿主安全策略。以下是用户提供的真实机器帧缓冲截图验收结果，**不是当前环境的复测结果**：

| X Server | Browser | 成功渲染 |
|---|---|---|
| MIT-SHM 开启 | 系统 Chrome | 0/10 |
| MIT-SHM 开启 | 测试 Chromium | 8/10 |
| MIT-SHM 关闭 | 系统 Chrome | 10/10 |
| MIT-SHM 关闭 | 测试 Chromium | 10/10 |
| MIT-SHM 关闭，真实 TUI | 系统 Chrome | 1/1 |

有头验收必须有实际窗口 / 帧缓冲截图；DOM、title、IsViewable 或窗口存在只作辅助证据。当前环境缺少 Xvfb 时应标为 BLOCKED，不能把启动失败视为成功。CLI 可能生成 `.playwright-cli/`，建议用户自己加入项目 `.gitignore`；程序不自动修改它。

## 排查：Ubuntu / WSL2

先确认环境（不改动系统策略）：

```bash
command -v bwrap
sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns user.max_user_namespaces
sudo aa-status
sudo journalctl -k -g 'apparmor|DENIED|userns'
```

在源码检出中还可以运行 `python examples/sandbox_probe.py --require-sandbox`，它输出 JSON 报告，不会放宽任何策略。

常见情况：

- **未安装 bwrap**：`sudo apt install bubblewrap`。
- **`Operation not permitted` / namespace 相关错误**：bwrap 已安装，但 user namespace 被限制。Ubuntu 24.04 起默认限制非特权 user namespace，见 [Ubuntu 24.04 发行说明](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions)。
- **WSL1**：不支持。在 Windows 中运行 `wsl -l -v` 确认版本为 2，必要时 `wsl --set-version <发行版名> 2`。
- **容器内运行**：容器的 seccomp 或命名空间策略可能禁止 bwrap，需要容器管理员调整；下面的 AppArmor profile 不能解决这类问题。
- **其他预检失败**：检查 `sandbox.bwrap_path`、工作区和挂载路径的权限，以及 `/bin/sh` 是否存在。

### 为 bwrap 单独授权 user namespace

先检查发行版是否已经为 bwrap 提供 profile（`/etc/apparmor.d/`）。没有时，由管理员只为实际使用的 bwrap 可执行文件授权。假设路径是 `/usr/bin/bwrap`，创建 `/etc/apparmor.d/deep-agent-bwrap`：

```text
abi <abi/4.0>,
include <tunables/global>
profile deep-agent-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
```

```bash
sudo apparmor_parser -r /etc/apparmor.d/deep-agent-bwrap
```

注意：

- 这条规则作用于所有通过该文件启动的 bwrap，不只是 `deep-agent`。如果不希望这样，可以复制一份 bwrap 到管理员拥有的独立路径，只为该路径授权，并在 `sandbox.bwrap_path` 中指向它（CI 中的做法见 [`.github/scripts/enable-ci-bwrap.sh`](../.github/scripts/enable-ci-bwrap.sh)）。
- 已有发行版 profile 时，修改其本地 override，不要添加针对同一路径的冲突 profile。
- 不要为 Python 或整个用户目录授权，也不要全局关闭 AppArmor 或 userns 限制。
- 撤销：`sudo apparmor_parser -R /etc/apparmor.d/deep-agent-bwrap`，再删除该文件。
