import base64
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from test_stream import make_broker

from innovus_mcp.command_help import help_script, read_help_metadata
from innovus_mcp.common import BrokerError
from innovus_mcp.mcp_server import create_server


@pytest.mark.parametrize("command", ["place_opt_design", "dbGet", "::example::command"])
def test_help_script_quotes_command_and_uses_packaged_helper(command):
    script = help_script(command, "full")
    assert "command_help.tcl" in script
    assert script.endswith(f'::innovus_mcp::command_help "{command}" "full"')


@pytest.mark.parametrize(
    "command", ["", "place_opt_design -help", "x; exit", "[exit]", "x\ny", "-P", "x" * 129]
)
def test_help_command_is_one_name(command):
    with pytest.raises(BrokerError, match="one Tcl command name"):
        help_script(command, "full")


def test_unknown_detail():
    with pytest.raises(BrokerError, match="summary or full"):
        help_script("dbGet", "interactive")


def test_help_receipt_is_utf8_and_preserves_external_failure(tmp_path):
    fields = {
        "format_version": "1",
        "complete": "0",
        "external_exit_code": "143",
        "message": "手册缺失\n退出",
    }
    path = tmp_path / "help.meta"
    path.write_text("\n".join(f"{k}={base64.b64encode(v.encode()).decode()}" for k, v in fields.items()))
    assert read_help_metadata(path) == {
        "complete": False,
        "external_exit_code": 143,
        "message": fields["message"],
    }


@pytest.mark.asyncio
async def test_help_submission_keeps_queue_dedup_and_request_context(tmp_path):
    broker, _ = make_broker(tmp_path)
    broker.jobs.clear()
    broker.active = None
    broker.state = "ready"
    payload = {"command": "dbGet", "request_id": "help-1", "wait_ms": 0}
    first = await broker.dispatch("command_help", payload)
    repeated = await broker.dispatch("command_help", payload)
    assert first["job_id"] == repeated["job_id"] and len(broker.queue) == 1
    assert first["help"] == {
        "command": "dbGet",
        "requested_detail": "full",
        "status": "pending",
        "complete": False,
    }
    assert first["intent"] == "query"
    with pytest.raises(BrokerError, match="different payload"):
        await broker.dispatch("command_help", {**payload, "detail": "summary"})


@pytest.mark.asyncio
async def test_interrupt_receipt_distinguishes_each_action(tmp_path):
    broker, identity = make_broker(tmp_path)
    job = broker.jobs[identity]
    job["interrupt_requested"] = False
    broker.tmux.interrupt = AsyncMock()
    first = await broker.dispatch("interrupt_job", {"job_id": identity})
    assert first["interrupt_sent"] and first["reason"] == "sent" and not first["terminal"]
    repeated = await broker.dispatch("interrupt_job", {"job_id": identity})
    assert not repeated["interrupt_sent"] and repeated["reason"] == "already_requested"
    broker.tmux.interrupt.assert_awaited_once()
    broker.active = None
    queued = await broker.dispatch("interrupt_job", {"job_id": identity})
    assert not queued["interrupt_sent"] and queued["reason"] == "not_active"
    job["state"] = "succeeded"
    done = await broker.dispatch("interrupt_job", {"job_id": identity})
    assert not done["interrupt_sent"] and done["reason"] == "terminal"


@pytest.mark.asyncio
async def test_help_schema_and_defaults_use_fastmcp(tmp_path, monkeypatch):
    recorded = []

    async def request(directory, method, **params):
        recorded.append((directory, method, params))
        return {"state": "queued"}

    monkeypatch.setattr("innovus_mcp.mcp_server.request", request)
    async with Client(create_server(tmp_path)) as client:
        tools = await client.list_tools()
        help_tool = next(tool for tool in tools if tool.name == "command_help")
        assert help_tool.annotations.readOnlyHint and not help_tool.annotations.destructiveHint
        assert help_tool.inputSchema["properties"]["detail"]["enum"] == ["summary", "full"]
        result = await client.call_tool("command_help", {"command": "dbGet", "request_id": "schema-test"})
        assert result.data["state"] == "queued"
    assert recorded == [
        (
            tmp_path,
            "command_help",
            {
                "command": "dbGet",
                "request_id": "schema-test",
                "detail": "full",
                "wait_ms": 500,
                "max_output_bytes": 16384,
                "max_result_bytes": 16384,
            },
        )
    ]


def test_missing_help_receipt_does_not_break_session(tmp_path):
    broker, identity = make_broker(tmp_path)
    job = broker.jobs[identity]
    job["help_request"] = {"command": "dbGet", "requested_detail": "full"}
    path = tmp_path / "jobs" / identity
    (path / "completion.meta").write_text("version=1\ncode=0\n")
    for name in ("result", "error_info", "error_code"):
        (path / f"{name}.txt").write_text("")
    broker.finish_job(job)
    assert broker.state == "ready" and job["state"] == "succeeded"
    assert job["help"]["status"] == "unavailable" and not job["help"]["complete"]
    assert json.loads((path / "job.json").read_text())["help"] == job["help"]
