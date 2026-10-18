"""Deterministic interactive process for tmux integration tests (not a Tcl interpreter)."""

import re
import sys
import time
from pathlib import Path

root = None
token = None
counter = 1
native = None


def frame(kind, identity):
    print(f"\x1b]777;IMCP:{token}:{kind}:{identity}\x07", end="", flush=True)


def prompt():
    global counter
    print(f"\ninnovus {counter}> ", end="", flush=True)
    counter += 1


print("FAKE STARTUP LOG", flush=True)
prompt()
for line in sys.stdin:
    try:
        if "::innovus_mcp::init" in line:
            args = re.search(r'::innovus_mcp::init "(.*?)" (\w+) (\w+)', line)
            root, token, identity = Path(args[1]), args[2], args[3]
            frame("READY", identity)
        elif "::innovus_mcp::ping" in line:
            frame("READY", line.split()[-1])
        elif "::innovus_mcp::set_native" in line:
            native = line.split()[-1]
            frame("READY", native)
        elif "::innovus_mcp::release_native" in line:
            frame("RELEASE", native)
        elif "::innovus_mcp::run" in line:
            identity = line.split()[-1]
            job = root / "jobs" / identity
            script = (job / "request.tcl").read_text()
            frame("START", identity)
            if "SLEEP:" in script:
                time.sleep(float(re.search(r"SLEEP:([\d.]+)", script)[1]))
            if "OUTPUT:" in script:
                length = int(re.search(r"OUTPUT:(\d+)", script)[1])
                while length:
                    n = min(length, 32768)
                    sys.stdout.write("x" * n)
                    sys.stdout.flush()
                    length -= n
            if "PROMPT_TEXT" in script:
                print("\ninnovus 999> ", end="", flush=True)
                time.sleep(0.5)
                print("still executing", flush=True)
            result = "x" * int(re.search(r"RESULT:(\d+)", script)[1]) if "RESULT:" in script else "ok"
            (job / "result.txt").write_text(result)
            (job / "error_info.txt").write_text("synthetic error" if "ERROR" in script else "")
            (job / "error_code.txt").write_text("FAKE" if "ERROR" in script else "")
            (job / "completion.meta").write_text("version=1\ncode=" + ("1" if "ERROR" in script else "0"))
            frame("DONE", identity)
        elif line.strip() == "exit":
            break
        else:
            print("native output: " + line.strip(), flush=True)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
    prompt()
