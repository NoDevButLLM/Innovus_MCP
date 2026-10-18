# Innovus MCP

Give coding agents access to a real Innovus session through MCP, with human control of the same terminal through tmux.

- **FastMCP** provides a stdio interface. Submit queries with `query_tcl` and changes with `mutate_tcl`.
- The **broker** manages the job queue, input ownership, and results; **tmux** hosts the Innovus terminal.
- Supports asynchronous jobs, output capture to disk, byte-based pagination, and command documentation without an interactive pager.

## Contents

- [1. Installation](#1-installation)
- [2. Start an Innovus Session](#2-start-an-innovus-session)
- [3. Connect a Coding Agent](#3-connect-a-coding-agent)
- [4. CLI and Human Access Through tmux](#4-cli-and-human-access-through-tmux)
- [5. MCP Tools](#5-mcp-tools)
- [6. Command Handling and Optimizations](#6-command-handling-and-optimizations)
- [7. Data Directories and Updates](#7-data-directories-and-updates)
- [8. Tests and Validation](#8-tests-and-validation)

## 1. Installation

### Requirements

| Dependency | Requirement |
| --- | --- |
| Operating system | Linux |
| Python | 3.11 or later |
| tmux | 3.2 or later |
| Innovus | Installed, with a working startup environment and license |
| System `man` command | Required for full manual pages; summary help works without it |

Innovus and its license are not included. The installer creates a Python virtual environment under HOME and installs the dependencies declared in `pyproject.toml`, including FastMCP 2.14.5.

### Install

```bash
git clone https://github.com/NoDevButLLM/Innovus_MCP.git
cd Innovus_MCP
bash scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
```

Add the PATH setting to your shell startup configuration so future terminals and agents can find the command. Installation does not require sudo.

Installation paths:

```text
~/.local/bin/innovus-mcp           # CLI entry point
~/.local/innovus_MCP/venv/         # Dedicated Python environment and installed package
~/.local/innovus_MCP/sessions/     # Default session data directory
```

If Python is not on your default PATH:

```bash
INNOVUS_MCP_PYTHON=/path/to/python3.11 bash scripts/install.sh
```

The installer uses uv when available and falls back to pip. Python and Tcl runtime files are installed in the dedicated environment, so running MCP does not require entering the source repository.

## 2. Start an Innovus Session

Run these commands in a shell with the Innovus and license environment configured:

```bash
cd /path/to/design
innovus-mcp start --session chip
innovus-mcp status --session chip
```

**Without `--cwd`, Innovus uses the directory from which you run `start`.** The default launch command is `innovus -no_gui`.

Add `--gui` to launch Innovus with its graphical interface instead:

```bash
innovus-mcp start --session chip_gui --gui
```

With `--gui`, the default command is `innovus`; without it, `-no_gui` is added. The GUI inherits the shell's display environment, so configure a working graphical display (such as `DISPLAY` and X11 authentication) before starting. MCP and tmux still connect to the same session.

You can also specify the working directory, tmux executable, and Innovus launch arguments:

```bash
innovus-mcp start --session chip \
  --cwd /path/to/design \
  --tmux /path/to/tmux \
  -- /path/to/innovus -no_gui
```

Arguments after `--` specify the executable and its arguments. `start` returns immediately; wait until `status` reports `state: ready` before submitting agent commands. Use tmux to view startup and license output.

The GUI switch also applies to an explicitly supplied executable named `innovus`, including an absolute path: `--gui` removes `-no_gui`, while omitting `--gui` ensures it is present. Put `--gui` before `--`:

```bash
innovus-mcp start --session chip_gui --gui -- /path/to/innovus
```

Other custom launch commands, such as wrapper scripts, are passed through unchanged and must configure their own GUI mode.

Each session has its own Innovus process and tmux server. Humans and agents in the same session share the database, Tcl variables, and working directory. `start` will not overwrite an existing session directory.

## 3. Connect a Coding Agent

### OpenCode

Start the Innovus session first, then configure OpenCode. OpenCode launches the MCP stdio service automatically.

**From this repository:** the included [opencode.json](opencode.json) connects to `chip` and loads the [agent instructions](src/innovus_mcp/opencode_instructions.md). Start OpenCode:

```bash
opencode
```

**From your design project:** copy the instructions into the project so its configuration does not depend on the source repository location:

```bash
cd /path/to/design
cp /path/to/Innovus_MCP/src/innovus_mcp/opencode_instructions.md ./innovus-mcp-instructions.md
```

Add the following to the project's `opencode.json`. Merge the relevant fields if you already have a configuration:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["./innovus-mcp-instructions.md"],
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

Connection notes:

- Use the same `--session` name as the running Innovus session.
- OpenCode must inherit a PATH containing `~/.local/bin`. Alternatively, replace `command[0]` with the absolute path returned by `command -v innovus-mcp`.
- For a custom session root, use the same path when starting the session and the MCP service. For example: `"command": ["innovus-mcp", "--root", "/data/eda_sessions", "serve", "--session", "chip"]`.
- OpenCode Code Mode may require tool discovery before execution. The included instructions describe `search({query: "innovus command_help"})`; this client step does not execute an Innovus command.

Once connected, try:

> Use Innovus MCP to explain the main usage of place_opt_design.

### Other Agents Supporting stdio MCP

Use the same launch arguments in your client's MCP configuration format:

```json
{
  "command": "innovus-mcp",
  "args": ["serve", "--session", "chip"]
}
```

`serve` connects to an existing broker and does not launch another Innovus process. Each MCP service connects to one specified session; configure another service to connect to a different session.

## 4. CLI and Human Access Through tmux

### CLI Commands

| Command | Purpose | Example |
| --- | --- | --- |
| `start` | Create a session and launch the broker, tmux, and Innovus | `innovus-mcp start --session chip` |
| `status` | Show state, ownership, queue, and tmux attach command | `innovus-mcp status --session chip` |
| `list` | List running sessions under the selected session root | `innovus-mcp list` |
| `serve` | Provide the MCP stdio service for an agent | `innovus-mcp serve --session chip` |
| `attach` | Connect to the real Innovus terminal, initially read-only | `innovus-mcp attach --session chip` |
| `stop` | Stop one or all sessions while retaining logs and job records | `innovus-mcp stop --session chip` / `innovus-mcp stop --all` |
| `delete` | Permanently delete records for one or all stopped sessions | `innovus-mcp delete --session chip` / `innovus-mcp delete --all` |

`--session` defaults to `default` for commands targeting a single session, except that `delete` requires either `--session` or `--all`. For `stop` and `delete`, `--session` and `--all` are mutually exclusive. `list` takes no `--session` argument. Common `start` options include `--cwd`, `--gui` (default: off), `--tmux`, `--startup-timeout` (default: 180 seconds), and `--queue-size` (default: 32 pending jobs).

Agents normally launch `serve` automatically. Running it manually in a terminal waits for MCP messages. Use `attach` to enter Innovus Tcl commands yourself.

### List Running Sessions

```bash
innovus-mcp list
innovus-mcp --root /data/eda_sessions list
```

`list` returns JSON with `root`, `count`, `sessions`, and `errors`. Each session includes its name, state, ownership, working directory, launch command, active job, queue, artifact directory, and tmux attach command. It uses the same root precedence as other commands and sorts sessions by name. A missing root returns an empty list without creating directories.

The command checks broker status and tmux pane liveness concurrently, with up to eight sessions checked at once and a two-second deadline for each probe. It includes sessions still starting and live panes whose broker is unavailable (`broker_available=false`, `state="unavailable"`); ownership and job information are unknown for those sessions. A retained dead pane does not count as running. Stopped sessions and stale records with no live pane are excluded. Invalid records or inconclusive pane checks appear in `errors`, so a partial listing remains usable. Symlinked session directories are skipped. Listing does not submit Innovus commands or change input ownership.

### Attach, Take Control, and Release

```bash
innovus-mcp attach --session chip
```

You can also run the `attach_command` returned by `start` or `status`, which has this form:

```bash
/path/to/tmux -S /recorded/tmux.sock attach-session -f read-only -t innovus
```

Sessions use separate sockets. The tmux target is usually named `innovus`; use the complete command returned for your session.

1. The initial connection is read-only, so you can watch startup messages and agent output.
2. Press `Ctrl+B`, release it, then press lowercase `h`. The broker pauses agent submissions and scheduling, then waits for the active job to finish before enabling human input.
3. Once `NATIVE HUMAN CONTROL` appears, enter commands directly in Innovus. The tmux client reconnects once during handoff; the Innovus process keeps running.
4. When finished, clear any unfinished input and run this at a fresh Innovus prompt:

   ```tcl
   ::innovus_mcp::release_native
   ```

5. Stop typing. The broker switches the terminal back to read-only and resumes the agent after confirming the prompt.

Press `Ctrl+B`, then `d` to detach. Detaching or losing SSH during human control keeps the agent paused. Reconnect, take control, and explicitly release it to resume the agent. Human commands are captured in the terminal log but do not create individual managed jobs.

### Terminal Output

Managed jobs display a short job ID, an indented script preview, and a completion line:

```text
[innovus-mcp] BEGIN job=05d7deac
    puts "TOP:[dbGet top.name]"
    puts "INSTS:[llength [dbGet top.insts.name]]"
TOP:ibex_core
INSTS:15695

[innovus-mcp] END job=05d7deac status=succeeded tcl_code=0
innovus 19>
```

The preview preserves script line breaks and shows at most six lines of 76 characters each, with a notice when shortened. The complete script remains in the job's `request.tcl`; execution is unchanged. The terminal may also echo the internal `::innovus_mcp::run` invocation. Start/end banners and previews are excluded from MCP job output, which retains the command's own output and separate Tcl result. The completion line reports Tcl status, not design correctness.

Internal synchronization frames use a private OSC sequence consumed without display by tmux; their raw bytes remain in `pane.raw` for the broker. The parser accepts both this format and the legacy visible markers, including frames split across capture chunks. Completion still requires the marker, completion manifest, and returned Innovus prompt.

The tmux window follows the smallest attached client instead of staying fixed at 240 columns. Generated attachment commands use `-f read-only` to let viewers affect pane size; tmux's `-r` also enables `ignore-size`, which prevents that resizing ([tmux manual](https://github.com/tmux/tmux/blob/3.7a/tmux.1)). These changes apply to new sessions after installation; existing brokers and loaded Tcl helpers require a new session to use them.

### Stop and Delete

```bash
# Normal stop requires an idle session under managed control
innovus-mcp stop --session chip

# Force termination while busy or under human control
innovus-mcp stop --session chip --force

# Delete logs, jobs, and output files for a stopped session
innovus-mcp delete --session chip

# Stop all sessions under the selected root
innovus-mcp stop --all

# Force-stop all sessions, including busy sessions and those under human control
innovus-mcp stop --all --force

# Delete all stopped sessions; running sessions are refused individually
innovus-mcp delete --all

# Apply the same operations to a custom root
innovus-mcp --root /data/eda_sessions stop --all
innovus-mcp --root /data/eda_sessions delete --all
```

Stopping does not save the design automatically. `delete` requires a recorded state of `stopped` and confirmation that both the broker and tmux have exited. It allows for the brief broker shutdown delay after `stop` and rejects symlink session directories. The design working directory and installation remain in place. After deletion, you can reuse the session name.

`--all` takes a snapshot of session directories under the selected root, sorts them by name, and processes up to eight at once. It uses the same `--root` / `INNOVUS_MCP_ROOT` / HOME precedence as other commands. Symlinked directories are skipped; invalid session records produce individual errors. A missing or empty root succeeds with no results.

Batch operations continue after individual failures and return JSON with `root`, `action`, per-session `results`, and `succeeded`, `skipped`, and `failed` counts. The exit code is 1 if any session fails, otherwise 0. `stop --all` skips sessions already confirmed stopped and preserves normal idle/ownership requirements unless `--force` is supplied. An unavailable broker is reported as a failure; force does not bypass the broker to kill orphaned processes. `delete --all` does not stop running sessions or bypass deletion checks. To remove all session records, run `stop --all` (with `--force` if needed), inspect its results, then run `delete --all`.

## 5. MCP Tools

### Tool Reference

| Tool | Main Parameters | Purpose |
| --- | --- | --- |
| `command_help` | `command`, `request_id`, `detail` | Get a full manual page or summary help without executing the requested command |
| `query_tcl` | `script`, `request_id` | Submit a Tcl script with query intent |
| `mutate_tcl` | `script`, `request_id` | Submit a Tcl script with mutation intent |
| `session_status` | None | Inspect session state, input ownership, active job, queue, and output capture |
| `list_jobs` | `after_sequence=0`, `limit=20` | List jobs in submission order, up to 100 per page |
| `get_job` | `job_id`, `wait_ms` | Observe a job with optional bounded waiting, output summary, and Tcl result |
| `read_job_output` | `job_id`, `offset`, `max_bytes`, `view` | Read a page of job console output |
| `read_job_result` | `job_id`, `artifact`, `offset`, `max_bytes`, `view` | Read a page of the Tcl result, error details, or error code |
| `read_session_output` | `offset`, `max_bytes`, `native_interval_id`, `view` | Read session logs, optionally limited to one human-control interval |
| `cancel_job` | `job_id` | Cancel a queued job that has not started |
| `interrupt_job` | `job_id` | Request a single Ctrl+C for the active managed job |

`query_tcl` and `mutate_tcl` share the same execution mechanism. Intent labels do not enforce read-only access or provide automatic snapshots or rollback.

### Submission and Observation Parameters

| Tool | Default `wait_ms` | Default `max_output_bytes` | Default `max_result_bytes` |
| --- | ---: | ---: | ---: |
| `command_help` | 500 | 16384 | 16384 |
| `query_tcl` / `mutate_tcl` | 0 | 16384 | 4096 |
| `get_job` | 0 | 6144 | 4096 |

- Submitting with `wait_ms=0` immediately returns job metadata. A positive value waits until the job reaches a terminal state or the budget expires, returning bounded output and any confirmed result. `get_job` can return an existing summary even without waiting.
- Each wait is limited to 10000 ms. **A wait timeout ends only that observation; it does not cancel the Innovus command.**
- Submission output and result page budgets are each capped at 65536 B. Console summaries from `get_job` are capped at 6144 B. Set a budget to 0 to omit that body when only status is needed.
- `read_*` tools default to 16384 B per page, capped at 65536 B. `view="text"` returns readable text; `view="base64"` returns raw bytes encoded as Base64.

### Results and Job States

Console output appears in `output.text`; the Tcl return value appears in `result.text`. For example, `puts READY` prints to the console, while the value of `info patchlevel` normally appears in the Tcl result.

| Field | Meaning |
| --- | --- |
| `job_id` | Identifier for subsequent observation, cancellation, interruption, and pagination |
| `state` | `queued`, `running`, `succeeded`, `failed`, `cancelled`, or `unknown` |
| `terminal` | Whether the job is in a terminal state; `unknown` is also terminal, with an unconfirmed outcome |
| `output_bytes` | Number of job output bytes captured so far |
| `result_bytes` / `error_info_bytes` / `error_code_bytes` | Artifact sizes; `null` means unconfirmed, while `0` means confirmed empty |
| `next_offset` / `has_more` | Byte cursor after this page and whether more content is available |

`succeeded` means Tcl returned normally. Design checks, Innovus error messages, and external program failures still require inspection of output and any dedicated status fields. `read_job_result.artifact` accepts `result`, `error_info`, or `error_code`, and defaults to `result`.

For `interrupt_job`, `interrupt_sent` indicates whether this request sent Ctrl+C. The `reason` is `sent`, `already_requested`, `not_active`, or `terminal`. Repeated requests do not send additional Ctrl+C characters. An interruption receipt does not confirm that the job has exited or that changes have been rolled back.

## 6. Command Handling and Optimizations

| Command Type | Typical Handling | Optimizations |
| --- | --- | --- |
| Command documentation | `command_help` | Noninteractive manual rendering avoids man/less waiting for keyboard input; short manuals usually fit in one response |
| Fast queries or changes | Submit with `wait_ms=500` | Return as soon as complete, with inline output/result to reduce follow-up calls |
| Long-running jobs | Keep the job ID and continue observing with `get_job` | Execution is independent of MCP requests; bounded waits use job completion events, and agent disconnection does not cancel jobs |
| Large console output | Capture to disk and read by cursor | Independent capture avoids tmux scrollback limits; bounded summaries limit response size |
| Large Tcl return values | Page through the separate result artifact | Distinguishes printed output from return values and avoids repeated reads of empty artifacts |
| Queued jobs and retries | Execute serially and deduplicate by request ID | Prevents duplicate mutations; observation waits do not hold the submission lock |
| Commands requiring further keyboard input | Take human control through tmux | Managed jobs expect scripts that need no subsequent input; command documentation uses a dedicated tool |

Take human control before starting an interactive command. A program already waiting for input in a managed job occupies the queue, and human handoff must also wait for that job to finish.

### 6.1 Command Documentation

Call `command_help` with parameters such as:

```json
{
  "command": "place_opt_design",
  "request_id": "place-opt-help-001",
  "detail": "full"
}
```

The default, `full`, includes purpose, syntax, and options. Use `summary` for brief runtime help. Full manuals usually appear in `result.text`; summary help usually appears in `output.text`.

Full help uses the target Innovus process's MANPATH to locate the manual, checks legacy/Stylus UI and available version information, then renders the specific file with `man -P cat`. The `help` metadata reports source, version, UI, acquisition status, and external exit status:

- `ok`: The requested documentation was retrieved successfully.
- `fallback`: The full manual was missing or incompatible; summary help was attempted within the same job, with `complete=false`.
- `unavailable`: Help for the requested command could not be retrieved.
- `pending`: The job has not finished, so the acquisition result is unconfirmed.

`help.complete=true` means the requested document was acquired in full; the inline response may still require pagination. Once enough content is available, the agent can answer without extra session checks, READY probes, or related-command queries. OpenCode tool discovery may add client steps, so one Innovus MCP call does not guarantee a single step across the entire client workflow.

### 6.2 Fast Commands and Long-Running Jobs

For a fast query, call `query_tcl`:

```json
{
  "script": "dbGet head.?",
  "request_id": "db-schema-001",
  "wait_ms": 500
}
```

If the job finishes within 500 ms, use the content in that response. If its state is still `queued` or `running`, call `get_job`:

```json
{
  "job_id": "<job_id returned by submission>",
  "wait_ms": 10000
}
```

Use `mutate_tcl` for long-running changes. After a wait expires, keep observing the same job instead of resubmitting the command. New jobs queue while the session is busy; human handoff pauses managed submissions and scheduling.

Use a unique `request_id` for each new submission. Within the same broker lifetime, retrying the same script, origin, and intent with the same ID returns the original job. Reusing the ID for different execution content is rejected. Retries may change the wait duration or output budgets.

### 6.3 Large Output and Return Values

An independent tmux `pipe-pane` consumer continuously writes output to `pane.raw`. Reads use job output indexes on disk, so agents do not depend on screenshots or finite tmux scrollback.

- Reuse inline pages first and read further only when needed. `offset` and `next_offset` are **raw byte offsets**, not text character counts.
- `get_job` returns small output once. When output exceeds the default summary budget, it returns nonoverlapping head and tail sections of 2048 B and 4096 B, with `summary_truncated=true`.
- To fill the gap in a summary, read from `output.next_offset` up to the already-read `output_tail.offset`. For a contiguous submission page, continue directly from its `next_offset`.
- While a job is running, `has_more=false` means only that the current snapshot has been read. More output may arrive; use `terminal` to determine whether the job has ended.

To read more of a Tcl return value, call `read_job_result`:

```json
{
  "job_id": "<job_id returned by submission>",
  "artifact": "result",
  "offset": 16384,
  "max_bytes": 65536
}
```

Replace `16384` with the actual `next_offset` from the preceding page. Console output has a separate cursor through `read_job_output`. Skip reads for artifacts with a confirmed size of 0. Use `view="base64"` when preserving exact bytes.

## 7. Data Directories and Updates

Session data defaults to `~/.local/innovus_MCP/sessions/<session>`, separate from the installation and design working directory. It includes `session.json`, broker logs, `pane.raw`, job scripts, results, and output indexes. Disk usage grows with output; use `delete` after stopping a session to remove its records.

Storage precedence is: `--root` > `INNOVUS_MCP_ROOT` > the HOME default. Place `--root` before the subcommand:

```bash
innovus-mcp --root /data/eda_sessions start --session chip --cwd /path/to/design
innovus-mcp --root /data/eda_sessions status --session chip
```

You can also set `INNOVUS_MCP_ROOT` consistently in the session startup and agent environments. Local communication sockets use short paths under `/tmp/imcp-UID-ID/`.

After updating the source, run `bash scripts/install.sh` again. **Existing brokers do not reload code.** Save any required design state, then use a new session or stop and recreate the old one. Point the agent configuration to the corresponding session name.

V1 does not provide memory, automatic recovery, or command protection. Closing an MCP client does not stop independently running jobs. If the broker exits, tmux and capture processes may continue, but this version cannot rebuild the broker to take over those processes. Copying session files to another machine preserves records; it cannot restore a running Innovus database or process.

## 8. Tests and Validation

Install the development dependencies in the same HOME environment as the CLI, then run tests from a checkout:

```bash
bash scripts/install.sh --dev
bash scripts/test.sh -q
```

The runner defaults to `~/.local/innovus_MCP/venv/bin/python` and tests the installed package. Set `INNOVUS_MCP_TEST_PYTHON` to use another environment. tmux is selected from `INNOVUS_MCP_TEST_TMUX`, then `INNOVUS_MCP_TMUX`, then PATH; tests do not require a repository-local Python or tmux installation. Integrations use disposable fake-Innovus sessions, and real Innovus checks are opt-in.

See the [test guide](tests/README.md) for coverage, configuration, and real-session checks. The [OpenCode validation guide](validation/opencode/README.md) describes direct MCP checks and agent runs with exported call traces. Real validation artifacts default to `~/.local/innovus_MCP/validation`, with an `--output` override. Test code and scripts are tracked; generated logs, exported conversations, and local environments are excluded from Git.
