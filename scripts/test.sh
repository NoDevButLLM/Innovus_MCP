#!/usr/bin/env bash
# Run repository tests with the environment created by install.sh --dev.
set -euo pipefail

innovus_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
innovus_test_python="${INNOVUS_MCP_TEST_PYTHON:-$HOME/.local/innovus_MCP/venv/bin/python}"
if [[ ! -x "$innovus_test_python" ]]; then
    printf 'Test Python not found: %s\nRun bash scripts/install.sh --dev first.\n' "$innovus_test_python" >&2
    exit 1
fi
cd "$innovus_repo_dir"
exec "$innovus_test_python" -m pytest "$@"
