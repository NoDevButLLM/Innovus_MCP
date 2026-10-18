"""Independent tmux pipe consumer. It survives a disconnected or failed broker."""

import json
import os
import select
import signal
import socket
import sys
import time
from pathlib import Path

from .common import atomic_json


def main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    root = Path(sys.argv[1])
    config = json.loads((root / "session.json").read_text())
    health = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    health.setblocking(False)
    status = {"pid": os.getpid(), "recorded_bytes": 0, "lost_bytes": 0, "error": None}
    output = os.open(root / "pane.raw", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    (root / "capture.ready").touch()
    last_report = 0
    try:
        while True:
            now = time.time()
            if now - last_report > 0.5:
                status["heartbeat"] = now
                data = json.dumps(status).encode()
                try:
                    health.sendto(data, config["health_socket"])
                except OSError:
                    pass
                try:
                    atomic_json(root / "capture.status", status)
                except OSError:
                    pass
                last_report = now
            ready, _, _ = select.select([0], [], [], 0.2)
            if not ready:
                continue
            chunk = os.read(0, 65536)
            if not chunk:
                break
            written = 0
            try:
                while written < len(chunk):
                    count = os.write(output, chunk[written:])
                    if not count:
                        raise OSError("zero-length capture write")
                    written += count
                status["recorded_bytes"] += written
            except OSError as exc:
                status["recorded_bytes"] += written
                status["lost_bytes"] += len(chunk) - written
                status["error"] = str(exc)
    finally:
        os.close(output)
        health.close()


if __name__ == "__main__":
    main()
