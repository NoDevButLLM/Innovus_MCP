# 项目协作约定

本文件供维护本仓库的 coding agent 使用，适用于整个仓库。用户当前任务的明确要求优先。
面向 MCP 调用方的使用指令位于 `src/innovus_mcp/opencode_instructions.md`。

## 项目目标与范围

- 为真实 Innovus 会话提供 FastMCP stdio 接口，支持 agent 与人工共享同一个 tmux 终端。
- 当前 V1 重点是输入控制权、异步作业、长输出分页和非交互帮助查询。
- 按用户要求扩展功能；当前没有记忆系统、自动恢复、快照回滚或命令保护系统。
- 优先复用标准库与已有开源依赖，MCP 协议交由 FastMCP 处理。

## 代码入口

开始修改前，阅读 `README.md`、`pyproject.toml` 和相关模块。

| 文件 | 职责 |
| --- | --- |
| `src/innovus_mcp/cli.py` | `start`、`status`、`list`、`serve`、`attach`、`stop`、`delete` |
| `src/innovus_mcp/mcp_server.py` | FastMCP 工具、参数 schema、说明及 broker 转发 |
| `src/innovus_mcp/broker.py` | 作业队列、控制权、输出索引和完成判定 |
| `src/innovus_mcp/tmux_backend.py` | tmux 命令、输入投递与终端连接 |
| `src/innovus_mcp/capture_writer.py` | 独立采集进程，持续将终端输出写入磁盘 |
| `src/innovus_mcp/tcl/driver.tcl` | Innovus 内执行脚本、保存 Tcl 结果与完成记录 |
| `src/innovus_mcp/command_help.py`、`src/innovus_mcp/tcl/command_help.tcl` | 帮助作业构造、非交互手册读取和获取状态 |
| `src/innovus_mcp/human.py` | 人工 tmux 接管入口 |
| `src/innovus_mcp/launch_gate.py` | 协调启动与输出采集握手 |
| `src/innovus_mcp/common.py` | 数据目录、参数引用、共享常量及本地 RPC |
| `scripts/install.sh` | 安装独立 HOME 环境和命令入口 |

## 开发与兼容性

- Python 最低版本为 3.11；代码格式遵循 `pyproject.toml` 的 Ruff 配置，行宽 110。
- 沟通默认使用中文；`README.md` 必须使用英文，其他文档按用户要求选择语言；代码标识遵循现有英文命名。
- MCP 工具签名、说明、README 示例及实际默认值需要同步维护。
- MCP stdout 用于协议传输，诊断日志写 stderr 或日志文件。
- 调用外部程序使用参数列表；构造动态 Tcl 数据参数复用 `tcl_word`，避免 shell 拼接。
- 新增 Tcl 或其他运行资源时，同步维护 setuptools package-data，确保普通安装后可用。
- 代码和配置保持可迁移，不硬编码用户名、个人绝对路径、具体机器的 Innovus 路径或凭据；HOME 位置使用动态解析。

## 必须保留的执行语义

- `query_tcl` 与 `mutate_tcl` 只区分意图，两者均可执行任意 Tcl；不要声称 query 强制只读。
- 同一 session 的 managed 作业串行执行，所有输入投递都要遵守 broker 的控制权状态。
- `wait_ms` 是观察预算，超时或 MCP 客户端断开不会取消作业；后续观察沿用原 `job_id`。
- 同一 broker 生命周期内，相同 `request_id` 的相同脚本、来源和 intent 去重；等待和输出预算可调整。
- 控制台 output 与 Tcl result 独立保存和分页。所有游标按原始字节计数，使用返回的 `next_offset`。
- 附件大小 `null` 表示未确认，`0` 表示已确认为空。运行中的 `has_more=false` 不代表作业完成。
- 保留完成标记、completion manifest 与提示符的联合完成判定，不能只凭输出文本释放队列。
- `succeeded` 表示 Tcl 正常返回；外部程序退出状态和文档获取状态需要分别表达。
- `interrupt_job` 只请求一次 Ctrl+C，重复请求不追加发送；回执不代表退出或回滚。
- 帮助查询使用 `command_help`，避免 managed 作业打开交互式 man/less；保持 UI、版本和降级信息。
- 内联输出和摘要保持有界。不要把全部长日志直接塞进工具响应，或重复返回小输出的首尾正文。

## 人工终端与会话数据

- 人工初始连接只读；`Ctrl+B`、小写 `h` 请求接管，等待当前作业结束。
- 人工通过 `::innovus_mcp::release_native` 归还，broker 确认后恢复 agent。
- 不用外部 `tmux send-keys` 或进程信号绕过正常 managed 输入流程。
- session 默认保存在 `~/.local/innovus_MCP/sessions`，覆盖优先级为 `--root`、`INNOVUS_MCP_ROOT`、默认值。
- `start --cwd` 默认使用调用时的当前目录。设计目录、session 数据和安装环境分别管理。
- `stop` 不自动保存设计。`delete` 仅删除满足停止条件的会话记录，保持目录及存活检查。
- `stop --all`、`delete --all` 仅作用于选定 session root，保留逐会话检查、错误和部分成功结果；不要因用户要求添加批量功能而实际停止或删除用户会话。
- 修改源码不会热更新已运行 broker；需要新会话加载更新。不要为部署改动擅自重启用户会话。

## 检查与验证

只有用户要求测试或验证时，才新增或运行测试。按改动范围可执行静态检查：

```bash
"$HOME/.local/innovus_MCP/venv/bin/ruff" check src tests validation scripts/validate_innovus.py
"$HOME/.local/innovus_MCP/venv/bin/ruff" format --check src tests validation scripts/validate_innovus.py
bash -n scripts/install.sh
bash -n scripts/test.sh
```

使用 `bash scripts/install.sh --dev` 安装检查依赖，`bash scripts/test.sh` 默认使用该安装环境。
`INNOVUS_MCP_TEST_PYTHON` 可覆盖测试 Python；tmux 使用 `INNOVUS_MCP_TEST_TMUX`、`INNOVUS_MCP_TMUX` 或 PATH。
测试和验证脚本不得依赖个人绝对路径或仓库中的 `.tools`、`.venv`；仓库资源通过脚本位置解析。

用户要求真实验证时：

- 使用任务专用的空白 Innovus session 和独立数据目录，保留日志及调用记录。
- 操作已有用户会话须符合任务授权范围；不要为测试更名、删除或替换原会话。
- OpenCode 验证前核对 PWD、配置文件、会话 location、MCP 连接目标与 session root，避免继承父项目配置。
- 记录 Innovus MCP 调用、OpenCode 外层工具、shell 和前端错误，区分模型开销与工具耗时。
- 验证结束只清理本任务创建的进程和会话；报告实际执行内容及未覆盖的场景。
- 静态检查、模拟测试、真实 MCP 验证和真实设计验证分别说明，不混用结论。

## 文档与 Git

- `README.md` 是公开使用入口，覆盖安装、agent 连接、CLI、MCP 接口及命令处理方式。
- `README.md` 的正文、标题、表格、示例说明及注释均使用英文。
- Git commit message 的标题和正文均使用英文。
- `opencode.json` 引用的指令文件必须随仓库和安装包提供，不能依赖被忽略的文档目录。
- `tests/`、验证脚本及用例随仓库提交。`docs/`、本机环境、历史验证运行目录和生成数据由 `.gitignore` 排除。
- 保留本地辅助文件，不把它们作为运行依赖，不在公开 README 中引用未提交的文件。
- 按用户授权提交或推送，并检查明确的暂存文件清单。未经要求，不强制加入忽略文件或修改忽略策略。
- 保留用户已有改动，不覆盖、回退无关文件，不将凭据、许可证信息或设计数据提交到远程。
