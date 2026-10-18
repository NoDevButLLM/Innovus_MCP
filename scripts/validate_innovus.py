"""Opt-in, real Innovus validation through FastMCP and a native tmux terminal.

Run against a fresh, idle disposable session; no chip design is required.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from innovus_mcp.common import default_session_root
from innovus_mcp.tmux_backend import Tmux

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from native_terminal import NativeTerminal  # noqa: E402


def compact(value):
    if isinstance(value, str) and len(value) > 2000:
        return {
            "utf8_bytes": len(value.encode()),
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
            "prefix": value[:160],
            "suffix": value[-80:],
        }
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


async def validate(args):
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"session": args.session, "root": str(args.root), "calls": [], "cases": {}, "connections": []}
    trace = args.artifacts / "mcp-trace.jsonl"
    trace.write_text("")
    transport = StdioTransport(
        command=sys.executable,
        keep_alive=False,
        args=["-m", "innovus_mcp", "--root", str(args.root), "serve", "--session", args.session],
    )

    async def call(client, case, tool, params, *, audit=False, expect_error=False):
        start = time.perf_counter()
        result = await client.call_tool(tool, params, raise_on_error=False)
        elapsed = (time.perf_counter() - start) * 1000
        row = {
            "case": case,
            "tool": tool,
            "arguments": params,
            "audit": audit,
            "duration_ms": round(elapsed, 3),
            "is_error": result.is_error,
            "text_bytes": sum(len(c.text.encode()) for c in result.content if hasattr(c, "text")),
            "data": compact(result.data),
            "error_text": [c.text for c in result.content if hasattr(c, "text")] if result.is_error else [],
        }
        report["calls"].append(row)
        with trace.open("a") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        assert result.is_error == expect_error, row
        return result.data if not result.is_error else row

    def record(name, **values):
        rows = [r for r in report["calls"] if r["case"] == name and not r["audit"]]
        values.update(
            tool_calls=len(rows),
            tool_duration_ms=round(sum(r["duration_ms"] for r in rows), 3),
            flow=[r["tool"] for r in rows],
            passed=True,
        )
        report["cases"][name] = values
        (args.artifacts / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"case": name, **values}, ensure_ascii=False), flush=True)

    def query(script, **options):
        return {"script": script, "request_id": uuid.uuid4().hex, "wait_ms": 500, **options}

    async def connected():
        started = time.perf_counter()
        client = Client(transport, timeout=30)
        await client.__aenter__()
        report["connections"].append({"connect_ms": round((time.perf_counter() - started) * 1000, 3)})
        return client

    client = await connected()
    try:
        status = await call(client, "setup", "session_status", {}, audit=True)
        assert status["state"] == "ready" and status["ownership"] == "managed"
        assert not status["active_job"] and not status["queue"] and not status["agent_paused"]
        tools = await client.list_tools()
        report["tools"] = [t.model_dump(mode="json", exclude_none=True) for t in tools]

        quick_times = []
        for _ in range(5):
            first = await call(client, "quick_help", "query_tcl", query("help dbGet"))
            assert first["state"] == "succeeded" and first["terminal"]
            assert not first["output"]["has_more"] and first["result_bytes"] == 0
            assert (
                first["result"]["text"] == "" and first["error_info_bytes"] == first["error_code_bytes"] == 0
            )
            assert "head" not in first and "tail" not in first and "output_tail" not in first
            quick_times.append(report["calls"][-1]["duration_ms"])
        help_page = await call(
            client, "quick_help", "read_job_output", {"job_id": first["job_id"], "view": "base64"}, audit=True
        )
        help_raw = base64.b64decode(help_page["base64"])
        (args.artifacts / "dbget-help.txt").write_text(first["output"]["text"])
        record(
            "quick_help",
            command="help dbGet",
            repetitions=5,
            output_bytes=first["output_bytes"],
            result_bytes=0,
            median_ms=statistics.median(quick_times),
            min_ms=min(quick_times),
            max_ms=max(quick_times),
            calls_per_command=1,
        )

        for name, script in [("quick_result", "info patchlevel"), ("dbget_schema", "dbGet head.?")]:
            value = await call(client, name, "query_tcl", query(script))
            assert value["state"] == "succeeded" and not value["result"]["has_more"]
            assert not value["output"]["has_more"]
            record(
                name,
                command=script,
                output_bytes=value["output_bytes"],
                result_bytes=value["result_bytes"],
                result=compact(value["result"]["text"]),
                output=compact(value["output"]["text"]),
            )

        mutation = await call(
            client, "quick_mutation", "mutate_tcl", query("set ::imcp_validation_shared agent_initial")
        )
        assert mutation["state"] == "succeeded" and mutation["result"]["text"] == "agent_initial"
        record("quick_mutation", command="set ::imcp_validation_shared agent_initial", result_bytes=13)

        slow_args = query(
            "after 2500; puts {long-job-done}; set ::imcp_validation_long finished", wait_ms=100
        )
        slow = await call(client, "long_job", "query_tcl", slow_args)
        assert not slow["terminal"] and slow["result_bytes"] is None
        assert not slow["output"]["has_more"] and not slow["output"]["text"]
        queued_args = query("info patchlevel", wait_ms=50)
        queued = await call(client, "queued_retry", "query_tcl", queued_args)
        assert queued["state"] == "queued"
        retry = await call(client, "queued_retry", "query_tcl", {**queued_args, "wait_ms": 0})
        assert retry["job_id"] == queued["job_id"]
        done = await call(client, "long_job", "get_job", {"job_id": slow["job_id"], "wait_ms": 10000})
        assert done["state"] == "succeeded" and done["result"]["text"] == "finished"
        queued_done = await call(
            client, "queued_retry", "get_job", {"job_id": queued["job_id"], "wait_ms": 10000}
        )
        assert queued_done["state"] == "succeeded"
        record(
            "long_job",
            command=slow_args["script"],
            initial_state=slow["state"],
            final_state=done["state"],
            broker_lifecycle_ms=round((done["finished_at"] - done["submitted_at"]) * 1000, 3),
        )
        record("queued_retry", command=queued_args["script"], initial_state="queued", same_job_on_retry=True)

        console_script = "puts -nonewline [string repeat 0123456789abcdef 65536]"
        console = await call(
            client, "large_console", "query_tcl", query(console_script, max_output_bytes=4096)
        )
        initial = console
        if not console["terminal"]:
            console = await call(
                client,
                "large_console",
                "get_job",
                {"job_id": console["job_id"], "wait_ms": 10000, "max_output_bytes": 0, "max_result_bytes": 0},
            )
        assert console["state"] == "succeeded" and console["output_bytes"] == 1048576
        output = initial["output"]["text"].encode()
        offset = initial["output"]["next_offset"]
        pages = []
        while offset < console["output_bytes"]:
            page = await call(
                client,
                "large_console",
                "read_job_output",
                {"job_id": console["job_id"], "offset": offset, "max_bytes": 1000000, "view": "base64"},
            )
            data = base64.b64decode(page["base64"])
            assert 0 < len(data) <= 65536
            assert page["next_offset"] == offset + len(data)
            output += data
            offset = page["next_offset"]
            pages.append(len(data))
        assert output == b"0123456789abcdef" * 65536
        summary = await call(client, "large_console", "get_job", {"job_id": console["job_id"]}, audit=True)
        assert summary["summary_truncated"]
        assert summary["output"]["next_offset"] <= summary["output_tail"]["offset"]
        record(
            "large_console",
            command=console_script,
            output_bytes=len(output),
            result_bytes=console["result_bytes"],
            initial_inline_bytes=initial["output"]["next_offset"],
            page_sizes=pages,
            sha256=hashlib.sha256(output).hexdigest(),
            summary_ranges=[
                [summary["output"]["offset"], summary["output"]["next_offset"]],
                [summary["output_tail"]["offset"], summary["output_tail"]["next_offset"]],
            ],
        )

        script = "for {set ::imcp_validation_i 0} {$::imcp_validation_i < 64} {incr ::imcp_validation_i} {help dbGet; after 20}; unset ::imcp_validation_i"
        streaming = await call(
            client,
            "streaming_help",
            "query_tcl",
            query(script, wait_ms=80, max_output_bytes=0, max_result_bytes=0),
        )
        assert not streaming["terminal"]
        final = await call(
            client,
            "streaming_help",
            "get_job",
            {"job_id": streaming["job_id"], "wait_ms": 10000, "max_output_bytes": 0, "max_result_bytes": 0},
        )
        assert final["state"] == "succeeded" and final["result_bytes"] == 0
        offset, output, pages = 0, b"", []
        while offset < final["output_bytes"]:
            page = await call(
                client,
                "streaming_help",
                "read_job_output",
                {"job_id": final["job_id"], "offset": offset, "max_bytes": 65536, "view": "base64"},
            )
            data = base64.b64decode(page["base64"])
            assert data and len(data) <= 65536
            output += data
            offset = page["next_offset"]
            pages.append(len(data))
        assert output == help_raw * 64
        record(
            "streaming_help",
            command=script,
            output_bytes=len(output),
            page_sizes=pages,
            broker_lifecycle_ms=round((final["finished_at"] - final["submitted_at"]) * 1000, 3),
            sha256=hashlib.sha256(output).hexdigest(),
        )

        result_script = "string repeat 0123456789abcdef 32768"
        large_result = await call(client, "large_result", "query_tcl", query(result_script))
        assert large_result["state"] == "succeeded" and large_result["output_bytes"] == 0
        assert large_result["result_bytes"] == 524288
        output = large_result["result"]["text"].encode()
        offset, pages = large_result["result"]["next_offset"], []
        while offset < large_result["result_bytes"]:
            page = await call(
                client,
                "large_result",
                "read_job_result",
                {"job_id": large_result["job_id"], "offset": offset, "max_bytes": 1000000, "view": "base64"},
            )
            data = base64.b64decode(page["base64"])
            assert 0 < len(data) <= 65536
            output += data
            offset = page["next_offset"]
            pages.append(len(data))
        assert output == b"0123456789abcdef" * 32768
        record(
            "large_result",
            command=result_script,
            output_bytes=0,
            result_bytes=len(output),
            initial_inline_bytes=large_result["result"]["next_offset"],
            page_sizes=pages,
            sha256=hashlib.sha256(output).hexdigest(),
        )

        failed = await call(
            client, "tcl_error", "query_tcl", query("error {validation deliberate Tcl failure}")
        )
        assert failed["state"] == "failed" and failed["tcl_code"] == 1
        assert failed["error_info_bytes"] > 0 and failed["error_code_bytes"] > 0
        details = await call(
            client, "tcl_error", "read_job_result", {"job_id": failed["job_id"], "artifact": "error_info"}
        )
        assert "validation deliberate Tcl failure" in details["text"]
        record(
            "tcl_error",
            command="error {validation deliberate Tcl failure}",
            final_state="failed",
            result_bytes=failed["result_bytes"],
            error_info_bytes=failed["error_info_bytes"],
            error_code_bytes=failed["error_code_bytes"],
        )
        printed = await call(
            client,
            "printed_error",
            "query_tcl",
            query("puts {**ERROR: validation printed diagnostic}; set ::imcp_validation_diagnostic ok"),
        )
        assert printed["state"] == "succeeded" and printed["error_count"] == 1
        record("printed_error", command=printed["preview"], final_state="succeeded", error_count=1)

        async def ownership(expected):
            for _ in range(200):
                current = await call(client, "native", "session_status", {}, audit=True)
                assert not current["error"], current
                if current["ownership"] == expected:
                    return current
                await asyncio.sleep(0.025)
            raise AssertionError(f"ownership did not become {expected}")

        backend = Tmux(status["tmux"], status["tmux_socket"])

        async def writers(expected):
            for _ in range(150):
                clients = await backend.clients()
                if clients and sum(c["readonly"] == "0" for c in clients) == expected:
                    return clients
                await asyncio.sleep(0.025)
            raise AssertionError(f"writer count did not become {expected}")

        async with NativeTerminal(backend.attach_args(readonly=True)) as terminal:
            await writers(0)
            terminal.write(b"set ::imcp_validation_readonly blocked\r")
            await asyncio.sleep(0.1)
            absent = await call(
                client, "native", "query_tcl", query("info exists ::imcp_validation_readonly")
            )
            assert absent["result"]["text"] == "0"
            handoff_job = await call(
                client,
                "native",
                "query_tcl",
                query("after 1500; set ::imcp_validation_handoff done", wait_ms=0),
            )
            for _ in range(100):
                active = await call(
                    client,
                    "native",
                    "get_job",
                    {"job_id": handoff_job["job_id"], "max_output_bytes": 0, "max_result_bytes": 0},
                    audit=True,
                )
                if active["state"] == "running":
                    break
                assert not active["terminal"]
                await asyncio.sleep(0.01)
            assert active["state"] == "running"
            terminal.write(b"\x02h")
            await ownership("handoff_pending")
            blocked = await call(client, "native", "query_tcl", query("info patchlevel"), expect_error=True)
            assert any("native_control" in t for t in blocked["error_text"])
            native = await ownership("native")
            await writers(1)
            assert (await call(client, "native", "get_job", {"job_id": handoff_job["job_id"]}))["terminal"]
            terminal.write(b"set ::imcp_validation_shared human_value\r")
            await asyncio.sleep(0.15)
            terminal.write(b"::innovus_mcp::release_native\r")
            returned = await ownership("managed")
            assert not returned["agent_paused"]
            await writers(0)
            shared = await call(client, "native", "query_tcl", query("set ::imcp_validation_shared"))
            assert shared["result"]["text"] == "human_value"
            terminal.write(b"\x02h")
            second_native = await ownership("native")
            await writers(1)
        disconnected = await ownership("native")
        assert disconnected["agent_paused"]
        async with NativeTerminal(backend.attach_args(readonly=True)) as terminal:
            await writers(0)
            terminal.write(b"\x02h")
            await ownership("native")
            await writers(1)
            reattached = await ownership("native")
            assert (
                reattached["native_interval"]["native_interval_id"]
                == second_native["native_interval"]["native_interval_id"]
            )
            terminal.write(b"::innovus_mcp::release_native\r")
            returned = await ownership("managed")
            assert not returned["agent_paused"]
            await writers(0)
        transcript = await call(
            client,
            "native",
            "read_session_output",
            {"native_interval_id": native["native_interval"]["native_interval_id"], "max_bytes": 65536},
            audit=True,
        )
        assert "human_value" in transcript["text"]
        record(
            "native",
            direct_tmux=True,
            readonly_input_ignored=True,
            waits_for_active_job=True,
            agent_blocked_during_native=True,
            automatic_resume=True,
            retained_readonly_connection=True,
            shared_value="human_value",
            disconnected_native_stays_paused=True,
            reattach_same_interval=True,
        )

        disconnect_args = query("after 1500; set ::imcp_validation_disconnect survived", wait_ms=50)
        in_flight = await call(client, "mcp_reconnect", "query_tcl", disconnect_args)
        assert not in_flight["terminal"]
    finally:
        await client.__aexit__(None, None, None)

    assert transport._connect_task is None, "MCP subprocess must actually disconnect"
    client = await connected()
    try:
        survived = await call(
            client, "mcp_reconnect", "get_job", {"job_id": in_flight["job_id"], "wait_ms": 10000}
        )
        assert survived["state"] == "succeeded" and survived["result"]["text"] == "survived"
        record(
            "mcp_reconnect",
            command=disconnect_args["script"],
            same_job_id=survived["job_id"] == in_flight["job_id"],
        )
        final_status = await call(client, "setup", "session_status", {}, audit=True)
        assert (
            final_status["state"] == "ready" and not final_status["queue"] and not final_status["active_job"]
        )
        assert not final_status["agent_paused"] and not final_status["output_capture_incomplete"]
        report["final_status"] = final_status
        report["passed"] = True
        (args.artifacts / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await client.__aexit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "--root", type=lambda value: Path(value).expanduser().resolve(), default=default_session_root()
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    asyncio.run(validate(parser.parse_args()))


if __name__ == "__main__":
    main()
