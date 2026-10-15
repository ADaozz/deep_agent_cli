# 正则搜索

本文说明默认本地工作区 `grep` 的安装定位、参数和结果限制。

## ripgrep 安装与定位

`deep-agent` 的 `grep` 工具默认支持正则表达式，并优先使用
[ripgrep](https://github.com/BurntSushi/ripgrep)。安装项目时，Python 依赖
`ripgrep-bin>=15.2.0,<16` 会自动安装 `rg`，通常无需手动安装系统软件包。

程序按以下顺序选择搜索引擎，无需配置路径：

1. 当前 Python 环境 scripts 目录中存在且具有执行权限的 `rg`，包括 venv 或 pipx 的项目环境。
2. 系统 `PATH` 中的 `rg`。
3. 两处均不可用时，使用独立进程中的 Python `re` 回退。

程序只定位已安装的可执行文件，不自动下载二进制，也不修改 `PATH`。
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
    print("rg unavailable; Python regex fallback will be used")
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

### 执行环境

沙箱模式且找到 `rg` 时，搜索复用当前会话的持久沙箱：`ask` 使用无网实例，
`allow` 使用有网实例。常驻的是沙箱和 Worker；每次搜索启动一个 rg，结束后退出。
`grep` 是独立的只读工具，不调用 `execute`，不触发命令审批。

未找到 rg 或运行于 UNSANDBOXED 模式时，使用独立、可终止的宿主 Python `re`
进程，并通过目录描述符约束文件访问。非本地自定义 Backend 保留自身的搜索接口。
路径检查与进程生命周期见 [架构](architecture.md#grep-搜索)，隔离范围见
[沙箱与权限](sandbox.md)。

Rust Regex 与 Python `re` 的 Unicode 字符类等语法和行为存在差异；不支持的表达式
会明确报错，不会转为字面量搜索。`rg` 执行失败、沙箱启动失败都会报错，不会显示为
“No matches found”。

### 搜索模式与限制

默认按正则表达式匹配：

```text
grep(pattern="def (run|execute)\\(", output_mode="content")
```

如需搜索普通字符串，请设置 `fixed_string=true`，例如：

```text
grep(pattern="config.yaml", fixed_string=true)
```

**行为变更**：旧版默认按字面量搜索；现在 `.`, `*`, `+`, `[` 等字符默认解释为正则语法。
通过自定义非本地 Backend 注入的工具仍使用该 Backend 的原有搜索接口。

`path` 默认 `/workspace`，可以搜索挂载的 `/skills`。建议先用 `grep` 定位，再用
`read_file(offset, limit)` 查看上下文。

#### Glob 过滤

`glob` 可以是一个字符串或字符串列表，语义与 `rg --glob` 相同，每条规则单独传给 `rg`
（不拼接逗号，不经过 Shell）：

```text
grep(pattern="TODO", glob="*.py")                                   # 只搜索 Python 文件
grep(pattern="TODO", glob="!*.test.py")                             # 排除 .test.py
grep(pattern="TODO", glob="!**/node_modules/**")                    # 排除 node_modules 目录
grep(pattern="TODO", glob=["*.py", "!**/tests/**"])                 # tests 以外的 Python 文件
grep(pattern="TODO", glob=["!**/node_modules/**", "!**/.venv/**"])  # 只排除，其余照常搜索
```

- 普通规则表示包含；只要存在包含规则，未命中任何包含规则的文件都被跳过。
- `!` 前缀表示排除；只有排除规则时，搜索其余所有候选文件。`\!` 匹配字面量 `!`。
- 规则按列表顺序处理，多条规则同时命中时**后面的规则优先**：`["*.py", "!**/tests/**"]`
  排除 tests，而 `["!**/tests/**", "*.py"]` 中后置的 `*.py` 会把 tests 下的 Python 文件重新包含。
- 目录规则末尾的 `/` 只表示“仅目录”，去掉之后：不含 `/` 的规则匹配任意深度的
  文件名或目录名；以 `/` 开头或其余部分含 `/` 的规则锚定到 `path` 指定的搜索目录。
  因此 `!repo/` 排除任意深度的 `repo` 目录，而 `!/repo/` 只排除搜索根下的 `repo`。
  `!*/.git/*` 只匹配恰好一级子目录中的 `.git`，排除任意深度应写 `!**/.git/**`。
  `dir/**` 匹配目录内容。
- 直接指定文件路径时同样应用 Glob（按文件名匹配）。
- 非法 Glob（未闭合的 `[`、`{`，无效范围，末尾单独的 `\`，空规则，开头未转义的 `#`
  或结尾空白）以及含 `..` 的规则直接报错，不会当作普通字符串。Glob 只用于筛选，
  不是权限控制，不能扩大可搜索的目录。

#### 忽略规则

默认与 ripgrep 一致：遵守 `.gitignore`（位于 Git 仓库内时）、`.ignore`、`.rgignore`
和 `.git/info/exclude`；跳过隐藏文件和目录、符号链接、二进制文件以及大于 10 MiB 的
文件。`.git` 目录始终跳过（直接把它作为 `path` 时除外）。指定 Glob 不会关闭这些规则，
但与 ripgrep 相同，**命中包含规则的文件会覆盖 ignore 和隐藏文件规则**：例如
`glob="*.log"` 会搜索 `.gitignore` 中忽略的 `*.log` 文件；被忽略或隐藏的**目录**
不会因 `*.py` 这类文件规则而被遍历。

Python 回退引擎（UNSANDBOXED 或未找到 `rg`）实现相同的 Glob、隐藏文件、`.git`、
符号链接和二进制规则，但**不读取 `.gitignore`/`.ignore`/`.rgignore`**，因此可能比
`rg` 多返回被忽略的文件；需要时请用排除 Glob（如 `!**/node_modules/**`）。

#### 输出模式与限制

| 模式 | 返回内容 | `max_count`（默认 1000）限制 |
|---|---|---|
| `content` | 匹配行及行号 | 匹配行数 |
| `files_with_matches` | 去重后的文件路径 | 文件数，一个文件的多次命中只占一个名额 |
| `count` | 每个文件的匹配行数 | 文件数；每个文件的计数是完整的 |

`count` 统计匹配行数，不统计同一行中命中子串的次数。**行为变更**：此前 `max_count`
在所有模式下都按匹配行累计，现在只在 `content` 模式按行计算。

每次搜索总超时 10 秒，返回体积最多约 64 KiB，单行内容最多 2048 字节。结果末尾会
明确标注不完整的原因：

- `Results truncated: maximum match count reached.`：`content` 达到 `max_count`。
- `Results truncated: maximum file count reached.`：其他模式达到 `max_count`。
- `Results truncated: maximum output size reached.`：达到 64 KiB。
- `Results incomplete: search timed out; ...`：超时，返回超时前已经写出的部分结果；
  没有任何结果时返回 `Error: Search timed out`。`files_with_matches` 用 NUL 分隔路径，
  以免文件名中的换行被拆成两个结果；ripgrep 会缓冲这路输出，超时可能不带上尚未写出的路径。
  `count` 的记录是 `路径\0次数\n`，路径同样可以包含换行。
- `Results incomplete: ripgrep could not search some files.`：部分文件不可读。
- `Results incomplete: files larger than 10 MiB skipped.`：直接指定的文件或 Python
  引擎遍历中的大文件被跳过（`rg` 遍历时静默跳过大文件）。

结果按路径排序；被截断时 `rg` 并行搜索得到的部分结果集合不保证每次相同。
搜索只读，不触发命令执行审批。超时或取消会终止本次 rg 或 Python Worker 及其子进程；
正常情况下保留持久沙箱，无法确认命令终止或 Worker 失效时才清理对应实例。
两种引擎均按单行搜索，不支持多行 pattern。

