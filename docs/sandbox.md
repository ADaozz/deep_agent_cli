# 沙箱与权限

本项目有两层相互独立的控制：

- **沙箱**（Bubblewrap）决定 `execute` 运行的命令能访问什么。
- **权限模式**（`ask` / `allow`）决定工具调用前是否需要你确认。

## 执行模式

启动时程序用 Bubblewrap 运行一次 `true` 作为预检，结果决定执行模式：

| 模式 | 条件 | 命令在哪里运行 |
|---|---|---|
| `SANDBOXED` | 找到 `bwrap` 且预检成功 | Bubblewrap 沙箱 |
| `UNSANDBOXED` | 预检失败，且配置了 `sandbox.allow_unsandboxed: true` 或在启动提示中输入 `UNSANDBOXED` | 宿主机，当前用户权限 |
| `CUSTOM` | 作为库使用时显式传入 `backend` | 由该 backend 决定 |

预检失败且没有上述授权时，CLI 拒绝启动。非交互环境下不会出现 `UNSANDBOXED` 提示，直接失败。`/status` 显示当前模式。

## 沙箱内可见的内容

每条 `execute` 命令在一个新的 Bubblewrap 实例中运行：

| 路径 | 内容 |
|---|---|
| `/workspace` | 工作区，读写 |
| `/skills` | `~/.deep-agent/skills/`，只读（目录存在时） |
| `/usr`、`/bin`、`/lib`、`/lib64` | 宿主机对应目录，只读 |
| `/etc` | 只有 `passwd`、`group`、`nsswitch.conf`、`ld.so.cache`、`localtime`、`alternatives`；开放网络时另加 `hosts`、`resolv.conf`、`ssl` |
| `/tmp`、`/home` | 空的 tmpfs；`HOME=/home/agent` |
| `/proc`、`/dev` | 沙箱自己的实例 |

其他宿主路径（家目录其余部分、`/opt`、`/var` 等）不可见。安装在 `~/.local`、`~/.nvm`、`~/.cargo`、`/opt` 等位置的工具链在沙箱中找不到，需要通过 `sandbox.extra_read_only_mounts` 挂载，并在 `env_set` 中调整 `PATH`。

额外挂载的限制：`source` 必须存在；`destination` 必须是沙箱内的绝对路径，不能是 `/workspace`、`/tmp`、`/home/agent`，也不能与 `/skills` 重叠。

其他隔离设置：

- 独立的 PID、IPC、UTS 命名空间；`deep-agent` 退出时沙箱进程随之结束。
- 环境变量先清空，再设置 `PATH=/usr/local/bin:/usr/bin:/bin`、`HOME`、`PWD`、`TMPDIR`，并传入宿主的 `LANG`、`TERM`、`COLORTERM`、`NO_COLOR`、`TZ` 和 `LC_*`。其他变量需要列入 `sandbox.env_allowlist` 或写在 `sandbox.env_set` 中。
- 不限制 CPU、内存、磁盘或进程数。

## 网络

- 默认使用 `--unshare-net`，命令没有网络。
- 模型在调用 `execute` 时传 `network=true`，该条命令共享宿主网络命名空间，能访问互联网、`localhost` 和局域网。`ask` 模式下这条命令仍需审批，审批界面会显示该参数。
- `allow` 模式下，每条 `execute` 都共享宿主网络，单次调用无法关闭。
- `web_search` 在宿主进程中运行，与沙箱网络无关。

## 文件工具

`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`delete` 不经过 Bubblewrap，由 `deep-agent` 进程直接读写宿主文件。它们只接受 `/workspace/...` 和 `/skills/...` 路径：前者映射到工作区，后者只读；其他路径返回权限错误。单个文件读写上限 10 MB。

工作区内的所有内容对 Agent 都可读写，包括 `.git/`、`.env` 等。需要保护的文件不要放在工作区里。

## 权限模式

| 模式 | 需要确认的工具 | 可用条件 |
|---|---|---|
| `ask`（默认） | `execute`、`write_file`、`edit_file`、`delete` | 所有执行模式 |
| `allow` | 无；所有工具自动批准，所有 `execute` 开放宿主网络 | 仅 `SANDBOXED`；开启时需输入 `ALLOW` |

- `/permission ask|allow` 切换。空闲时立即生效；有任务运行或有待处理的审批、暂停时，在下一次模型调用前生效。切到 `allow` 不会自动批准已经在等待的审批。
- 会话保存其权限模式，恢复会话时沿用。
- 作为库使用时，自定义工具如果有副作用，需要通过 `create_agent(interrupt_on=...)` 显式加入审批规则；内置工具的审批规则不能被覆盖。

## 命令输出与日志

- 输出实时显示在对应的工具块中。
- 超过 `sandbox.max_output_bytes`（默认 100000 字节）时，返回给模型的结果只保留末尾部分，完整输出写入 `<工作区>/.deep-agent/logs/exec/<时间>-<id>.log`。工具结果同时给出宿主路径和 Agent 可读的 `/workspace/.deep-agent/logs/exec/...` 路径。
- 默认没有超时。设置 `sandbox.timeout_seconds` 后作为上限；超时返回退出码 124，取消返回 130。
- 父 shell 退出后，若后台进程仍持有输出管道，会继续读取到管道关闭或 100 毫秒内没有新输出为止；之后的输出不进入本次结果。

程序不会修改项目的 `.gitignore`。建议加入：

```gitignore
.deep-agent/
.deepagents/
```

`.deepagents/` 用于上下文压缩时保存被摘要掉的历史，见 [architecture.md](architecture.md#上下文压缩)。

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
