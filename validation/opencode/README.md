# OpenCode and Innovus MCP Validation

These flows exercise real Innovus through the production FastMCP server. Each self-contained run creates a disposable empty Innovus session and stops it in cleanup. Test code is tracked; generated logs, conversations, and environment snapshots remain local.

## Setup

From a repository checkout:

```bash
bash scripts/install.sh --dev
```

Use the installed Python at `~/.local/innovus_MCP/venv/bin/python`. Innovus and its license environment must be available. tmux is selected from `--tmux`, `INNOVUS_MCP_TEST_TMUX`, `INNOVUS_MCP_TMUX`, then PATH. Innovus and OpenCode are found on PATH unless an executable is supplied explicitly. No repository-local `.tools` or `.venv` is required.

Commands below run from the repository root using Python module execution. This makes the sibling validation modules available without changing PYTHONPATH.

## Direct MCP Checks

These runs do not call a model service:

```bash
"$HOME/.local/innovus_MCP/venv/bin/python" -m validation.opencode.validate_api --viewer
"$HOME/.local/innovus_MCP/venv/bin/python" -m validation.opencode.validate_api --stylus --viewer
```

Use `--innovus`, `--tmux`, and `--output` to override executables or the artifact location. `--viewer` attaches a real read-only tmux client.

Coverage includes full and summary help, missing commands, result paging, noninteractive behavior with pager environment variables, missing manuals, mismatched UI manuals, stderr warnings, external exit status, documentation version mismatches, long jobs, and 128 KiB output. Full-help cases require system `man` and the corresponding installed Innovus manuals. Some checks expect legacy/Stylus command documentation and may need adjustment for other Innovus releases.

Fault injection changes only the disposable session's environment and test-generated `man` wrapper. Those settings are restored before cleanup. No chip design is loaded.

## Real OpenCode Agent Runs

Install and configure OpenCode with a model provider first. Supply the model explicitly:

```bash
"$HOME/.local/innovus_MCP/venv/bin/python" -m validation.opencode.run_flow \
  --model 'provider/model'
```

Replace `provider/model` with an identifier configured in your OpenCode installation. Optional flags include `--opencode`, `--innovus`, `--tmux`, `--output`, and repeated `--case` selections:

```bash
"$HOME/.local/innovus_MCP/venv/bin/python" -m validation.opencode.run_flow \
  --model 'provider/model' --case place-opt
```

The flow sends test questions, Innovus documentation, and MCP responses to the selected model provider. It uses the existing OpenCode configuration without copying credentials. OpenCode runs with `--auto`; additional shell or tool calls are recorded and affect acceptance. The harness was exercised with OpenCode 2.0.32 and uses its `api`, `run`, and `session export` commands. Other CLI versions may require changes to those arguments.

[cases.json](cases.json) defines six rounds by default:

| Case | Repetitions | Expected Innovus MCP calls |
| --- | ---: | --- |
| Main `place_opt_design` usage | 3 | One full `command_help` per round |
| `dbGet` usage and examples | 1 | One help call plus at most one necessary page |
| Brief `place_opt_design` syntax | 1 | One summary `command_help` |
| Missing command | 1 | One help call reporting `unavailable` |

These are explicit acceptance budgets, not guarantees for every documentation version or model. A manual larger than the inline budget needs paging. Checks require pages to follow `next_offset` for the same job, without reading already returned content again.

Each round gets a separate git project, OpenCode configuration, instructions, and Innovus session root. The harness sets `PWD` and `OPENCODE_CONFIG`, creates an OpenCode session with the exact project location, and checks that location before sending a model request. Trace middleware wraps the production server without changing tool schemas or results.

## Artifacts and Results

Both flows default to `~/.local/innovus_MCP/validation`, alongside the installer's HOME application directory. Every run creates a unique timestamped subdirectory. `--output` changes the parent directory. Relative resource paths are resolved from the validation code; changing the checkout location does not require editing scripts.

Agent runs produce a top-level `summary.json` and `report.md`, plus per-case artifacts:

| File | Content |
| --- | --- |
| `session.json` | Full OpenCode session export |
| `session.md` | Readable messages and tool calls, excluding reasoning blocks |
| `events.jsonl` | Raw OpenCode run events |
| `mcp-trace.jsonl` | MCP parameters, full responses, durations, and exceptions |
| `summary.json` | Call counts, timings, and acceptance checks |
| `config-sources.json`, `create-session.json` | Configuration and session-location evidence |
| `audit-before.json`, `audit-after.json` | Broker state before and after the agent request |
| `sessions/acceptance/` | Disposable Innovus logs, jobs, results, and raw output |
| `stop.json` | Cleanup result |

Direct MCP runs produce `api-trace.jsonl`, `summary.json`, and session artifacts. The broader [validation script](../../scripts/validate_innovus.py) can also inspect an operator-selected disposable session; see the [test guide](../../tests/README.md).

Innovus MCP calls, OpenCode outer tool calls, shell calls, and frontend errors are counted separately. Resource discovery and frontend retries can add client steps even when the Innovus MCP call budget passes. Inspect the exported conversation as well as the summary. Broker audits and startup/cleanup are excluded from agent business call counts.

Acceptance uses full tool bodies. OpenCode's sanitized exports replace those bodies and cannot be used for content checks. A new run retains its own evidence rather than overwriting previous results. Agent-run summaries use `complete=true` only after the whole run finishes; failed acceptance returns exit code 1.
