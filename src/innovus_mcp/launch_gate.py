"""Executed inside tmux, before Innovus, to establish capture before startup."""

import json
import os
import sys
import time
from pathlib import Path


def main():
    root = Path(sys.argv[1])
    config = json.loads((root / "session.json").read_text())
    while not (root / "capture.ready").exists():
        time.sleep(0.02)
    os.write(1, f"\x1b]777;IMCP:{config['token']}:CAPTURE:startup\x07".encode())
    while not (root / "launch.ready").exists():
        time.sleep(0.02)
    os.chdir(config["cwd"])
    os.execvpe(config["command"][0], config["command"], os.environ)


if __name__ == "__main__":
    main()
