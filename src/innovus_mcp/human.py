"""Internal tmux takeover entry point; humans enter Tcl in the native terminal."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .common import BrokerError, request
from .tmux_backend import Tmux


async def take_control(directory):
    owner_pid = os.getpid()
    status = await request(directory, "acquire_native", owner_pid=owner_pid)
    print("Waiting for the active job; terminal input opens after takeover.", flush=True)
    while status["ownership"] == "handoff_pending":
        await asyncio.sleep(0.2)
        status = await request(directory, "session_status")
        if status["error"]:
            raise BrokerError("handoff_failed", status["error"])
    if status["ownership"] != "native" or status["native_interval"].get("owner_pid") != owner_pid:
        raise BrokerError("handoff_failed", "Native ownership was not granted to this terminal")
    return status


def main():
    try:
        if not sys.stdin.isatty():
            raise BrokerError("terminal_required", "Takeover needs a real tmux terminal")
        status = asyncio.run(take_control(Path(sys.argv[1]).resolve()))
        print("Native control. Return at a fresh prompt with ::innovus_mcp::release_native", flush=True)
        env = os.environ.copy()
        env.pop("TMUX", None)
        backend = Tmux(status["tmux"], status["tmux_socket"])
        args = backend.attach_args(readonly=False)
        # Keep this PID across exec so the broker can reserve an in-flight attachment.
        os.execvpe(args[0], args, env)
    except (BrokerError, OSError) as exc:
        print(f"{getattr(exc, 'code', 'error')}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
