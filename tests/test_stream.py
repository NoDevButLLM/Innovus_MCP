import asyncio
import json
import uuid

import pytest

from innovus_mcp.broker import Broker


def make_broker(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "tmux": "tmux",
                "tmux_socket": str(tmp_path / "unused.sock"),
                "token": "a" * 32,
                "name": "test",
                "session_id": "test",
            }
        )
    )
    broker = Broker(tmp_path)
    job_id = uuid.uuid4().hex
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    broker.jobs[job_id] = dict(job_id=job_id, state="running", spans=[], output_bytes=0)
    broker.active = job_id
    return broker, job_id


@pytest.mark.parametrize("chunk_size", [1, 2, 17, 64, 500])
@pytest.mark.parametrize("osc", [False, True], ids=["legacy", "osc"])
def test_marker_splits_and_independent_pages(tmp_path, chunk_size, osc):
    broker, job_id = make_broker(tmp_path)
    # Large lines, carriage returns, invalid UTF-8, and prompt-like report text.
    output = b"first\r\n" + b"x" * 100000 + b"\xff\ninnovus 99> "
    wire = marker(broker, "START", job_id, osc) + output
    (tmp_path / "pane.raw").write_bytes(wire)
    for offset in range(0, len(wire), chunk_size):
        broker.buffer += wire[offset : offset + chunk_size]
        broker.parse_stream()
    assert broker.jobs[job_id]["state"] == "running"
    assert broker.jobs[job_id]["output_bytes"] == len(output)
    assert len(broker.buffer) < 200
    first = broker.output_page(broker.jobs[job_id], max_bytes=1000000, view="base64")
    assert first["next_offset"] == 65536
    assert first == broker.output_page(broker.jobs[job_id], max_bytes=1000000, view="base64")
    assert broker.output_page(broker.jobs[job_id], offset=len(output))["text"] == ""


def marker(broker, kind, job_id, osc):
    payload = f"IMCP:{broker.config['token']}:{kind}:{job_id}".encode()
    return b"\x1b]777;" + payload + b"\x07" if osc else b"\x1e" + payload + b"\x1f"


@pytest.mark.parametrize("osc", [False, True], ids=["legacy", "osc"])
def test_completion_requires_marker_manifest_and_prompt(tmp_path, osc):
    broker, job_id = make_broker(tmp_path)
    (tmp_path / "jobs" / job_id / "completion.meta").write_text("version=1\ncode=1\n")
    wire = marker(broker, "START", job_id, osc) + b"hello"
    broker.buffer = wire
    broker.parse_stream()
    assert broker.active == job_id
    broker.buffer += marker(broker, "DONE", job_id, osc)
    broker.parse_stream()
    assert broker.active == job_id
    broker.buffer += b"\ninnovus 2> "
    broker.parse_stream()
    assert broker.active is None
    assert broker.jobs[job_id]["state"] == "failed"
    assert broker.jobs[job_id]["tcl_code"] == 1


@pytest.mark.asyncio
async def test_deduplication_and_queued_cancellation(tmp_path):
    broker, old_id = make_broker(tmp_path)
    broker.jobs.clear()
    broker.active = None
    broker.state = "ready"
    payload = dict(script="set a 1", request_id="retry", origin="human", intent="mutate")
    first = await broker.dispatch("submit", payload)
    again = await broker.dispatch("submit", payload)
    assert again["job_id"] == first["job_id"]
    assert len(broker.queue) == 1
    from innovus_mcp.common import BrokerError

    with pytest.raises(BrokerError, match="different payload"):
        await broker.dispatch("submit", {**payload, "script": "set a 2"})
    cancelled = await broker.dispatch("cancel_job", {"job_id": first["job_id"]})
    assert cancelled["state"] == "cancelled"
    assert not broker.queue


@pytest.mark.parametrize("length", [0, 1, 2048, 4096, 6144, 6145, 100000])
def test_summary_has_no_overlapping_ranges(tmp_path, length):
    broker, job_id = make_broker(tmp_path)
    (tmp_path / "pane.raw").write_bytes(b"x" * length)
    job = broker.jobs[job_id]
    job.update(state="succeeded", output_bytes=length, spans=[[0, length]])
    view = broker.observed_view(job, 6144, 0, summary=True)
    assert "head" not in view and "tail" not in view
    if length <= 6144:
        assert view["output"]["text"] == "x" * length
        assert "output_tail" not in view and not view["summary_truncated"]
    else:
        assert view["output"]["next_offset"] <= view["output_tail"]["offset"]
        assert view["output"]["next_offset"] + len(view["output_tail"]["text"]) == 6144
        assert view["summary_truncated"]


@pytest.mark.asyncio
async def test_waiting_submission_wakes_on_queued_cancel(tmp_path):
    broker, _ = make_broker(tmp_path)
    broker.jobs.clear()
    broker.active = None
    broker.state = "ready"
    pending = asyncio.create_task(
        broker.dispatch("submit", {"script": "not executed", "request_id": "cancel-wait", "wait_ms": 1000})
    )
    await asyncio.sleep(0)
    identity = next(iter(broker.jobs))
    await broker.dispatch("cancel_job", {"job_id": identity})
    view = await asyncio.wait_for(pending, timeout=0.2)
    assert view["state"] == "cancelled" and view["result_bytes"] is None
    assert "result" not in view and identity not in broker.job_done
