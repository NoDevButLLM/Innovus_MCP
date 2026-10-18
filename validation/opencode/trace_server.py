"""The production FastMCP server plus test-only tool-call tracing middleware."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fastmcp.server.middleware import Middleware

from innovus_mcp.mcp_server import create_server


class TraceCalls(Middleware):
    def __init__(self, path: Path):
        self.path = path

    async def on_call_tool(self, context, call_next):
        started = time.time()
        row = {
            "tool": context.message.name,
            "arguments": context.message.arguments,
            "started_at": started,
        }
        try:
            result = await call_next(context)
            row["data"] = result.structured_content
            row["content"] = [item.model_dump(mode="json", exclude_none=True) for item in result.content]
            row["is_error"] = False
            return result
        except Exception as exc:
            row.update(is_error=True, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            row.update(finished_at=time.time(), duration_ms=round((time.time() - started) * 1000, 3))
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    server = create_server(args.directory)
    server.add_middleware(TraceCalls(args.trace))
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
