from copy import deepcopy

import pytest

from validation.opencode.run_flow import analyze


def evidence(tmp_path):
    case = {"command": "dbGet", "status": "ok", "detail": "full", "prompt": "Query usage"}
    exported = {
        "info": {"id": "ses_test", "location": {"directory": str(tmp_path)}},
        "messages": [
            {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool",
                        "name": "execute",
                        "state": {
                            "metadata": {"toolCalls": [{"tool": "innovus.command_help"}]},
                        },
                    },
                    {"type": "text", "text": "Command usage"},
                ],
            }
        ],
    }
    rows = [
        {
            "tool": "command_help",
            "arguments": {},
            "is_error": False,
            "duration_ms": 50,
            "data": {
                "terminal": True,
                "job_id": "job",
                "help": {
                    "command": "dbGet",
                    "status": "ok",
                    "detail": "full",
                    "complete": True,
                },
                "result": {"has_more": False, "next_offset": 100},
            },
        }
    ]
    return case, exported, rows


def test_flow_accepts_single_help_and_records_opencode_discovery(tmp_path):
    case, exported, rows = evidence(tmp_path)
    exported["messages"][0]["content"][0]["state"]["metadata"]["toolCalls"].insert(
        0, {"tool": "opencode.list_mcp_resources"}
    )
    result = analyze(case, tmp_path, exported, rows, 1)
    assert result["passed"] and result["mcp_calls"] == 1
    assert result["other_nested_tools"] == ["opencode.list_mcp_resources"]
    exported["messages"][0]["content"].insert(
        0,
        {
            "type": "tool",
            "name": "execute",
            "state": {
                "content": [{"type": "text", "text": "Unknown tool"}],
                "metadata": {"error": True, "toolCalls": []},
            },
        },
    )
    result = analyze(case, tmp_path, exported, rows, 1)
    assert result["passed"] and len(result["opencode_frontend_errors"]) == 1
    assert result["opencode_outer_tools"] == 2


@pytest.mark.parametrize("offset,expected", [(100, True), (0, False)])
def test_flow_accepts_only_necessary_contiguous_paging(tmp_path, offset, expected):
    case, exported, rows = evidence(tmp_path)
    case.update(allow_paging=True, max_mcp_calls=2)
    rows[0]["data"]["result"]["has_more"] = True
    rows.append(
        {
            "tool": "read_job_result",
            "arguments": {"job_id": "job", "offset": offset},
            "is_error": False,
            "duration_ms": 2,
            "data": {"next_offset": 200, "has_more": False},
        }
    )
    exported["messages"][0]["content"][0]["state"]["metadata"]["toolCalls"].append(
        {"tool": "innovus.read_job_result"}
    )
    result = analyze(case, tmp_path, exported, rows, 1)
    assert result["passed"] is expected
    assert result["checks"]["terminal"]  # Metadata comes from the submitted job, not the last page.


def test_flow_rejects_wrong_directory_shell_and_missing_trace(tmp_path):
    case, exported, rows = evidence(tmp_path)
    wrong = deepcopy(exported)
    wrong["info"]["location"]["directory"] = str(tmp_path / "other")
    assert not analyze(case, tmp_path, wrong, rows, 1)["passed"]
    exported["messages"][0]["content"].append({"type": "tool", "name": "shell", "state": {}})
    assert not analyze(case, tmp_path, exported, rows, 1)["checks"]["no_shell"]
    assert not analyze(case, tmp_path, exported, [], 1)["checks"]["trace_matches_export"]
