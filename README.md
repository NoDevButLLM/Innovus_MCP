# Innovus MCP

A FastMCP server and command broker for one shared, tmux-hosted Innovus session.

- Humans use the actual Innovus terminal through tmux, with explicit input ownership handoff.
- Agents submit asynchronous Tcl jobs through separate `query_tcl` and `mutate_tcl` tools.
- Long jobs continue after MCP disconnects. Console output and Tcl results have independent byte-based paging.
- An independent `pipe-pane` consumer records output to disk, beyond tmux's scrollback limit.

V1 has no memory, recovery, or command protection system. Both Tcl tools accept unrestricted scripts; query intent is not read-only enforcement. Native commands are recorded as terminal activity, not individually tracked jobs.

## Install

Requirements: Linux, Python 3.11+, tmux 3.2+, and an Innovus environment with a working license setup. The implementation uses the open-source `fastmcp` package, not a custom MCP protocol implementation.

```bash
bash scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer creates a standalone environment at `~/.local/innovus_MCP/venv` and the command `~/.local/bin/innovus-mcp`. It installs the package normally, so runtime imports and packaged Tcl files do not depend on the release checkout. Set `INNOVUS_MCP_PYTHON` if the Python executable has another name or location. After upgrading, start a new session to load the new broker code.

Runtime tmux selection is `start --tmux`, then `INNOVUS_MCP_TMUX`, then the executable on PATH. A source checkout also offers `.tools/tmux/tmux` as a fallback when PATH has no tmux. Specify a compatible executable if the system version is too old.

Local prerequisites may be installed under `.tools/` and the virtual environment under `.venv/`. These generated directories are ignored by Git. Runtime dependencies are declared in `pyproject.toml`.

## Start and inspect a session

Run from a shell where Innovus and its license environment are configured:

```bash
innovus-mcp start --session chip --cwd /path/to/design -- innovus -no_gui
innovus-mcp status --session chip
```

Start returns immediately. Wait until status reports `ready` before submitting work. Startup output and license diagnostics are visible by attaching read-only to tmux. Both start and status print an `attach_command` you can run directly.

Session artifacts default to `~/.local/innovus_MCP/sessions/<name>`, independent of the working directory or installation path. The CLI and MCP server use the same default. `--root` overrides `INNOVUS_MCP_ROOT`, which overrides the HOME default; explicit roots support `~`. Existing session names/artifacts are never overwritten automatically. For another machine, install the package there and start a new session; copying artifacts does not resume a running Innovus process.

## Human access: native tmux

Run the `attach_command` printed by `start` or `status` directly, for example:

```bash
/path/to/tmux -S /recorded/tmux.sock attach-session -r -t innovus
```

Or use the convenience connection command:

```bash
innovus-mcp attach --session chip
```

Both connect a read-only native tmux client. You can watch agent output and startup/license messages. The status line shows `MANAGED`.

**Take control:** press `Ctrl+B`, release the keys, then press lowercase `h`. The broker pauses managed submissions and dispatch, waits for the active job, and grants writable native control. Wait for `NATIVE HUMAN CONTROL` before typing Tcl. The tmux client reconnects once during takeover; the Innovus process and its state remain intact.

Type Innovus commands directly in the terminal. Native editing, completion, interactive input, and Ctrl+C retain Innovus's behavior. Human commands are captured in the session transcript without individual job IDs.

**Return control:** finish native work and clear partial input, then type this at a fresh Innovus prompt:

```tcl
::innovus_mcp::release_native
```

Stop typing after that command. The broker makes the attached client read-only, verifies the returned prompt and a fresh handshake, and automatically resumes agent jobs. The terminal remains connected for viewing; press `Ctrl+B`, then `h` again for another takeover.

`Ctrl+B`, then `d` detaches. Detaching during native control or losing SSH keeps agents paused. Reconnect read-only, press the takeover shortcut, finish native work, and explicitly return control. Additional viewers stay read-only. Use the printed `-r` attachment command; an uncoordinated writable attachment bypasses this cooperative ownership workflow.

The CLI contains `start`, `status`, `serve`, `attach`, `stop`, and `delete`. Human job submission, polling, output/result paging, and pause/resume commands have been removed. Agent job management remains available through MCP.

## Connect an agent through FastMCP

Portable OpenCode configuration (also supplied as `opencode.json`); ensure OpenCode inherits PATH containing `~/.local/bin`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["src/innovus_mcp/opencode_instructions.md"],
  "mcp": {
    "innovus": {
      "type": "local",
      "command": ["innovus-mcp", "serve", "--session", "chip"],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

Use the same session name for `start` and the MCP command. When overriding the session root, use the same override for both. Start Innovus separately before connecting. The stdio server is a lightweight client of the broker and does not launch another Innovus instance.

Tools: `command_help`, `session_status`, `query_tcl`, `mutate_tcl`, `list_jobs`, `get_job`, `read_job_output`, `read_job_result`, `read_session_output`, `cancel_job`, and `interrupt_job`.

### Command usage: noninteractive help

Prefer `command_help` for command-usage questions:

```json
{
  "command": "place_opt_design",
  "request_id": "place-opt-help-unique-001"
}
```

The default `detail="full"` returns the target session's manual without opening a pager. `summary`
returns runtime syntax help. Defaults are `wait_ms=500` and 16 KiB each for output and result pages.
Full manuals normally appear in `result.text`; summaries normally appear in `output.text`. Check both.
The `help` receipt includes status (`ok`, `fallback`, `unavailable` or `pending`), source path, Innovus
and documentation versions, UI mode, external exit code, and `complete`. Completeness describes
acquisition of the requested documentation; inline pages can still have `has_more=true`.

Full help resolves the installed manual with the running Innovus process's MANPATH, checks the UI
and available version information, and renders the exact file with system `man -P cat`. If the
manual is missing or mismatched, it returns runtime help in the same job with `status="fallback"`
and `complete=false`. Unknown commands return `unavailable`. System `man` and the installed Innovus
manuals are needed for full help; runtime summaries remain available without them.

The supplied `opencode.json` loads [agent instructions](src/innovus_mcp/opencode_instructions.md). When
copying it into another project, also copy that guide or update its `instructions` path. Once help
is terminal and sufficient, the agent should answer directly. Do not open interactive `man` or
`less` through `query_tcl`, or add session checks and READY probes to ordinary help queries.

For every submission, supply a unique `request_id`. If a call disconnects before acknowledgment, retry the identical submission with the same ID. The live broker returns the existing job instead of executing it twice. Reusing an ID with a different script, intent, or origin is rejected.

For short commands, use bounded waiting on submission:

```json
{
  "script": "help dbGet",
  "request_id": "help-dbget-unique-001",
  "wait_ms": 500,
  "max_output_bytes": 16384,
  "max_result_bytes": 4096
}
```

Both `query_tcl` and `mutate_tcl` accept these options. `wait_ms=0` (the default) returns job metadata immediately. A positive wait returns the same job metadata plus bounded `output` and, after confirmed completion, `result` pages. Reuse those pages instead of reading them again. Output and result budgets each cap at 65536 source bytes; zero omits the corresponding page. Negative budgets/waits clamp to zero; waits cap at 10000 ms. Observation options can change on an identical retry without changing the job or executing the script again.

If the job is still running, retain its ID and call `get_job` with `wait_ms` up to 10000. Expiry returns its current state and available output; this is an observation deadline, not an execution timeout. Disconnecting a waiter also leaves the job running. `succeeded` means Tcl returned zero; it does not certify design correctness. Errors printed by Innovus can coexist with Tcl success. Inspect the captured output when assessing a command.

## Output and results

Output pages default to 16 KiB and are capped at 64 KiB of source bytes. Each response includes `next_offset`, `available_bytes`, and `has_more`. Each reader has its own cursor. An empty page on a running job means no new output yet. `view="base64"` returns exact bytes; text mode replaces invalid UTF-8 and strips terminal formatting. Offsets always count source bytes.

The Tcl return value is separate from console output. Use `read_job_result` with `artifact="result"`, `"error_info"`, or `"error_code"`. Results are published after job completion, including large query return values. Native activity is accessible through `read_session_output`, optionally using the `native_interval_id` reported by status.

Completed jobs include `result_bytes`, `error_info_bytes`, and `error_code_bytes`. `0` means the artifact is empty and needs no additional read; `null` means its size has not been confirmed (including queued/running jobs and outcomes without confirmed completion). `get_job` includes up to 4096 bytes of the Tcl result by default; set `max_result_bytes=0` for metadata and output only.

`get_job` uses a single `output` page for up to 6144 bytes of console output. For larger output, it returns disjoint `output` (first 2048 bytes) and `output_tail` (last 4096 bytes) pages, with `summary_truncated=true`. `max_output_bytes` can lower that combined summary budget; zero omits both pages. These page objects replace the earlier `head` and `tail` text fields. Their offsets always refer to the original output bytes. For a truncated summary, read the missing middle starting at `output.next_offset`, stopping at `output_tail.offset` if the tail has already been consumed. For a contiguous submission page, continue from `output.next_offset`. Check `has_more` for the current snapshot; running jobs may emit more bytes later even when it is currently false.

The raw session transcript is `pane.raw`; job output indexes exclude control markers. Files are retained until the operator deletes the stopped session. Disk usage grows with emitted output. Background subprocess output is attributed by arrival interval and may not match its originating command.

## Cancel, interrupt, and stop

```bash
innovus-mcp stop --session chip
# Explicit termination of busy/native/unresponsive sessions:
innovus-mcp stop --session chip --force
```

Agents use the MCP `cancel_job` and `interrupt_job` tools. Only queued cancellation guarantees that no command ran. Interruption requests one Ctrl+C and does not imply completion or rollback. `interrupt_sent` reports this call's action; `reason` is `sent`, `already_requested`, `not_active`, or `terminal`. Repeating the request sends no additional Ctrl+C. A normal native handoff waits for the active job, so it cannot directly take over a job stuck in a pager. Prefer `command_help` and noninteractive options for managed jobs.

Stop performs no automatic design save. Disconnecting clients leaves the session running. If the broker exits, tmux and a healthy capture writer can continue, but v1 does not reconstruct the broker or replay jobs. The session manifest records the tmux socket for manual attachment:

```bash
/path/to/tmux -S /recorded/tmux.sock attach-session -t innovus
```

An unavailable session or incomplete capture is reported explicitly. Do not treat unknown job outcomes as safe to retry with a new request ID.

### Delete a stopped session

```bash
innovus-mcp stop --session chip
innovus-mcp delete --session chip

# With a custom session root:
innovus-mcp --root /path/to/sessions delete --session chip
```

`delete` permanently removes that session's directory, including logs, captured output and job
results. Supply `--session` explicitly. The session manifest must report `stopped`, and both broker
and tmux endpoints must be inactive. A short broker shutdown interval after `stop` is handled
automatically. Running or unconfirmed sessions and symlinked session directories are rejected.
`--root` and `INNOVUS_MCP_ROOT` follow the same rules as the other commands. Deletion leaves the
design working directory and installed software in place; the removed session name can be reused.

## Development

For an editable development environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check src
bash -n scripts/install.sh
```

Existing broker processes keep their loaded code: reconnecting the MCP adapter alone does not
activate broker changes. Use a new session to load an update, or stop an existing session after
saving its design and start a new one. V1 does not reattach a replacement broker to an existing
Innovus process.
