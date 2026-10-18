#!/usr/bin/env bash
# Install a release independently of its source checkout, without sudo.
set -euo pipefail

innovus_release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
innovus_package="${1:-$innovus_release_dir}"
innovus_python="${INNOVUS_MCP_PYTHON:-python3.11}"
if ! command -v "$innovus_python" >/dev/null 2>&1; then
    innovus_python="${INNOVUS_MCP_PYTHON:-python3}"
fi
"$innovus_python" -c 'import sys; sys.exit("Python >=3.11 is required") if sys.version_info < (3, 11) else None'

innovus_app_dir="$HOME/.local/innovus_MCP"
innovus_venv="$innovus_app_dir/venv"
innovus_entry="$HOME/.local/bin/innovus-mcp"
if [[ -e "$innovus_entry" && ! -L "$innovus_entry" ]]; then
    printf 'Cannot replace existing regular file: %s\n' "$innovus_entry" >&2
    exit 1
fi
mkdir -p "$innovus_app_dir/sessions" "$HOME/.local/bin"
"$innovus_python" -m venv "$innovus_venv"

innovus_uv="$(command -v uv || true)"
if [[ -z "$innovus_uv" && -x "$innovus_release_dir/.tools/uv/bin/uv" ]]; then
    innovus_uv="$innovus_release_dir/.tools/uv/bin/uv"
fi
if [[ -n "$innovus_uv" ]]; then
    "$innovus_uv" pip install --python "$innovus_venv/bin/python" --reinstall-package innovus-mcp "$innovus_package"
else
    "$innovus_venv/bin/python" -m pip install "$innovus_package"
fi
ln -sfn "$innovus_venv/bin/innovus-mcp" "$innovus_entry"
printf 'Installed: %s\nSessions: %s/sessions\n' "$innovus_entry" "$innovus_app_dir"
printf 'Ensure PATH contains %s/.local/bin, then run: innovus-mcp --help\n' "$HOME"
