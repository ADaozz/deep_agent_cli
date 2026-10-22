# 沙箱与权限

工作区在沙箱里的路径就是它的真实绝对路径。例如 Workspace 为 `/mnt/d/test` 时，`restricted` 和 `full-access` 的工作区路径、`pwd` 和文件工具都使用 `/mnt/d/test`。切换模式不改变这条路径。每次命令调用都从该目录开始，上次调用的 `cd` 不会延续。

| 项目 | `restricted` | `full-access` |
|---|---|---|
| Workspace 路径 | `/mnt/d/test` | `/mnt/d/test` |
| 当前工作目录 | `/mnt/d/test` | `/mnt/d/test` |
| Workspace 写入 | 允许 | 允许 |
| Workspace 外写入 | 按沙箱挂载规则限制 | 按操作系统权限 |

本项目有三层相互独立的控制：

- **沙箱**（Bubblewrap）决定 `execute` 运行的命令能访问什么。找不到可用的 `bwrap` 时启动失败，不会改在宿主机上执行。
- **文件系统模式**（`restricted` / `full-access`）决定沙箱里能看见和写入哪些路径。它不提权，也不取消审批。
- **权限模式**（`ask` / `allow`）决定工具调用前是否需要你确认。

## 执行模式

启动时程序启动一次沙箱并运行 `true` 作为预检（随后立即销毁），结果决定执行模式：

| 模式 | 条件 | 命令在哪里运行 |
|---|---|---|
| `SANDBOXED` | 找到 `bwrap` 且预检成功 | Bubblewrap 沙箱 |
| `CUSTOM` | 作为库使用时显式传入 `backend` | 由该 backend 决定 |

预检失败时 CLI 拒绝启动。启动预检只验证沙箱可用性，不逐项检查 Node、Cargo 或浏览器能否运行。

## 文件系统模式

`sandbox.mode` 缺省为 `restricted`。运行中用 `/sandbox` 切换；切换在下一次模型调用前生效，不打断正在执行的命令，也不写回 `config.yaml`。新进程和恢复会话都回到配置文件里的模式。`/sandbox` 不改变模型看到的工作区路径，也不改变 `ask` / `allow`。

随程序提供的沙箱配置是一份最小基线，不是针对具体部署的安全策略。它只确定工作区、运行所需的只读系统目录、持久的 `/tmp` 与 `/home`，以及模板里若干可选的常见工具路径。这些可选路径在源目录不存在时跳过，既不表示工具已经可用，也不覆盖容器、多用户主机或自定义安装前缀。

可见范围和需要隔离的目录由部署方按实际环境声明。通过 `extra_read_only_mounts`、`extra_read_write_mounts` 和 `env_set` 提供运行所依赖的路径，并避免把凭据、密钥及其他不准备交给 Agent 的目录纳入挂载。`full-access` 仍强制保护 Agent 安装目录和配置目录，额外挂载不能解除这层保护。其余路径是否可见、是否可写，以当前模式和显式挂载为准。

| 模式 | 可见范围 |
|---|---|
| `restricted` | 工作区真实路径、`/skills`、声明的额外挂载，以及持久的 `/tmp`、`/home`。系统目录只读，不映射整个宿主文件系统 |
| `full-access` | 看到当前环境的真实文件系统。工作区就是它的真实绝对路径，不再另建虚拟目录。写入直接落在当前环境：容器内就是容器，本机就是本机。Agent 安装目录只读，配置目录不可见 |

两种模式都使用工作区的真实绝对路径、额外挂载，以及 `/run/deep-agent` 上的 worker 和 `rg`。`/home` 与 `/tmp` 都不是 tmpfs，沙箱进程结束不会丢掉其中的文件。`restricted` 仍把持久的 home 和 tmp 挂到 `/home` 与 `/tmp`；工作区若位于这两个目录之下，会在该挂载之后按真实路径重新挂上，因此路径不变且可以写入。

- `restricted`：`~/.deep-agent/sandbox/home` 挂到 `/home`，`~/.deep-agent/sandbox/tmp` 挂到 `/tmp`（目录不存在就创建）。`HOME=/home/agent`。切回 `restricted` 后仍看到这两个专用目录。
- `full-access`：`HOME` 是真实家目录，`TMPDIR=/tmp`。`/usr` 等运行时目录保持宿主持有的权限，不再铺一层空的 `/etc`。工作区路径与宿主目录是同一条绝对路径。

### HOME 隔离与持久化

文件系统模式决定沙箱内的 `HOME`。两种模式默认不共享用户环境。

| 模式 | HOME | 物理存储 |
|---|---|---|
| `restricted` | `/home/agent` | `~/.deep-agent/sandbox/home` |
| `full-access` | 真实用户家目录 | 宿主机真实家目录 |

`restricted` 将用户级安装、工具缓存和运行配置写入独立的持久目录，因此不会默认暴露宿主机个人环境。`full-access` 使用宿主机家目录，可以直接使用其中已有的用户级依赖和工具配置。

这些目录在 Worker 重建、CLI 退出和会话切换之后仍然保留。模式切换不在两者之间迁移安装结果。在 `restricted` 中以 `pip install --user` 安装的包，进入 `full-access` 后可能无法导入；相应文件仍位于 `~/.deep-agent/sandbox/home`。

`/sandbox` 不改变工作区的真实路径及其内容，也不删除任一模式已有的 HOME。切换会使当前沙箱进程退出，随后按目标模式的挂载重新启动。一个 HOME 中的软件不构成另一模式的可用依赖。

两种模式需要使用同一套依赖时，应将其放在工作区的 `.venv` 中，或挂载到 `/home/agent` 以外的路径并配置相应环境变量。`/home/agent` 不能作为额外挂载的目标，因此不能通过单条挂载把 `restricted` 的 HOME 替换为真实家目录。

`full-access` 在用户挂载之后盖上自保护，额外挂载不能揭开或改写它们：

- 安装文件只读：`agent` 包目录和已安装的 `deep-agent` 入口脚本。不保护整个 `site-packages`。可编辑安装时只有 `agent/` 只读。
- 配置目录不可见：`paths.config_dir`，未设置时为 `~/.deep-agent`。配置目录上是空的、权限为 `000` 的 tmpfs；若 `source_path` 或 `state_path` 落在该目录之外，再单独遮住那一个文件。`/skills` 仍是 skills 的可写入口。

自保护路径不能是 `/`，也不能包含工作区。工作区若位于安装目录内部，`full-access` 启动失败。tmpfs 的属主仍可 `chmod` 这个空挂载，但看到的只是空目录，读不到宿主配置文件。

## 沙箱内可见的内容

`execute` 命令运行在当前 Agent 会话的持久 Bubblewrap 沙箱中（见下文“沙箱生命周期”）。`restricted` 的默认视图：

| 路径 | 内容 |
|---|---|
| `/mnt/d/test` | 工作区，读写 |
| `/skills` | `~/.deep-agent/skills/`，读写（目录不存在时创建） |
| `/usr`、`/bin`、`/lib`、`/lib64` | 宿主机对应目录，只读 |
| `/etc` | 只有 `passwd`、`group`、`nsswitch.conf`、`os-release`、`ld.so.cache`、`localtime`、`alternatives`；开放网络时另加 `hosts`、`resolv.conf`、`ssl`。这份列表写在代码里。还需要别的路径时，在 `extra_read_only_mounts` 里按相同路径补上，并设 `optional: true`；源路径不存在就跳过。示例配置里的 `/etc/pki` 就是这样补上的 |
| `/tmp`、`/home` | 持久目录 `~/.deep-agent/sandbox/tmp` 与 `~/.deep-agent/sandbox/home`；`HOME=/home/agent` |
| `/proc`、`/dev` | 沙箱自己的实例 |

表外的宿主路径只有在工作区或额外挂载覆盖它们时才可见。`full-access` 改为映射当前文件系统，并在最后盖上安装只读和配置目录遮罩。默认模板已包含若干可选工具目录挂载，完整清单见 [默认工具挂载](configuration.md#默认工具挂载)。其他安装位置由用户通过 `sandbox.extra_read_only_mounts` 或 `extra_read_write_mounts` 声明，并按需调整环境变量。

额外挂载的限制：`source` 支持 `~`，相对路径按配置文件目录解析；`destination` 必须是沙箱内的绝对路径，不支持 `~`，不能是工作区挂载根（本例为 `/mnt/d/test`）、`/tmp`、`/home/agent`，也不能与 `/skills` 或 `/run/deep-agent` 重叠。`source` 必须存在，除非该项写了 `optional: true`；可选源不存在时跳过，不寻找替代来源。

挂载用于控制文件系统可见范围。用户可以选择较宽的只读目录，也可以只暴露所需版本或文件，均通过现有挂载列表表达，审批规则仍由 `ask` / `allow` 独立决定。只读只能禁止写入，不能阻止读取其中的密钥、凭据等敏感文件。

文件工具使用同一份启动配置。能安全表达的挂载才会同时进入 Bubblewrap 和文件工具路由：

- 同一 `destination` 不能出现两次。后应用的挂载如果盖住先前的目标，启动直接失败，避免只读声明被后面的读写挂载悄悄覆盖。
- 嵌套目标的 `source` 必须就是父挂载里的对应路径。不能把另一棵目录叠进已挂载的父目录，否则列举父目录会和沙箱里看到的内容不一致。
- 同一宿主目录可以同时挂到不同的虚拟路径，并且权限可以不同。文件工具按虚拟路径判断只读或读写：只读路径不能改，读写路径可以改，两边看到的是同一份数据。
- 这些检查发生在 Backend 初始化时。Agent 不能在运行中新增挂载或改写这张表。

其他隔离设置：

- 独立的 PID、IPC、UTS 命名空间；`deep-agent` 退出（包括被强制结束）时沙箱进程随之结束。
- 沙箱内需要 `/usr/bin/python3`（或 `/usr/local/bin/python3`、`/bin/python3`）来运行常驻 Worker；Worker 脚本只读挂载在 `/run/deep-agent/`，额外挂载不能覆盖该目录。
- 环境变量先清空，再设置 `PATH=/run/deep-agent:/usr/local/bin:/usr/bin:/bin`、`HOME`、`PWD`、`TMPDIR`，并传入宿主的 `LANG`、`TERM`、`COLORTERM`、`NO_COLOR`、`TZ` 和 `LC_*`。其他变量需要列入 `sandbox.env_allowlist` 或写在 `sandbox.env_set` 中。自定义 `PATH` 替换默认工具目录，程序随后自动将 `/run/deep-agent` 放到最前；宿主已安装的内置 `rg` 因此仍可直接调用。生成模板另含 Cargo 和浏览器相关环境设置，见下节。
- 不限制 CPU、内存、磁盘或进程数。

## 工具目录与环境变量

程序不自动发现 Node、Cargo、Chrome 等工具的安装位置，也不自动选择版本。默认模板采用常见目录；能加载配置或启动沙箱，不代表所有可选工具都已安装并可运行。

- **Node / npm / npx**：`~/.nvm/versions/node` 整体只读挂载到 `/opt/node`。在沙箱中查看该目录，再由用户将 `/opt/node/<实际版本>/bin` 加入 `env_set.PATH`，保留已有 Cargo 和系统路径。PATH 不支持版本通配符。若把源目录缩小到单个版本并仍挂载为 `/opt/node`，PATH 相应改为 `/opt/node/bin`。系统 Node 可使用已有系统路径。
- **Cargo / Rustup**：模板将 `~/.cargo` 挂载为 `/opt/cargo`，将 `~/.rustup` 挂载为 `/opt/rustup`，并设置 `RUSTUP_HOME=/opt/rustup`。自定义安装位置需要同步调整源目录和环境变量。模板没有将 `CARGO_HOME` 指向只读宿主目录；Cargo 默认缓存写入沙箱 HOME。`~/.cargo` 挂载范围仍包含其中的配置和凭据，需要更小可见范围时可将源缩小为 `~/.cargo/bin`，目标改为 `/opt/cargo/bin`。
- **Chrome 与缓存**：`/opt/google` 的程序目录只读，Playwright 浏览器缓存 `/opt/ms-playwright` 单独读写挂载。宿主使用 `XDG_CACHE_HOME` 或自定义缓存位置时，将缓存 `source` 改为实际目录，并保持沙箱目标和 `PLAYWRIGHT_BROWSERS_PATH` 一致。
- **X11**：模板将已有的 `/tmp/.X11-unix` 只读挂载到同一沙箱路径。WSLg 若使用 `/mnt/wslg/.X11-unix`，修改原条目的 `source` 即可；不要再添加一个相同目标。显示环境及认证说明见 [Playwright 与有头 X11](#playwright-与有头-x11)。

这些挂载均为可选项；缺失会跳过，也不会自动安装工具。升级不会覆盖 `~/.deep-agent/config.yaml`，现有用户需对照模板手动合并。YAML 中的列表会整体替换对应配置，不会自动追加条目；编辑示例时应保留仍需使用的挂载和环境变量。修改后重启 CLI，新建的沙箱才使用更新配置。

## 沙箱生命周期

每个 Runner（CLI 进程中的执行实例）最多有两个持久沙箱，都在第一次需要时才启动，并在该 Runner 的不同对话会话之间复用。不同 CLI 进程各自管理自己的沙箱：

| 沙箱 | 何时使用 | 网络 |
|---|---|---|
| 无网沙箱 | `network=false` 的 `execute` | `--unshare-net`，只有自己的 `lo` |
| 有网沙箱 | `network=true` 或 `allow` 模式下的 `execute` | 共享宿主网络 |

- 每次 `execute` 都启动一个新的 `/bin/sh -lc`：`cd`、shell 变量等状态不保留；保留的是沙箱环境本身——`/tmp`、`/home` 中的文件和后台进程（例如 `nohup cmd > log 2>&1 &`）在后续调用中仍然存在。这两个目录在有网沙箱和无网沙箱之间也是同一份：`restricted` 用专用持久目录，`full-access` 用当前环境的真实目录。PID 命名空间仍然分开。
- 两个沙箱共享工作区真实路径、`/skills`、`/tmp`、`/home` 和启动时声明的额外挂载。
- 沙箱的主进程是一个常驻 Worker，宿主只通过它继承的管道下发命令。这个控制通道不出现在文件系统或网络中，Worker 本身设为不可 dump，沙箱内的进程不能通过 `/proc` 接管它；无网沙箱看不到有网沙箱的进程，也连不上它的抽象 Unix socket（抽象 socket 随网络命名空间隔离）。
- 取消和超时只杀掉当前命令的进程组（包括它启动的后台进程），沙箱继续可用。
- Worker 异常退出时，正在执行的命令返回 `sandbox_lost` 错误，不会自动重试；下一次 `execute` 启动新的沙箱。
- 沙箱无法启动时，`execute` 返回错误，不会改在宿主机上运行。
- 切换会话（`/resume`、`/new`）或等待其他窗口释放会话时，保留当前 Runner 的沙箱及后台进程，不会接管其他 CLI 进程的沙箱。切换会话仍恢复配置中的文件系统模式；若模式确实变化，则重建沙箱。关闭 Runner 或退出 CLI 时，两个沙箱及其中所有进程都被销毁。`/tmp` 与 `/home` 里的文件保留。切换模型或权限模式不会重建沙箱。`/sandbox` 会关闭当前 Runner 的两个沙箱，下一次 `execute` 按新挂载启动。

## 网络

- 默认使用 `--unshare-net`：命令及子进程不能直接访问宿主 IP 网络，但仍可在自己的网络命名空间使用回环接口。
- 模型在调用 `execute` 时传 `network=true`，该条命令在有网沙箱中运行，能访问互联网、`localhost` 和局域网。`ask` 模式下这条命令仍需审批，审批界面会显示该参数。
- `allow` 模式下，每条 `execute` 都在有网沙箱中运行，单次调用无法关闭。

`network=false` 隔离的是**直接 IP 网络访问**，不保证完全没有间接网络通信。例如：有网沙箱中的后台服务在共享工作区下创建文件系统 Unix Socket；无网命令连接这个 Socket 发出请求；有网后台服务代为访问互联网，并把响应返回无网命令。共享文件或 FIFO 也可能构成类似通信路径。抽象 Unix Socket 与这里的工作区文件系统 Socket 不同，前者仍受网络命名空间隔离。

经其他有网进程实现的间接联网属于 **KNOWN LIMITATION**，不是沙箱逃逸。本轮不增加 Unix Socket 防护、Seccomp 或 IPC 配置开关，不改变共享工作区或双持久沙箱设计。

两个沙箱都按需启动、Runner 内跨会话复用；权限模式只决定下一条命令要不要审批，不决定沙箱是否销毁。文件系统模式只决定下一次启动时的挂载。

- `ask`：每条 `execute` 都要审批；`network=true` 走有网沙箱，`network=false` 走无网沙箱。
- `allow`：自动批准，且每条 `execute` 都走有网沙箱。
- 已经在跑的后台进程不受权限模式切换影响。需要停掉它们时，让 Agent 结束进程，或退出 CLI。`/sandbox` 会结束当前沙箱进程，因此也会结束其中的后台进程；两种模式各自目录里的文件都保留。
- `web_search` 在宿主进程中运行，与沙箱网络无关；`ask` 模式下每次搜索均需审批。

## 文件工具

`ls`、`read_file`、`write_file`、`edit_file`、`delete` 不经过 Bubblewrap，由 `deep-agent` 进程直接读写宿主文件，因此路径检查不能省略。`grep` 和 `glob` 不注册给模型。文件工具与当前文件系统模式的挂载一致：

| 路径 | `restricted` | `full-access` |
|---|---|---|
| `/mnt/d/test` | 工作区，读写 | 同一份工作区，读写 |
| `/skills` | Skills 目录，读写 | Skills 目录，读写 |
| 声明的额外挂载 | 按各自只读或读写 | 按各自只读或读写；不能揭开配置目录或把安装路径改成可写 |
| 安装目录 | 不可见 | 只读 |
| 配置目录及目录外的配置文件、会话库 | 不可见 | `Permission denied`，不列出真实内容 |
| 其他路径 | `Permission denied`。`/home` 与 `/tmp` 只通过 `execute` 访问 | 以 `/` 为根的真实文件系统 |

读取和写入都解析最终宿主路径。符号链接进入配置目录则拒绝，进入安装目录则只读。`ls /` 在 `full-access` 下列出真实根目录子项，并补上沙箱里才有的顶层挂载（如 `/skills`）。

目录挂载授权整个目标目录。单文件挂载只授权那个文件，不开放它所在目录里的其他文件，也不为了挂载文件而授权父目录。只读目标上的 `write_file`、`edit_file`、`delete` 和上传在接触宿主文件之前被拒绝；列举、读取、搜索和下载仍然允许。只读拒绝不依赖宿主目录本身的权限位，也不因为同一份数据另有读写别名而放行。

写入、编辑和上传会解析完整的宿主路径。路径落在当前虚拟路由下更具体的只读子挂载时直接拒绝，相对路径、链式和多级符号链接都不能绕过。指向同一宿主目录的只读别名和读写别名仍按各自的虚拟路径授权。

`delete` 不能删除挂载点本身，包括工作区根、其他目录挂载的根，以及单文件挂载。通过别名或父目录把挂载源删掉也同样拒绝，避免持久沙箱仍绑定已经消失的 inode。挂载内部的普通文件可以删除。删除时先解析父目录的真实路径，父目录落在只读子挂载里则拒绝。最后一级如果是符号链接，只删除链接本身，不跟随目标；链接所在的真实目录是只读的时候，链接也不能删。

Bubblewrap 为了运行命令而只读挂载的 `/usr`、`/bin`、`/lib`、`/lib64` 以及少量 `/etc` 文件，在 `restricted` 下不会自动成为文件工具的授权路径。`ls('/')` 和无路径 `glob` 聚合已经授权的目录；明确访问未授权路径返回 `Permission denied`。空的已授权目录返回空结果，而不是权限错误。工作区路径按完整绝对路径匹配，不会把相邻目录当成工作区。`..` 和指向授权根之外的符号链接都不能逃逸。单个文件的搜索上限仍是 10 MiB。

CUSTOM backend 保留调用方自己的规则。

`grep` 和 `glob` 不在模型工具表中。代码搜索和文件发现通过 `execute` 运行 `rg`：`rg -n 'pattern' path` 搜内容，`rg --files -g 'pattern' path` 找文件。宿主上的 `rg`（优先当前 Python 环境，其次系统 `PATH`）只读挂载为 `/run/deep-agent/rg`，并位于沙箱 `PATH` 的最前面，所以两个持久沙箱里都可以直接执行 `rg`。`rg` 保持自身默认行为，包括 Git 仓库内的 `.gitignore`、隐藏文件和符号链接。搜索的审批和网络与其他 `execute` 相同，不再使用以前独立 `grep` 工具的免审批规则。宿主没有 `rg` 时，`execute` 的说明和沙箱启动日志都会写明搜索不可用。未注册的搜索实现仍留在代码里，不会作为工具交给模型。

工作区内的所有内容对 Agent 都可读写，包括 `.git/`、`.env` 等。需要保护的文件不要放在工作区里。

## 权限模式

| 模式 | 需要确认的工具 | 可用条件 |
|---|---|---|
| `ask`（默认） | `execute`、`web_search`、`write_file`、`edit_file`、`delete` | 所有执行模式 |
| `allow` | 原生工具自动批准，所有 `execute` 开放宿主网络。宿主 MCP 工具、`mcp_add` 和 `mcp_remove` 仍需批准 | 仅 `SANDBOXED`；开启时需输入 `ALLOW` |

- `/permission ask|allow` 切换。空闲时立即生效；有任务运行或有待处理的审批、暂停时，在下一次模型调用前生效。切到 `allow` 不会自动批准已经在等待的审批。
- `/sandbox restricted|full-access` 切换文件系统模式，时机与 `/permission` 相同。空闲时打开选择；运行中必须写出目标模式。`full-access` 会写入当前真实环境。待生效时底栏显示 pending。它不修改 `ask` / `allow`，也不取消宿主 MCP 工具的审批，也不写入会话目录。
- 正在输入 `ALLOW` 时，新的工具审批、提问或暂停不会替换确认栏。提交或取消确认后才打开该面板；`F2` 可先查看，`Esc` 回到确认栏。
- 会话保存其权限模式，恢复会话时沿用。
- 作为库使用时，自定义工具如果有副作用，需要通过 `create_agent(interrupt_on=...)` 显式加入审批规则；内置工具的审批规则不能被覆盖。

## 命令输出与日志

- 输出实时显示在对应的工具块中。
- 超过 `sandbox.max_output_bytes`（默认 100000 字节）时，返回给模型的结果只保留末尾部分，完整输出写入 `<工作区>/.deep-agent/logs/exec/<时间>-<id>.log`。工具结果给出这条真实路径，文件工具可以直接读取。
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

浏览器应使用当前 CLI 的依赖版本安装，在宿主机手动运行 `playwright-cli install-browser --help`，再运行 `playwright-cli install-browser chromium`；不要用可能解析到不同版本的 `npx playwright install chromium`。默认 `playwright-cli open` 使用系统 Chrome；`--browser=chromium` 使用与该 CLI 匹配的缓存 Chromium。实际下载由用户准备，项目不锁定 Playwright CLI 版本。

Node 可以来自 `/usr/bin/node`，CLI 则可以在自定义 npm prefix 下。系统 Node 在默认 `/usr` 挂载中；额外 prefix 需要挂载其可执行文件及 npm 包整体（包含 `lib/node_modules`），再调整 PATH。精简示例：

```yaml
sandbox:
  extra_read_only_mounts:
    # 将此项追加到已有列表，保留仍需使用的 Node、Cargo / Rustup、Chrome 和 X11 挂载。
    - source: ~/.local  # 替换为 npm config get prefix 的实际结果
      destination: /opt/npm-prefix
  env_allowlist:
    - DISPLAY
  env_set:
    PATH: /opt/npm-prefix/bin:/opt/cargo/bin:/usr/local/bin:/usr/bin:/bin
    RUSTUP_HOME: /opt/rustup
    PLAYWRIGHT_BROWSERS_PATH: /opt/ms-playwright
```

此示例只新增 npm prefix 挂载，浏览器缓存沿用默认模板的可写 `/opt/ms-playwright` 挂载。若只使用已安装浏览器并希望只读缓存，先从 `extra_read_write_mounts` 移除缓存项，再加入 `extra_read_only_mounts`，同一目标不能同时存在于两个列表。

合并进现有配置，不覆盖其他挂载和允许的变量；上述源目录必须存在。系统 Chrome 还需要 `/opt/google` 的只读挂载；位于系统目录外的 Node 本体需要独立挂载并加入 PATH。不要把个人机器的 Node 目录当成通用要求。

有头模式需要允许传入 `DISPLAY`，并确保相应 X Server 可访问。默认模板已允许 `DISPLAY`，并可选只读挂载宿主 `/tmp/.X11-unix` 到同一沙箱路径；WSLg 若使用 `/mnt/wslg/.X11-unix`，修改现有条目的源路径即可。启动 CLI 的终端需已有正确的 `DISPLAY`。现有配置需手动合并这些项目，退出并重新启动 CLI 后生效。WSLg 已提供显示服务，无需另外安装 Xvfb。

已实测挂载 WSLg 的 X11 文件 socket 后，`network=false` 下 `playwright-cli open about:blank --headed` 可以启动、截图并关闭。普通 Linux X11 桌面可使用宿主 `/tmp/.X11-unix` 作为挂载源。若仅依赖 X11 抽象 socket，无网沙箱的网络命名空间仍无法连接宿主 X Server，需要 `network=true`；访问互联网页面也需要网络，ask 模式需审批。挂载显示 socket 允许连接宿主显示服务，不改变 IP 网络或 IPC 隔离。Xauthority 认证可能需要传入 `XAUTHORITY` 并挂载认证文件，目前未验证；WSLg 此处使用 X11，原生 Wayland 未覆盖。

保留 `--unshare-ipc`，在专用 Xvfb 中关闭 MIT-SHM：

```bash
Xvfb :2 -screen 0 1600x900x24 -extension MIT-SHM
# 宿主启动 CLI 前：export DISPLAY=:2
```

无需增加 Chrome 启动参数。不要为了适配浏览器删除 IPC 隔离，也不要擅自修改宿主安全策略。专用 Xvfb 上关闭 MIT-SHM 后，系统 Chrome 与测试 Chromium 才能稳定渲染；开启 MIT-SHM 时系统 Chrome 通常无法出画面。CLI 可能生成 `.playwright-cli/`，建议自行加入项目 `.gitignore`；程序不自动修改它。

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
