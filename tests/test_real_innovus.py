"""Opt in with INNOVUS_MCP_REAL_SESSION pointing at an idle disposable broker session."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

from innovus_mcp.common import request

pytestmark = pytest.mark.skipif(
    not os.environ.get("INNOVUS_MCP_REAL_SESSION"),
    reason="Set INNOVUS_MCP_REAL_SESSION to a disposable live Innovus session",
)


async def run(script, intent="query"):
    directory = Path(os.environ["INNOVUS_MCP_REAL_SESSION"]).resolve()
    job = await request(
        directory, "submit", script=script, request_id=uuid.uuid4().hex, origin="agent", intent=intent
    )
    for _ in range(30):
        state = await request(directory, "get_job", job_id=job["job_id"], wait_ms=1000)
        if state["terminal"]:
            return directory, job["job_id"], state
    raise AssertionError("real Innovus job did not complete")


@pytest.mark.asyncio
async def test_real_tcl_global_scope_and_quoting():
    variable = "imcp_test_" + uuid.uuid4().hex
    directory, identity, state = await run(
        f'set {variable} [list {{braces}} {{$dollar}} {{[brackets]}} {{"quotes"}} {{backslash\\path}}]'
    )
    assert state["state"] == "succeeded"
    expected = (await request(directory, "read_job_result", job_id=identity))["text"]
    directory, identity, state = await run(f"set {variable}")
    assert (await request(directory, "read_job_result", job_id=identity))["text"] == expected
    await run(f"unset {variable}")


@pytest.mark.asyncio
async def test_real_tcl_error_and_nonstandard_completion():
    directory, identity, state = await run("error {deliberate MCP validation error}")
    assert state["state"] == "failed" and state["tcl_code"] == 1
    assert (
        "deliberate MCP validation error"
        in (await request(directory, "read_job_result", job_id=identity, artifact="error_info"))["text"]
    )
    for script, code in [("return {top level}", 2), ("break", 3), ("continue", 4)]:
        _, _, state = await run(script)
        assert state["state"] == "failed" and state["tcl_code"] == code


@pytest.mark.asyncio
async def test_real_long_result_and_printed_diagnostic():
    directory, identity, state = await run("puts {**ERROR: diagnostic validation}; string repeat x 200000")
    assert state["state"] == "succeeded"
    assert state["error_count"] == 1
    page = await request(directory, "read_job_result", job_id=identity, max_bytes=1000000)
    assert page["available_bytes"] == 200000 and page["next_offset"] == 65536


@pytest.mark.asyncio
async def test_real_silent_job_survives_short_wait_and_fake_prompt():
    directory = Path(os.environ["INNOVUS_MCP_REAL_SESSION"]).resolve()
    job = await request(
        directory,
        "submit",
        script="after 500; puts {innovus 999> }; after 500; set x done",
        request_id=uuid.uuid4().hex,
    )
    state = await request(directory, "get_job", job_id=job["job_id"], wait_ms=10)
    assert not state["terminal"]
    await asyncio.sleep(0.6)
    state = await request(directory, "get_job", job_id=job["job_id"])
    assert not state["terminal"]
    state = await request(directory, "get_job", job_id=job["job_id"], wait_ms=10000)
    assert state["state"] == "succeeded"


@pytest.mark.asyncio
async def test_real_fastmcp_stdio_reconnect():
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    directory = Path(os.environ["INNOVUS_MCP_REAL_SESSION"]).resolve()
    transport = StdioTransport(
        command=sys.executable,
        keep_alive=False,
        args=["-m", "innovus_mcp", "--root", str(directory.parent), "serve", "--session", directory.name],
    )
    async with Client(transport) as client:
        response = await client.call_tool(
            "query_tcl",
            {"script": "after 1500; puts {real MCP stdio}; info patchlevel", "request_id": uuid.uuid4().hex},
        )
        identity = response.data["job_id"]
    assert transport._connect_task is None
    async with Client(transport) as client:
        response = await client.call_tool("get_job", {"job_id": identity, "wait_ms": 10000})
        assert response.data["state"] == "succeeded"
        output = await client.call_tool("read_job_output", {"job_id": identity})
        assert "real MCP stdio" in output.data["text"]
        result = await client.call_tool("read_job_result", {"job_id": identity})
        assert result.data["text"].startswith("8.")
