# Innovus MCP 使用约定

- 当前已配置名为 `innovus` 的 MCP。本指南已作为项目指令提供。
- OpenCode Code Mode 会延迟暴露 MCP 工具。在新的会话中，先在一次 execute 中执行 `return await search({query: "innovus command_help"});`，取得定义后在下一次 execute 调用 `tools.innovus.command_help`。后续复用已发现的定义。这是客户端工具发现，不会执行 Innovus 命令。
- 无需枚举 MCP resources、读取目录或用 shell 查找接入说明。
- 帮助参数名为 `command`、`request_id`、`detail`；`detail` 可为 `full` 或 `summary`。复用已发现的工具定义。
- 查询命令用法时优先使用 `command_help`。默认 `full` 提供用途、语法与参数说明；只需要语法时用 `summary`。
- 完整手册通常位于 `result.text`，简短帮助通常位于 `output.text`，两者都要检查。根据 `help.status` 判断获取是否成功，不能只看 Tcl 作业状态。
- 使用唯一 `request_id`；重试相同提交复用该 ID。任务未结束时保留同一个 job ID，使用有界等待观察。
- 终态内容足够回答时直接回答。无需额外检查 session_status、执行 READY 探测或查阅相关命令。
- 按 `has_more` 和 `next_offset` 读取必要后续页。`help.complete` 表示获取了所请求的文档；内联页仍受字节预算限制。
- 不推断文档未提供的参数默认值；明确说明降级、文档缺失及版本差异。
- 通用 managed Tcl 脚本需无需后续键盘输入。交互式 `man`、`less` 会占住当前作业；不要通过 query_tcl 打开它们。
- `interrupt_job.interrupt_sent` 表示本次是否发送 Ctrl+C；重复请求不会再发一次，中断请求不保证作业退出。
- 帮助查询使用 MCP；外部 tmux 输入和发送进程信号不属于正常帮助查询流程。
