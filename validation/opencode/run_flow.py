"""Real OpenCode -> FastMCP -> broker -> tmux -> Innovus acceptance flow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from innovus_mcp.common import BrokerError, request

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path.home() / ".local" / "innovus_MCP" / "validation"


def default_tmux():
    return os.environ.get("INNOVUS_MCP_TEST_TMUX") or os.environ.get("INNOVUS_MCP_TMUX") or "tmux"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def execute(command, *, cwd, timeout=180, env=None):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": True}
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}


async def ready(directory, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = await request(directory, "session_status")
            if state["state"] == "ready":
                return state
            if state["state"] in {"startup_failed", "unavailable"}:
                raise RuntimeError(f"Innovus startup: {state.get('error')}")
        except BrokerError as exc:
            if exc.code != "broker_unavailable":
                raise
        await asyncio.sleep(0.2)
    raise TimeoutError("Innovus startup deadline exceeded")


def transcript(data):
    """Readable export of observable messages and tools; reasoning blocks are omitted."""
    lines = ["# OpenCode 实测对话与工具过程", ""]
    for message in data["messages"]:
        if message["type"] == "user":
            lines.extend(["## 用户", "", message.get("text", ""), ""])
        else:
            for block in message.get("content", []):
                if block["type"] == "text":
                    lines.extend(["## Agent", "", block.get("text", ""), ""])
                elif block["type"] == "tool":
                    lines.extend(
                        [
                            f"### 工具 {block['name']}",
                            "",
                            "```json",
                            json.dumps(block, ensure_ascii=False, indent=2),
                            "```",
                            "",
                        ]
                    )
    return "\n".join(lines)


def analyze(case, workspace, exported, rows, elapsed):
    tools, answer, nested_tools = [], [], []
    for message in exported["messages"]:
        for block in message.get("content", []):
            if block["type"] == "tool":
                tools.append(block)
                nested_tools.extend(block.get("state", {}).get("metadata", {}).get("toolCalls", []))
            elif block["type"] == "text":
                answer.append(block.get("text", ""))
    shells = [t for t in tools if t["name"] in {"shell", "bash", "terminal"}]
    frontend_errors = [
        {
            "tool": tool["name"],
            "messages": [part.get("text", "")[:500] for part in tool.get("state", {}).get("content", [])],
        }
        for tool in tools
        if tool.get("state", {}).get("metadata", {}).get("error")
        and not any(
            call["tool"].startswith("innovus.")
            for call in tool.get("state", {}).get("metadata", {}).get("toolCalls", [])
        )
    ]
    checks = {
        "within_mcp_call_budget": 1 <= len(rows) <= case.get("max_mcp_calls", 1),
        "no_shell": not shells,
        "has_answer": bool("".join(answer).strip()),
        "has_session_export": bool(exported.get("info", {}).get("id")),
        "correct_project": Path(
            exported.get("info", {}).get("directory")
            or exported.get("info", {}).get("location", {}).get("directory", "")
        ).resolve()
        == workspace,
        "single_help_submission": sum(r["tool"] == "command_help" for r in rows) == 1,
        "only_help_and_pages": bool(rows)
        and all(
            r["tool"]
            in (
                {"command_help", "read_job_result", "read_job_output"}
                if case.get("allow_paging")
                else {"command_help"}
            )
            for r in rows
        ),
        "no_mcp_tool_errors": all(not r["is_error"] for r in rows),
        "trace_matches_export": len(rows) == sum(t["tool"].startswith("innovus.") for t in nested_tools),
    }
    if rows:
        result = rows[0].get("data") or {}
        help_info = result.get("help", {})
        checks.update(
            correct_command=help_info.get("command") == case["command"],
            correct_acquisition_status=help_info.get("status") == case["status"],
            terminal=result.get("terminal") is True,
        )
        if case.get("detail"):
            checks["correct_detail"] = help_info.get("detail") == case["detail"]
        if case["status"] == "ok":
            checks["complete"] = help_info.get("complete") is True
        pages = {artifact: result.get(artifact, {}) for artifact in ("output", "result")}
        valid_paging = True
        for row in rows[1:]:
            artifact = (
                "output" if row["tool"] == "read_job_output" else row["arguments"].get("artifact", "result")
            )
            previous = pages.get(artifact, {})
            valid_paging &= (
                previous.get("has_more", False)
                and row["arguments"].get("offset", 0) == previous.get("next_offset")
                and row["arguments"].get("job_id") == result.get("job_id")
            )
            pages[artifact] = row.get("data") or {}
        checks["only_needed_contiguous_pages"] = bool(valid_paging)
        checks["content_pages_complete"] = all(not page.get("has_more", False) for page in pages.values())
    return {
        "case": workspace.name,
        "prompt": case["prompt"],
        "session_id": exported.get("info", {}).get("id"),
        "mcp_calls": len(rows),
        "mcp_flow": [r["tool"] for r in rows],
        "shell_calls": len(shells),
        "opencode_outer_tools": len(tools),
        "other_nested_tools": [t["tool"] for t in nested_tools if not t["tool"].startswith("innovus.")],
        "opencode_frontend_errors": frontend_errors,
        "mcp_duration_ms": round(sum(r["duration_ms"] for r in rows), 3),
        "agent_wall_ms": round(elapsed * 1000, 3),
        "checks": checks,
        "passed": all(checks.values()),
    }


async def run_case(args, run_dir, case, repetition):
    workspace = run_dir / f"{case['name']}-{repetition}"
    workspace.mkdir()
    root = workspace / "sessions"
    name = "acceptance"
    directory = root / name
    trace = workspace / "mcp-trace.jsonl"
    command = [sys.executable, "-m", "innovus_mcp", "--root", str(root)]
    config = {
        "$schema": "https://opencode.ai/config.json",
        "instructions": [str(HERE / "AGENT_GUIDE.md")],
        "mcp": {
            "innovus": {
                "type": "local",
                "command": [
                    sys.executable,
                    str(HERE / "trace_server.py"),
                    "--directory",
                    str(directory),
                    "--trace",
                    str(trace),
                ],
                "enabled": True,
                "timeout": 30000,
            }
        },
    }
    write_json(workspace / "opencode.json", config)
    shutil.copyfile(HERE / "AGENT_GUIDE.md", workspace / "AGENTS.md")
    # OpenCode may otherwise use the enclosing repository's config and project directory.
    initialized = execute(["git", "init", "--quiet"], cwd=workspace)
    if initialized["returncode"]:
        raise RuntimeError(initialized["stderr"])
    env = {**os.environ, "PWD": str(workspace), "OPENCODE_CONFIG": str(workspace / "opencode.json")}
    started = execute(
        [
            *command,
            "start",
            "--session",
            name,
            "--cwd",
            str(workspace),
            "--tmux",
            args.tmux,
            "--",
            args.innovus,
            "-no_gui",
        ],
        cwd=workspace,
    )
    write_json(workspace / "start.json", started)
    if started["returncode"]:
        raise RuntimeError(started["stderr"])
    try:
        state = await ready(directory, args.startup_timeout)
        write_json(workspace / "audit-before.json", state)
        sources = execute([args.opencode, "debug", "config"], cwd=workspace, env=env)
        write_json(workspace / "config-sources.json", json.loads(sources["stdout"]))
        if str(HERE / "trace_server.py") not in sources["stdout"]:
            raise RuntimeError("OpenCode did not load this case's traced MCP configuration")
        created = execute(
            [
                args.opencode,
                "api",
                "POST",
                "/api/session",
                "--standalone",
                "--data",
                json.dumps(
                    {"location": {"directory": str(workspace)}, "title": f"innovus-mcp {workspace.name}"}
                ),
            ],
            cwd=workspace,
            env=env,
        )
        write_json(workspace / "create-session.json", created)
        info = json.loads(created["stdout"])
        info = info.get("data", info)
        if info.get("location", {}).get("directory") != str(workspace):
            raise RuntimeError("OpenCode session location does not match the isolated test directory")
        expected_session_id = info["id"]
        begin = time.monotonic()
        result = execute(
            [
                args.opencode,
                "run",
                "--standalone",
                "--auto",
                "--format",
                "json",
                "--model",
                args.model,
                "--session",
                expected_session_id,
                "--title",
                f"innovus-mcp {workspace.name}",
                case["prompt"],
            ],
            cwd=workspace,
            timeout=args.timeout,
            env=env,
        )
        elapsed = time.monotonic() - begin
        (workspace / "events.jsonl").write_text(result["stdout"], encoding="utf-8")
        (workspace / "opencode.stderr.log").write_text(result["stderr"], encoding="utf-8")
        write_json(
            workspace / "process.json", {k: v for k, v in result.items() if k not in {"stdout", "stderr"}}
        )
        events = [json.loads(line) for line in result["stdout"].splitlines() if line.startswith("{")]
        session_ids = {event["sessionID"] for event in events if event.get("sessionID")}
        if len(session_ids) != 1:
            raise RuntimeError(f"Expected one OpenCode session ID, found {session_ids}")
        session_id = session_ids.pop()
        if session_id != expected_session_id:
            raise RuntimeError("OpenCode used an unexpected session")
        export = execute(
            [args.opencode, "session", "export", session_id, "--standalone"],
            cwd=workspace,
            timeout=args.timeout,
            env=env,
        )
        (workspace / "export.stderr.log").write_text(export["stderr"], encoding="utf-8")
        (workspace / "session.json").write_text(export["stdout"], encoding="utf-8")
        exported = json.loads(export["stdout"])
        # Some CLI versions wrap the transfer object in a data envelope.
        if "messages" not in exported and "data" in exported:
            exported = exported["data"]
        (workspace / "session.md").write_text(transcript(exported), encoding="utf-8")
        rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        report = analyze(case, workspace, exported, rows, elapsed)
        state = await request(directory, "session_status")
        write_json(workspace / "audit-after.json", state)
        report["checks"].update(
            process_success=result["returncode"] == 0 and not result["timed_out"],
            export_success=export["returncode"] == 0,
            broker_idle=state["state"] == "ready" and not state["active_job"] and not state["queue"],
            capture_complete=not state["output_capture_incomplete"],
        )
        report["passed"] = all(report["checks"].values())
        write_json(workspace / "summary.json", report)
        return report
    finally:
        # Only this flow's disposable session is stopped; retain all artifacts.
        stopped = execute([*command, "stop", "--session", name, "--force"], cwd=workspace)
        write_json(workspace / "stop.json", stopped)


async def main(args):
    run_dir = args.output.expanduser().resolve() / (
        time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )
    run_dir.mkdir(parents=True)
    write_json(
        run_dir / "environment.json",
        {
            "python": sys.version,
            "executable": sys.executable,
            "model": args.model,
            "opencode": args.opencode,
            "innovus": args.innovus,
            "tmux": args.tmux,
            "source_sha256": {
                str(path.relative_to(HERE.parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((HERE.parents[1] / "src" / "innovus_mcp").rglob("*"))
                if path.suffix in {".py", ".tcl"}
            },
        },
    )
    cases = json.loads(args.cases.expanduser().read_text())
    reports = []
    print(f"Artifacts: {run_dir}", flush=True)
    for case in cases:
        if args.case and case["name"] not in args.case:
            continue
        for repetition in range(1, case.get("repetitions", 1) + 1):
            print(f"Running {case['name']}-{repetition}", flush=True)
            try:
                report = await run_case(args, run_dir, case, repetition)
            except Exception as exc:
                report = {
                    "case": f"{case['name']}-{repetition}",
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            reports.append(report)
            write_json(
                run_dir / "summary.json",
                {"complete": False, "passed": all(r["passed"] for r in reports), "cases": reports},
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
    lines = [
        "# OpenCode 实测结果",
        "",
        f"模型：`{args.model}`",
        "",
        "| 场景 | Innovus MCP | OpenCode 外层工具 | shell | 前端错误 | MCP 毫秒 | Agent 毫秒 | P1 通过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in reports:
        lines.append(
            f"| [{row['case']}]({row['case']}/session.md) | {row.get('mcp_calls', '-')} | "
            f"{row.get('opencode_outer_tools', '-')} | "
            f"{row.get('shell_calls', '-')} | {len(row.get('opencode_frontend_errors', []))} | "
            f"{row.get('mcp_duration_ms', '-')} | "
            f"{row.get('agent_wall_ms', '-')} | {row['passed']} |"
        )
    lines.extend(["", "调用数来自测试中间件，状态审计与启动/停止不计入 agent 业务调用。", ""])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(
        run_dir / "summary.json",
        {
            "complete": True,
            "passed": bool(reports) and all(r["passed"] for r in reports),
            "cases": reports,
        },
    )
    if not reports or not all(r["passed"] for r in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Configured OpenCode provider/model identifier")
    parser.add_argument("--opencode", default=shutil.which("opencode") or "opencode")
    parser.add_argument("--innovus", default=shutil.which("innovus") or "innovus")
    parser.add_argument("--tmux", default=default_tmux())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cases", type=Path, default=HERE / "cases.json")
    parser.add_argument("--case", action="append", help="Run selected named cases only")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--startup-timeout", type=float, default=180)
    asyncio.run(main(parser.parse_args()))
