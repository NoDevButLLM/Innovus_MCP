"""Build a managed help job and decode its small, Tcl-written receipt."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from .common import BrokerError, tcl_word


def help_script(command: str, detail: str) -> str:
    if not isinstance(command, str) or not re.fullmatch(r"(?:::)?[A-Za-z_][A-Za-z0-9_:]{0,127}", command):
        raise BrokerError("invalid_command", "Supply one Tcl command name, without arguments")
    if detail not in {"summary", "full"}:
        raise BrokerError("invalid_detail", "Choose summary or full")
    helper = Path(__file__).parent / "tcl" / "command_help.tcl"
    return (
        f"source {tcl_word(str(helper))} -quiet\n"
        f"::innovus_mcp::command_help {tcl_word(command)} {tcl_word(detail)}"
    )


def read_help_metadata(path: Path) -> dict:
    # Values are UTF-8 encoded as base64 by Tcl; no optional Tcl JSON package is needed.
    values = {}
    for line in path.read_text(encoding="ascii").splitlines():
        key, value = line.split("=", 1)
        values[key] = base64.b64decode(value, validate=True).decode("utf-8")
    if values.pop("format_version") != "1":
        raise ValueError("Unsupported help receipt")
    values["complete"] = values["complete"] == "1"
    raw_exit = values.get("external_exit_code", "")
    values["external_exit_code"] = int(raw_exit) if raw_exit else None
    return values
