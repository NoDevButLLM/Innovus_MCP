# Tests

Run tests using the Python environment created by the installer:

```bash
bash scripts/install.sh --dev
bash scripts/test.sh -q
```

`--dev` installs the package plus pytest, pytest-asyncio, and Ruff under `~/.local/innovus_MCP/venv`. The test runner locates the checkout from its own script path and changes to the repository root. It can be invoked from another working directory. It tests the installed package; reinstall after changing production code.

To use an editable environment, install `.[dev]` there and select its Python with `INNOVUS_MCP_TEST_PYTHON`. The runner accepts all pytest arguments:

```bash
INNOVUS_MCP_TEST_PYTHON="$HOME/.local/innovus_MCP/venv/bin/python" \
  bash scripts/test.sh -q tests/test_stream.py
```

## Coverage

- CLI parsing and the removal of obsolete human job commands.
- Legacy and OSC control frames split across capture chunks, completion detection, bounded summaries, and paging.
- Command-help validation, metadata, FastMCP schemas, deduplication, and interruption receipts.
- Real tmux with a fake interactive Innovus process: long jobs, large output/results, stdio reconnects, human handoff, read-only terminals, hidden OSC markers, and capture surviving broker exit.
- OpenCode trace analysis without calling a model service.

The fake process is a terminal fixture, not a Tcl interpreter. It does not validate Innovus commands or the production Tcl helper. Tests need Linux terminal and Unix socket support. Restricted sandboxes may require permission to create the local sockets used by the integration fixtures.

## Executable Selection

Python defaults to the installer's `~/.local/innovus_MCP/venv/bin/python`. Override it with `INNOVUS_MCP_TEST_PYTHON`.

tmux is selected in this order: `INNOVUS_MCP_TEST_TMUX`, `INNOVUS_MCP_TMUX`, then `tmux` on PATH. Set a supported executable when needed:

```bash
INNOVUS_MCP_TEST_TMUX="$(command -v tmux)" bash scripts/test.sh -q tests/test_integration.py
```

If tmux is unavailable, tmux integration tests are skipped. The CLI requires tmux 3.2 or newer. Fixtures create unique session roots under pytest's temporary directory and clean only their own tmux sessions.

## Real Innovus Checks

These checks require a licensed Innovus environment and an explicitly selected disposable session. They change Tcl variables and exercise errors, delays, output, and reconnects. Use a fresh empty session:

```bash
innovus_validation_root="$HOME/.local/innovus_MCP/validation/manual-$(date +%Y%m%dT%H%M%S)-$$/sessions"
innovus-mcp --root "$innovus_validation_root" start --session real
innovus-mcp --root "$innovus_validation_root" status --session real
```

Wait for `state: ready`, then run:

```bash
INNOVUS_MCP_REAL_SESSION="$innovus_validation_root/real" \
  bash scripts/test.sh -q tests/test_real_innovus.py
innovus-mcp --root "$innovus_validation_root" stop --session real --force
```

Without `INNOVUS_MCP_REAL_SESSION`, these tests are skipped. The operator owns the selected real session's lifecycle; the pytest checks do not start or stop it.

For a broader direct MCP validation of an already started empty session:

```bash
"$HOME/.local/innovus_MCP/venv/bin/python" scripts/validate_innovus.py \
  --root "$innovus_validation_root" --session real \
  --artifacts "$HOME/.local/innovus_MCP/validation/manual-report-$(date +%Y%m%dT%H%M%S)-$$"
```

Run this before stopping that session, or create a new empty one. This flow records timings and byte hashes and exercises long output, Tcl results, queue retries, native terminal handoff, and MCP reconnects. It does not stop the operator-selected session.

The [OpenCode validation guide](../validation/opencode/README.md) covers self-contained real Innovus checks and optional model-driven agent runs.
