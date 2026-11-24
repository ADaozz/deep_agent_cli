# 正则搜索

`grep` 和 `glob` 不在模型默认工具表中。代码搜索和文件发现通过 `execute` 调用沙箱里的 `rg`：

```bash
rg -n 'pattern' path
rg --files -g 'pattern' path
```

`rg` 由宿主环境提供，优先使用当前 Python 环境里的可执行文件，其次使用系统 `PATH`。Bubblewrap 把它只读挂载到 `/run/deep-agent/rg`，并将 `/run/deep-agent` 放在沙箱 `PATH` 最前（自定义 `env_set.PATH` 或继承宿主 `PATH` 时也会保留）。持久 Worker 和一次性命令入口使用相同的 `rg` 挂载。模型直接执行 `rg`，不需要知道宿主安装路径。`ask` 的无网沙箱和 `allow` 的有网沙箱使用同一份二进制。Agent 不能自行安装或下载 `rg`。

宿主没有 `rg` 时，`execute` 的工具说明会写明代码搜索不可用，沙箱启动日志也会给出同样的诊断，不能把搜索说成可用。

搜索权限与 `execute` 一致：`ask` 下每次命令都要审批，`allow` 下免审批并默认使用有网沙箱。

如果搜索任务可拆成多个独立、互不干扰的子问题，主 Agent 可以在 `auto` 或 `allow` 权限下委派给 SubAgent，
例如不同模块的只读调查。子任务共享同一授权工作区，权限与主任务相同；`ask` 下不提供委派；
存在前后依赖或写入竞争的任务应由主 Agent 顺序处理。完整边界见 [SubAgent 配置](configuration.md#subagent-委派)。

`rg` 保持自身默认行为：位于 Git 仓库内时遵守 `.gitignore`、`.ignore`、`.rgignore` 和 `.git/info/exclude`，并默认跳过隐藏文件、符号链接、二进制文件以及大于自身限制的文件。需要放宽时使用 `rg` 自己的参数，例如 `--hidden`、`--no-ignore` 或 `-L`。

## ripgrep 安装与定位

安装项目时，Python 依赖 `ripgrep-bin>=15.2.0,<16` 会自动安装 `rg`，通常无需手动安装系统软件包。

`resolve_rg()` 按以下顺序选择可执行文件，无需配置路径：

1. 当前 Python 环境 scripts 目录中存在且具有执行权限的 `rg`，包括 venv 或 pipx 的项目环境。
2. 系统 `PATH` 中的 `rg`。
3. 两处均不可用时，不挂载 `/run/deep-agent/rg`，搜索不可用。

`rg` 定位函数只检查已安装的可执行文件，不自动下载二进制，也不修改宿主用户的 `PATH`。沙箱把找到的 `rg` 只读挂到 `/run/deep-agent/rg`，并把 `/run/deep-agent` 放在沙箱 `PATH` 最前。这项处理不涉及 Node、Cargo 或浏览器的自动发现；其他工具仍按 [挂载配置](sandbox.md#工具目录与环境变量) 使用。
pipx 环境中的依赖命令可能不会直接暴露到用户的 `PATH`，但 Agent 仍可找到环境内的 `rg`。

以下系统安装方式可作为备选：

### Ubuntu / Debian / WSL2

```bash
sudo apt update
sudo apt install ripgrep
```

### Fedora

```bash
sudo dnf install ripgrep
```

### Arch Linux

```bash
sudo pacman -S ripgrep
```

### 验证安装

使用虚拟环境时，在该环境中运行：

```bash
python - <<'PY'
import subprocess
from agent.tools.grep import resolve_rg

rg = resolve_rg()
if rg:
    print(rg, flush=True)
    subprocess.run([rg, "--version"], check=True)
else:
    print("rg unavailable")
PY
```

使用 pipx 时可检查项目环境中的依赖命令：

```bash
pipx runpip deep-agent-cli show ripgrep-bin
"$(pipx environment --value PIPX_LOCAL_VENVS)/deep-agent-cli/bin/rg" --version
```

验证系统备选安装：

```bash
rg --version
command -v rg
```

`ripgrep` 是系统软件包名称，`rg` 是实际执行的命令。WSL2 用户若选择系统安装，
需要在 WSL2 Linux 环境中安装。Agent 不要求用户把虚拟环境的 scripts 目录加入 `PATH`。

常驻的是沙箱和 Worker；每次搜索启动一个 rg，结束后退出。模型侧的搜索走 `execute` + `rg`。
`rg` 执行失败或沙箱启动失败都会报错，不会显示为 “No matches found”。
