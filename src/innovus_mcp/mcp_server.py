"""FastMCP owns MCP framing, validation, schemas, and stdio transport."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .common import BrokerError, request


def create_server(directory: Path) -> FastMCP:
    mcp = FastMCP(
        "Innovus shared session",
        instructions=(
            "One shared tmux-hosted Innovus session. For short commands, submit with wait_ms=500 "
            "to receive bounded output and Tcl result pages in one call. For command usage, use "
            "command_help (full by default, summary for syntax only). It reads manuals without a pager. "
            "Once terminal content is sufficient, answer directly without session_status, readiness "
            "probes or related-command queries. Do not submit interactive man/less through query_tcl. "
            "Managed jobs cannot accept follow-up keyboard input; new Tcl queues behind the active job. "
            "Retain the job ID; if still running, use get_job with "
            "bounded waiting. Console output and Tcl return values are separate. Reuse inline pages "
            "and read further only when has_more is true, using next_offset as the byte cursor. "
            "A running output page may grow later. Artifact byte sizes are null until confirmed, "
            "zero means empty. Reuse request_id when retrying the same submission. "
            "For status-only polling set both page budgets to zero. Do not infer defaults absent from "
            "the documentation. An interrupt acknowledgment is not completion; repeated requests send "
            "no additional Ctrl+C. Use supported MCP operations; external tmux input and process "
            "signals are not part of the command-help workflow. "
            "query_tcl is an intent label and does NOT enforce read-only execution. "
            "Native human control pauses all managed Tcl submissions."
        ),
    )

    async def call(method, **params):
        try:
            return await request(directory, method, **params)
        except BrokerError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @mcp.tool(annotations={"readOnlyHint": True})
    async def session_status() -> dict:
        """Inspect ownership, active job, queue, tmux identity, and capture health."""
        return await call("session_status")

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def command_help(
        command: str,
        request_id: str,
        detail: Literal["summary", "full"] = "full",
        wait_ms: int = 500,
        max_output_bytes: int = 16384,
        max_result_bytes: int = 16384,
    ) -> dict:
        """Get command usage without executing the command or opening an interactive pager.

        Prefer this tool for all Innovus command-usage questions. full provides purpose, syntax and
        parameters from the target session's manual; summary provides runtime help only. The help
        receipt includes acquisition status, completeness, source, version, UI and external exit code.
        Full help is normally in result.text; runtime summary is normally in output.text. Inspect both.
        If full is unavailable, runtime summary is returned with status=fallback and complete=false.
        Tcl success alone does not certify documentation acquisition. Inline pages can be truncated
        even when help.complete is true: use has_more and next_offset to page the indicated artifact.
        Reuse an identical request_id on retry. Retain a running job's ID and observe with get_job.
        Once sufficient terminal content arrives, answer without status checks or readiness probes.
        Defaults not listed by the documentation must not be inferred. Wait and page limits match
        query_tcl; a wait deadline never cancels execution. Do not use raw interactive man/less.
        """
        return await call(
            "command_help",
            command=command,
            request_id=request_id,
            detail=detail,
            wait_ms=wait_ms,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    async def query_tcl(
        script: str,
        request_id: str,
        wait_ms: int = 0,
        max_output_bytes: int = 16384,
        max_result_bytes: int = 4096,
    ) -> dict:
        """Submit query-intent Tcl. Unrestricted Tcl: read-only behavior is NOT enforced.

        Reuse request_id only for an identical retry. wait_ms=0 returns a job ID immediately;
        For command usage prefer command_help. Do not submit interactive man/less. Positive waits return bounded
        output/result pages and stop waiting after at most 10 seconds, without cancelling execution.
        Byte budgets cap at 65536 each; zero omits that inline page. Continue from next_offset only
        when more data is needed. result_bytes/error_info_bytes/error_code_bytes describe artifacts.
        """
        return await call(
            "submit",
            script=script,
            request_id=request_id,
            origin="agent",
            intent="query",
            wait_ms=wait_ms,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    async def mutate_tcl(
        script: str,
        request_id: str,
        wait_ms: int = 0,
        max_output_bytes: int = 16384,
        max_result_bytes: int = 4096,
    ) -> dict:
        """Submit edit-intent Tcl. No snapshots, rollback, or protection are provided.

        Reuse request_id only for an identical retry. wait_ms=0 returns immediately; a positive wait
        returns bounded output/result pages and waits at most 10 seconds. Deadline expiry never
        cancels execution. Byte budgets cap at 65536 each; zero omits that inline page. Reuse returned
        pages and their next_offset cursors. Artifact sizes are null until confirmed; zero means empty.
        """
        return await call(
            "submit",
            script=script,
            request_id=request_id,
            origin="agent",
            intent="mutate",
            wait_ms=wait_ms,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_jobs(after_sequence: int = 0, limit: int = 20) -> dict:
        """List up to 100 jobs in submission order, with a pagination cursor."""
        return await call("list_jobs", after_sequence=after_sequence, limit=limit)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_job(
        job_id: str,
        wait_ms: int = 0,
        max_output_bytes: int = 6144,
        max_result_bytes: int = 4096,
    ) -> dict:
        """Inspect a job, bounded output summary, inline Tcl result, and artifact byte sizes.

        Wait at most 10 seconds; timeout never cancels execution. Small output appears once in
        output. Larger output uses disjoint output and output_tail pages, at most 6144 source bytes
        combined. summary_truncated marks omitted bytes. Page offsets count raw bytes. Result budget
        caps at 65536; zero budgets omit inline pages. Reuse inline data before paging more.
        For status-only observations set max_output_bytes=0 and max_result_bytes=0. Once a help job
        is terminal and sufficient content is present, answer without extra observations or probes.
        """
        return await call(
            "get_job",
            job_id=job_id,
            wait_ms=wait_ms,
            max_output_bytes=max_output_bytes,
            max_result_bytes=max_result_bytes,
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_job_output(
        job_id: str, offset: int = 0, max_bytes: int = 16384, view: str = "text"
    ) -> dict:
        """Page console output independently. Byte cursors, 64 KiB maximum, text or exact base64 view."""
        return await call("read_job_output", job_id=job_id, offset=offset, max_bytes=max_bytes, view=view)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_job_result(
        job_id: str, artifact: str = "result", offset: int = 0, max_bytes: int = 16384, view: str = "text"
    ) -> dict:
        """Page a completed Tcl return value, error_info, or error_code, separately from console output."""
        return await call(
            "read_job_result", job_id=job_id, artifact=artifact, offset=offset, max_bytes=max_bytes, view=view
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_session_output(
        offset: int = 0, max_bytes: int = 16384, native_interval_id: str | None = None, view: str = "text"
    ) -> dict:
        """Page startup/native/session transcript, optionally relative to a native human interval."""
        return await call(
            "read_session_output",
            offset=offset,
            max_bytes=max_bytes,
            native_interval_id=native_interval_id,
            view=view,
        )

    @mcp.tool(annotations={"readOnlyHint": False})
    async def cancel_job(job_id: str) -> dict:
        """Cancel a queued job. A running job cannot be conclusively cancelled by this tool."""
        return await call("cancel_job", job_id=job_id)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def interrupt_job(job_id: str) -> dict:
        """Request one Ctrl+C; acknowledgment is not completion or rollback.

        interrupt_sent reports this call's action; reason is sent, already_requested, not_active or
        terminal. Repeating a request sends no additional Ctrl+C. Interactive pagers may remain open.
        """
        return await call("interrupt_job", job_id=job_id)

    return mcp
