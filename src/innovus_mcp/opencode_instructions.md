# Innovus MCP command usage

The configured `innovus` MCP provides `command_help` for Innovus command usage. In a new OpenCode
Code Mode session, first execute `return await search({query: "innovus command_help"});`, then call
`tools.innovus.command_help` in the next execute after its definition has been discovered. Reuse
that definition subsequently. This is client tool discovery and executes no Innovus command.
Resource enumeration, shell commands, directory inspection and readiness probes are unnecessary.
The help parameters are `command`, `request_id` and `detail`.

- Use `detail="full"` for purpose, syntax and parameter descriptions. Use `summary` for syntax only.
- Full manuals normally appear in `result.text`; runtime help normally appears in `output.text`.
  Inspect both and check `help.status`, `help.complete`, version and UI mode.
- Once terminal content is sufficient, answer directly. State missing documentation or fallback
  explicitly. Do not infer parameter defaults that the documentation does not provide.
- Retain a running job's ID, observe with bounded waits, and page only necessary remaining content
  using `next_offset`. For status-only checks set both page budgets to zero.
- Supply a unique request ID; reuse it for an identical retry. A wait deadline never cancels a job.
- Managed Tcl scripts must run without subsequent keyboard input. Do not open interactive `man`
  or `less` through `query_tcl`. Use the supported MCP operations for help; external tmux input and
  process signals are outside the normal help workflow.
- `interrupt_sent` reports this call's Ctrl+C action. A repeated request sends no further Ctrl+C;
  acknowledgment does not imply completion or rollback.

Humans use the documented tmux ownership handoff for direct design edits. `query_tcl` and
`mutate_tcl` remain unrestricted Tcl intent labels.
