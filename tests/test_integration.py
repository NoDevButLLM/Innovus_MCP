"""Run with a supported tmux. Each test creates and stops only its own dedicated server."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from innovus_mcp.common import BrokerError, request

TMUX = shutil.which(os.environ.get("INNOVUS_MCP_TEST_TMUX") or os.environ.get("INNOVUS_MCP_TMUX") or "tmux")
pytestmark = pytest.mark.skipif(not TMUX, reason="Install tmux >=3.2 or set INNOVUS_MCP_TEST_TMUX")


async def wait_ready(directory):
    for _ in range(200):
        try:
            status = await request(directory, "session_status")
            if status["state"] == "ready":
                return status
            if status["state"] == "startup_failed":
                raise AssertionError(status)
        except BrokerError as exc:
            if exc.code != "broker_unavailable":
                raise
        await asyncio.sleep(0.05)
    raise AssertionError("broker did not become ready")


async def finish(directory, identity):
    while True:
        state = await request(directory, "get_job", job_id=identity, wait_ms=1000)
        if state["terminal"]:
            return state


@pytest.fixture
def session(tmp_path):
    name = "test" + uuid.uuid4().hex[:8]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "innovus_mcp",
            "--root",
            str(tmp_path),
            "start",
            "--session",
            name,
            "--tmux",
            TMUX,
            "--",
            sys.executable,
            "-u",
            str(Path(__file__).with_name("fake_innovus.py")),
        ],
        check=True,
        capture_output=True,
    )
    directory = tmp_path / name
    yield directory
    try:
        asyncio.run(request(directory, "stop", force=True))
    except Exception:
        config = json.loads((directory / "session.json").read_text())
        subprocess.run([TMUX, "-S", config["tmux_socket"], "kill-server"], capture_output=True)


@pytest.mark.asyncio
async def test_long_jobs_large_outputs_and_native_handoff(session):
    await wait_ready(session)
    first = await request(
        session,
        "submit",
        script="SLEEP:1 OUTPUT:500000 RESULT:200000 PROMPT_TEXT",
        request_id="long",
        origin="agent",
        intent="query",
    )
    state = await request(session, "get_job", job_id=first["job_id"], wait_ms=20)
    assert not state["terminal"]
    second = await request(
        session, "submit", script="ERROR", request_id="next", origin="human", intent="mutate"
    )
    assert (await finish(session, first["job_id"]))["state"] == "succeeded"
    assert (await finish(session, second["job_id"]))["state"] == "failed"
    output = await request(session, "read_job_output", job_id=first["job_id"], max_bytes=1000000)
    assert output["next_offset"] == 65536
    assert output["available_bytes"] > 500000
    result = await request(session, "read_job_result", job_id=first["job_id"], max_bytes=1000000)
    assert result["available_bytes"] == 200000
    assert len(result["text"]) == 65536
    retry = await request(
        session,
        "submit",
        script="SLEEP:1 OUTPUT:500000 RESULT:200000 PROMPT_TEXT",
        request_id="long",
        origin="agent",
        intent="query",
    )
    assert retry["job_id"] == first["job_id"]
    await request(session, "acquire_native")
    for _ in range(100):
        status = await request(session, "session_status")
        if status["ownership"] == "native":
            break
        await asyncio.sleep(0.05)
    assert status["ownership"] == "native"
    with pytest.raises(BrokerError, match="Native"):
        await request(session, "submit", script="oops", request_id="blocked")
    # Simulate native terminal input after acquiring the lease, without creating a managed job.
    from innovus_mcp.tmux_backend import Tmux

    backend = Tmux(TMUX, status["tmux_socket"])
    backend.pane = status["pane_id"]
    await backend.send("hello from human")
    await backend.send("::innovus_mcp::release_native")
    for _ in range(200):
        status = await request(session, "session_status")
        if status["ownership"] == "managed":
            break
        await asyncio.sleep(0.05)
    assert status["ownership"] == "managed"
    assert not status["agent_paused"]
    interval = await request(
        session, "read_session_output", native_interval_id=status["native_interval"]["native_interval_id"]
    )
    assert "hello from human" in interval["text"]
    assert (await request(session, "submit", script="resumed", request_id="auto-resume"))["job_id"]


@pytest.mark.asyncio
async def test_fastmcp_tools_with_real_stdio_transport(session):
    await wait_ready(session)
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        keep_alive=False,
        args=["-m", "innovus_mcp", "--root", str(session.parent), "serve", "--session", session.name],
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        query = next(t for t in tools if t.name == "query_tcl")
        assert query.annotations.readOnlyHint is False
        accepted = await client.call_tool(
            "query_tcl", {"script": "SLEEP:0.3 OUTPUT:100", "request_id": "mcp"}
        )
        identity = accepted.data["job_id"]
    assert transport._connect_task is None
    # Closing the MCP adapter does not cancel the running job.
    assert (await finish(session, identity))["state"] == "succeeded"
    async with Client(transport) as client:
        state = await client.call_tool("get_job", {"job_id": identity})
        assert state.data["state"] == "succeeded"


@pytest.mark.asyncio
async def test_bounded_submission_pages_and_retry(session):
    await wait_ready(session)
    first = await request(session, "submit", script="OUTPUT:4163 RESULT:0", request_id="inline", wait_ms=1000)
    assert first["state"] == "succeeded"
    assert first["output"]["text"] == "x" * 4163
    assert first["result"]["text"] == "" and first["result_bytes"] == 0
    assert first["error_info_bytes"] == first["error_code_bytes"] == 0
    assert "head" not in first and "tail" not in first and "output_tail" not in first
    retry = await request(
        session,
        "submit",
        script="OUTPUT:4163 RESULT:0",
        request_id="inline",
        wait_ms=1000,
        max_output_bytes=100,
        max_result_bytes=0,
    )
    assert retry["job_id"] == first["job_id"]
    assert retry["output"]["next_offset"] == 100 and retry["output"]["has_more"]
    assert "result" not in retry
    summary = await request(session, "get_job", job_id=first["job_id"])
    assert summary["output"]["text"] == "x" * 4163
    assert not summary["summary_truncated"] and "output_tail" not in summary


@pytest.mark.asyncio
async def test_wait_releases_lock_and_large_result_is_bounded(session):
    await wait_ready(session)
    pending = asyncio.create_task(
        request(session, "submit", script="SLEEP:0.4 RESULT:200000", request_id="waiting", wait_ms=1000)
    )
    await asyncio.sleep(0.05)
    second = await asyncio.wait_for(
        request(session, "submit", script="next", request_id="parallel"), timeout=0.25
    )
    assert second["state"] == "queued"
    first = await pending
    assert first["result_bytes"] == 200000 and len(first["result"]["text"]) == 4096
    page = await request(session, "read_job_result", job_id=first["job_id"], offset=4096, max_bytes=1000000)
    assert page["next_offset"] == 4096 + 65536 and page["has_more"]
    assert (await finish(session, second["job_id"]))["state"] == "succeeded"


async def wait_ownership(session, expected):
    for _ in range(200):
        status = await request(session, "session_status")
        if status["ownership"] == expected:
            return status
        assert not status["error"], status
        await asyncio.sleep(0.025)
    raise AssertionError(f"ownership did not become {expected}: {status}")


@pytest.mark.asyncio
async def test_native_tmux_shortcut_and_automatic_return(session):
    from native_terminal import NativeTerminal

    from innovus_mcp.tmux_backend import Tmux

    status = await wait_ready(session)
    backend = Tmux(TMUX, status["tmux_socket"])
    async with NativeTerminal(backend.attach_args(readonly=True)) as terminal:
        for _ in range(100):
            if await backend.clients():
                break
            await asyncio.sleep(0.02)
        assert all(c["readonly"] == "1" for c in await backend.clients())
        visible = await backend.cmd("capture-pane", "-p", "-t", status["pane_id"])
        assert "IMCP:" not in visible
        terminal.write(b"must-not-reach-innovus\r")
        await asyncio.sleep(0.1)
        assert b"must-not-reach-innovus" not in (session / "pane.raw").read_bytes()
        job = await request(session, "submit", script="SLEEP:0.8", request_id="before-handoff")
        for _ in range(100):
            state = await request(session, "get_job", job_id=job["job_id"], max_output_bytes=0)
            if state["state"] == "running":
                break
            await asyncio.sleep(0.01)
        assert state["state"] == "running"
        terminal.write(b"\x02h")
        pending = await wait_ownership(session, "handoff_pending")
        assert pending["agent_paused"]
        with pytest.raises(BrokerError, match="Native"):
            await request(session, "submit", script="blocked", request_id="during-handoff")
        await wait_ownership(session, "native")
        assert (await finish(session, job["job_id"]))["state"] == "succeeded"
        for _ in range(100):
            writers = [c for c in await backend.clients() if c["readonly"] == "0"]
            if writers:
                break
            await asyncio.sleep(0.02)
        assert len(writers) == 1
        terminal.write(b"native-command-through-tty\r")
        await asyncio.sleep(0.1)
        terminal.write(b"::innovus_mcp::release_native\r")
        returned = await wait_ownership(session, "managed")
        assert not returned["agent_paused"]
        assert all(c["readonly"] == "1" for c in await backend.clients())
        assert b"native-command-through-tty" in (session / "pane.raw").read_bytes()
        resumed = await request(
            session, "submit", script="resumed-with-viewer", request_id="with-viewer", wait_ms=1000
        )
        assert resumed["state"] == "succeeded"
        terminal.write(b"\x02h")
        await wait_ownership(session, "native")
        for _ in range(100):
            if any(c["readonly"] == "0" for c in await backend.clients()):
                break
            await asyncio.sleep(0.02)
        terminal.write(b"::innovus_mcp::release_native\r")
        await wait_ownership(session, "managed")


@pytest.mark.asyncio
async def test_managed_interrupt_with_readonly_viewer(session):
    from native_terminal import NativeTerminal

    from innovus_mcp.tmux_backend import Tmux

    status = await wait_ready(session)
    backend = Tmux(TMUX, status["tmux_socket"])
    async with NativeTerminal(backend.attach_args(readonly=True)):
        for _ in range(100):
            if await backend.clients():
                break
            await asyncio.sleep(0.01)
        job = await request(session, "submit", script="SLEEP:5", request_id="interrupt-viewer")
        for _ in range(100):
            metadata = json.loads((session / "jobs" / job["job_id"] / "job.json").read_text())
            raw = (session / "pane.raw").read_bytes()
            if metadata["state"] == "running" and b"START:" in raw:
                break
            await asyncio.sleep(0.01)
        assert metadata["state"] == "running" and b"START:" in raw
        ack = await request(session, "interrupt_job", job_id=job["job_id"])
        assert ack["interrupt_requested"] and not ack["terminal"]
        for _ in range(100):
            if b"^C" in (session / "pane.raw").read_bytes():
                break
            await asyncio.sleep(0.02)
        assert b"^C" in (session / "pane.raw").read_bytes()
        # VINTR reached the terminal, but the application need not honor it immediately.
        # No DONE or manifest was emitted, so delivery is not completion.
        state = await request(session, "get_job", job_id=job["job_id"])
        assert not state["terminal"]


@pytest.mark.asyncio
async def test_broker_exit_leaves_tmux_and_capture_running(session):
    status = await wait_ready(session)
    job = await request(
        session,
        "submit",
        script="SLEEP:1 OUTPUT:800000",
        request_id="survival",
        origin="agent",
        intent="query",
    )
    for _ in range(100):
        status = await request(session, "session_status")
        if (
            status["active_job"] == job["job_id"]
            and b"START:" + job["job_id"].encode() in (session / "pane.raw").read_bytes()
        ):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("Job did not reach the application before broker termination")
    before = (session / "pane.raw").stat().st_size
    config = json.loads((session / "session.json").read_text())
    os.kill(config["broker_pid"], signal.SIGTERM)
    for _ in range(250):
        if (session / "pane.raw").stat().st_size > before + 800000 and (
            session / "jobs" / job["job_id"] / "completion.meta"
        ).exists():
            break
        await asyncio.sleep(0.02)
    assert (session / "pane.raw").stat().st_size > before + 800000
    assert (session / "jobs" / job["job_id"] / "completion.meta").exists()
    from innovus_mcp.tmux_backend import Tmux

    tmux = Tmux(TMUX, config["tmux_socket"])
    assert await tmux.cmd("display-message", "-p", "-t", config["pane_id"], "#{pane_dead}") == "0"
