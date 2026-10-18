import pytest

from innovus_mcp.cli import parser


@pytest.mark.parametrize("command", ["start", "status", "serve", "attach", "stop"])
def test_management_commands_remain(command):
    assert parser().parse_args([command, "--session", "chip"]).action == command


@pytest.mark.parametrize("command", ["submit", "job", "jobs", "output", "result", "view", "resume-agent"])
def test_human_job_commands_are_removed(command):
    with pytest.raises(SystemExit):
        parser().parse_args([command])
