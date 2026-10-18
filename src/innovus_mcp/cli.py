from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .common import (
    DEFAULT_PROMPT,
    BrokerError,
    atomic_json,
    default_session_root,
    request,
    runtime_dir,
    session_dir,
)
from .tmux_backend import Tmux


def parser():
    result = argparse.ArgumentParser(description="Shared tmux-hosted Innovus sessions and FastMCP")
    result.add_argument(
        "--root",
        default=str(default_session_root()),
        help="Session root (default: INNOVUS_MCP_ROOT or ~/.local/innovus_MCP/sessions)",
    )
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List running sessions under the selected session root")
    for action in ("start", "status", "serve", "attach", "stop", "delete"):
        command = commands.add_parser(
            action,
            **(
                {"help": "Permanently remove a stopped session's logs and job artifacts"}
                if action == "delete"
                else {}
            ),
        )
        if action in {"stop", "delete"}:
            targets = command.add_mutually_exclusive_group(required=action == "delete")
            targets.add_argument(
                "--session", default="default", help="Session name (stop defaults to default)"
            )
            targets.add_argument(
                "--all", action="store_true", help="Apply to all sessions under the selected root"
            )
        else:
            command.add_argument("--session", default="default")
        if action == "start":
            command.add_argument("--cwd", default=os.getcwd())
            command.add_argument(
                "--gui", action="store_true", help="Start Innovus with its GUI (default: no GUI)"
            )
            local_tmux = Path(__file__).resolve().parents[2] / ".tools" / "tmux" / "tmux"
            command.add_argument(
                "--tmux",
                default=os.environ.get("INNOVUS_MCP_TMUX")
                or shutil.which("tmux")
                or (str(local_tmux) if local_tmux.exists() else "tmux"),
            )
            command.add_argument("--prompt", default=DEFAULT_PROMPT)
            command.add_argument("--startup-timeout", type=float, default=180)
            command.add_argument("--queue-size", type=int, default=32)
            command.add_argument("command", nargs=argparse.REMAINDER)
        if action == "stop":
            command.add_argument("--force", action="store_true")
    return result


def start(args, directory):
    if directory.exists():
        raise BrokerError(
            "session_exists", f"Session/artifacts already exist: {directory}. Use another name."
        )
    executable = shutil.which(os.path.expanduser(args.tmux))
    if not executable:
        raise BrokerError("tmux_missing", "Install tmux >=3.2 or pass --tmux /path/to/tmux")
    executable = os.path.abspath(executable)
    version = subprocess.check_output([executable, "-V"], text=True).strip()
    match = re.search(r"(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (3, 2):
        raise BrokerError("tmux_version", f"{version} is too old; use tmux >=3.2")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    command = command or ["innovus"]
    command[0] = os.path.expanduser(command[0])
    if Path(command[0]).name == "innovus":
        command = [command[0], *(arg for arg in command[1:] if arg != "-no_gui")]
        if not args.gui:
            command.append("-no_gui")
    if not shutil.which(command[0]):
        raise BrokerError("executable_missing", command[0])
    # Preserve launcher/venv symlinks: their invoked path can determine tool behavior.
    command[0] = os.path.abspath(shutil.which(command[0]))
    working_directory = Path(args.cwd).expanduser().resolve()
    if not working_directory.is_dir():
        raise BrokerError("invalid_cwd", args.cwd)
    if args.queue_size < 1 or args.startup_timeout <= 0:
        raise BrokerError("invalid_setting", "Queue size and startup timeout must be positive")
    re.compile(args.prompt)
    directory.mkdir(parents=True)
    identity = uuid.uuid4().hex
    runtime = runtime_dir(identity)
    config = dict(
        name=args.session,
        session_id=identity,
        token=uuid.uuid4().hex,
        command=command,
        cwd=str(working_directory),
        tmux=executable,
        broker_socket=str(runtime / "broker.sock"),
        tmux_socket=str(runtime / "tmux.sock"),
        health_socket=str(runtime / "health.sock"),
        prompt=args.prompt,
        startup_timeout=args.startup_timeout,
        queue_size=args.queue_size,
        state="starting",
    )
    atomic_json(directory / "session.json", config)
    with (directory / "broker.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "innovus_mcp.broker", str(directory)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    print(
        json.dumps(
            {
                "session": args.session,
                "broker_pid": process.pid,
                "state": "starting",
                "artifacts": str(directory),
                "attach_command": Tmux(executable, config["tmux_socket"]).attach_command(),
            },
            indent=2,
        )
    )


def load_session_config(directory):
    if directory.is_symlink():
        raise BrokerError("invalid_session_directory", "Refusing a symlinked session directory")
    if not directory.exists():
        raise BrokerError("unknown_session", f"Session directory does not exist: {directory}")
    if not directory.is_dir() or directory.resolve().parent != directory.parent.resolve():
        raise BrokerError("invalid_session_directory", "Expected a direct child of the session root")
    manifest = directory / "session.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise BrokerError("invalid_session_directory", "Expected a regular session.json manifest")
    config = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(config.get("session_id", ""))):
        raise BrokerError("invalid_session_directory", "Invalid session manifest")
    return config


def session_directories(root):
    if not root.exists():
        return []
    return sorted(
        (path for path in root.iterdir() if not path.is_symlink() and path.is_dir()),
        key=lambda path: path.name,
    )


async def running_session(directory):
    config = load_session_config(directory)
    for key in ("broker_socket", "tmux_socket", "tmux"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise BrokerError("invalid_session_directory", f"Session manifest is missing {key}")
    backend = Tmux(config["tmux"], config["tmux_socket"])

    async def pane_status():
        # A retained tmux server/dead pane does not mean Innovus is still running.
        if not Path(config["tmux_socket"]).exists():
            return False, None
        output = await backend.cmd(
            "display-message",
            "-p",
            "-t",
            config.get("pane_id", "%0"),
            "#{pane_dead}|#{pane_pid}",
            timeout=2,
        )
        dead, pid = output.split("|", 1)
        if dead not in {"0", "1"}:
            raise ValueError("Unrecognized tmux pane status")
        return dead == "0", int(pid)

    status, pane = await asyncio.gather(
        asyncio.wait_for(request(directory, "session_status"), timeout=2),
        pane_status(),
        return_exceptions=True,
    )
    if not isinstance(status, Exception) and (
        not isinstance(status, dict) or status.get("session_id") != config["session_id"]
    ):
        status = BrokerError("invalid_status", "Broker returned an unexpected session identity")
    broker_available = not isinstance(status, Exception)
    pane_alive, pane_pid = (None, None) if isinstance(pane, Exception) else pane
    errors = []
    if isinstance(pane, Exception):
        errors.append({"session": directory.name, "error": f"Cannot confirm tmux pane: {pane}"})
    # Include startup before its pane exists, and orphaned live panes after broker loss.
    if pane_alive is not True and not (broker_available and status.get("state") == "starting"):
        return None, errors
    summary = (
        dict(status)
        if broker_available
        else {
            "session_id": config["session_id"],
            "state": "unavailable",
            "ownership": None,
            "agent_paused": None,
            "active_job": None,
            "queue": None,
            "error": "broker_unavailable",
            "tmux": config["tmux"],
            "tmux_socket": config["tmux_socket"],
            "attach_command": backend.attach_command(),
        }
    )
    summary.update(
        session=directory.name,
        artifacts=str(directory),
        cwd=config.get("cwd"),
        command=config.get("command"),
        recorded_state=config.get("state"),
        broker_available=broker_available,
        pane_alive=pane_alive,
        pane_pid=pane_pid,
    )
    if not broker_available:
        summary["broker_error"] = str(status) or type(status).__name__
    return summary, errors


async def list_sessions(root):
    root = Path(root).expanduser().resolve()
    response = {"root": str(root), "sessions": [], "count": 0, "errors": []}
    if not root.exists():
        return response
    semaphore = asyncio.Semaphore(8)

    async def inspect(directory):
        async with semaphore:
            try:
                session_dir(root, directory.name)
                return await running_session(directory)
            except (BrokerError, OSError, ValueError, TypeError, KeyError) as exc:
                return None, [{"session": directory.name, "error": str(exc)}]

    directories = session_directories(root)
    for summary, errors in await asyncio.gather(*(inspect(directory) for directory in directories)):
        if summary is not None:
            response["sessions"].append(summary)
        response["errors"].extend(errors)
    response["count"] = len(response["sessions"])
    return response


async def endpoint_is_live(address):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(address), timeout=1)
    except TimeoutError as exc:
        raise BrokerError("delete_unconfirmed", f"Timed out checking session endpoint {address}") from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            return False
        raise BrokerError("delete_unconfirmed", f"Cannot check session endpoint {address}: {exc}") from exc
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def confirm_stopped(config):
    if config.get("state") != "stopped":
        raise BrokerError("session_not_stopped", "Stop the session before deleting its artifacts")
    endpoints = [config.get(key) for key in ("broker_socket", "tmux_socket")]
    if not all(isinstance(address, str) and address for address in endpoints):
        raise BrokerError("invalid_session_directory", "Session manifest is missing runtime endpoints")
    # stop acknowledges before its broker exits; allow that short shutdown interval.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while True:
        if not any(await asyncio.gather(*(endpoint_is_live(address) for address in endpoints))):
            break
        if loop.time() >= deadline:
            raise BrokerError("session_running", "Broker or tmux is still running")
        await asyncio.sleep(0.05)


async def delete_session(directory):
    config = load_session_config(directory)
    await confirm_stopped(config)
    # rmtree does not follow symlinks contained in the session's artifact tree.
    shutil.rmtree(directory)
    return {"session": directory.name, "state": "deleted", "artifacts": str(directory)}


async def execute_all(args):
    root = Path(args.root).expanduser().resolve()
    directories = session_directories(root)
    semaphore = asyncio.Semaphore(8)

    async def apply(directory):
        async with semaphore:
            entry = {"session": directory.name}
            try:
                session_dir(root, directory.name)
                config = load_session_config(directory)
                if args.action == "stop" and config.get("state") == "stopped":
                    await confirm_stopped(config)
                    return {**entry, "status": "skipped", "reason": "already_stopped"}
                if args.action == "delete":
                    result = await delete_session(directory)
                else:
                    result = await request(directory, "stop", force=args.force)
                return {**entry, "status": "succeeded", "result": result}
            except (BrokerError, OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
                return {
                    **entry,
                    "status": "failed",
                    "error": {
                        "code": getattr(exc, "code", "error"),
                        "message": str(exc) or type(exc).__name__,
                    },
                }

    results = await asyncio.gather(*(apply(directory) for directory in directories))
    return {
        "root": str(root),
        "action": args.action,
        "results": results,
        **{
            status: sum(entry["status"] == status for entry in results)
            for status in ("succeeded", "skipped", "failed")
        },
    }


async def execute(args, directory):
    if args.action == "delete":
        response = await delete_session(directory)
        print(json.dumps(response, indent=2, ensure_ascii=True))
        return
    if args.action == "attach":
        if not sys.stdin.isatty():
            raise BrokerError("terminal_required", "tmux attachment needs a real terminal")
        status = await request(directory, "session_status")
        print("Read-only terminal. Press Ctrl+B, then h to take control.", file=sys.stderr)
        backend = Tmux(status["tmux"], status["tmux_socket"])
        env = os.environ.copy()
        env.pop("TMUX", None)
        process = await asyncio.create_subprocess_exec(*backend.attach_args(readonly=True), env=env)
        if await process.wait():
            raise BrokerError("attach_failed", "tmux attach failed")
        return
    response = await request(
        directory,
        "stop" if args.action == "stop" else "session_status",
        **({"force": args.force} if args.action == "stop" else {}),
    )
    print(json.dumps(response, indent=2, ensure_ascii=True))


def main():
    args = parser().parse_args()
    try:
        if args.action == "list":
            print(json.dumps(asyncio.run(list_sessions(args.root)), indent=2, ensure_ascii=True))
            return
        if getattr(args, "all", False):
            response = asyncio.run(execute_all(args))
            print(json.dumps(response, indent=2, ensure_ascii=True))
            if response["failed"]:
                raise SystemExit(1)
            return
        directory = session_dir(args.root, args.session)
        if args.action == "start":
            start(args, directory)
        elif args.action == "serve":
            from .mcp_server import create_server

            create_server(directory).run(transport="stdio", show_banner=False)
        else:
            asyncio.run(execute(args, directory))
    except (BrokerError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'error')}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
