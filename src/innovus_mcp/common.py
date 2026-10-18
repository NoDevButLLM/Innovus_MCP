from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

MAX_REQUEST = 2 * 1024 * 1024
MAX_PAGE = 65536
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
DEFAULT_PROMPT = r"(?:@?innovus\s+\d+>|@?innovus>)\s*$"


class BrokerError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def atomic_json(path: Path, value: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    tmp.replace(path)


def default_session_root() -> Path:
    configured = os.environ.get("INNOVUS_MCP_ROOT")
    root = Path(configured) if configured else Path.home() / ".local" / "innovus_MCP" / "sessions"
    return root.expanduser().resolve()


def session_dir(root: str | Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
        raise BrokerError("invalid_session", "Use 1-48 letters, digits, underscores or hyphens")
    return Path(root).expanduser().resolve() / name


def runtime_dir(identity: str) -> Path:
    path = Path(tempfile.gettempdir()) / f"imcp-{os.getuid()}-{identity[:16]}"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def tcl_word(value: str) -> str:
    return (
        '"'
        + "".join(
            {"\\": "\\\\", '"': '\\"', "$": "\\$", "[": "\\[", "]": "\\]", "\n": "\\n", "\r": "\\r"}.get(c, c)
            for c in value
        )
        + '"'
    )


async def request(directory: Path, method: str, **params):
    try:
        config = json.loads((directory / "session.json").read_text())
        reader, writer = await asyncio.open_unix_connection(config["broker_socket"], limit=MAX_REQUEST)
    except (OSError, ValueError) as exc:
        raise BrokerError("broker_unavailable", str(exc)) from exc
    try:
        body = json.dumps({"version": 1, "method": method, "params": params}).encode() + b"\n"
        if len(body) > MAX_REQUEST:
            raise BrokerError("request_too_large", "Request exceeds 2 MiB JSON envelope limit")
        writer.write(body)
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 20))
        if "error" in response:
            raise BrokerError(response["error"]["code"], response["error"]["message"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()
