"""Direct real-Innovus MCP checks, separate from agent-selected calls in run_flow.py."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from innovus_mcp.common import request, tcl_word
from innovus_mcp.tmux_backend import Tmux

from .run_flow import DEFAULT_OUTPUT, HERE, default_tmux, execute, ready, write_json


async def main(args):
    output = args.output.expanduser().resolve() / (
        "api-" + time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )
    output.mkdir(parents=True)
    root = output / "sessions"
    directory = root / "api"
    command = [sys.executable, "-m", "innovus_mcp", "--root", str(root)]
    launch = [
        *command,
        "start",
        "--session",
        "api",
        "--cwd",
        str(output),
        "--tmux",
        args.tmux,
        "--",
        args.innovus,
        "-no_gui",
    ]
    if args.stylus:
        launch.append("-stylus")
    started = execute(launch, cwd=output)
    write_json(output / "start.json", started)
    if started["returncode"]:
        raise RuntimeError(started["stderr"])
    rows, cases = [], []

    async def call(client, tool, params):
        begin = time.perf_counter()
        response = await client.call_tool(tool, params)
        row = {
            "tool": tool,
            "arguments": params,
            "duration_ms": (time.perf_counter() - begin) * 1000,
            "data": response.data,
        }
        rows.append(row)
        with (output / "api-trace.jsonl").open("a") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        return response.data

    def record(name, begin, **detail):
        cases.append({"case": name, "passed": True, "mcp_calls": len(rows) - begin, **detail})
        write_json(output / "summary.json", {"passed": True, "stylus": args.stylus, "cases": cases})
        print(json.dumps(cases[-1], ensure_ascii=False), flush=True)

    def help_args(command="place_opt_design", **kwargs):
        return {"command": command, "request_id": uuid.uuid4().hex, **kwargs}

    async def query(client, script, **kwargs):
        return await call(
            client,
            "query_tcl",
            {"script": script, "request_id": uuid.uuid4().hex, "wait_ms": 10000, **kwargs},
        )

    print(f"Artifacts: {output}", flush=True)
    try:
        await ready(directory, 180)
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "innovus_mcp", "--root", str(root), "serve", "--session", "api"],
        )
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(transport))
            if args.viewer:
                sys.path.insert(0, str(HERE.parents[1] / "tests"))
                from native_terminal import NativeTerminal

                state = await request(directory, "session_status")
                backend = Tmux(state["tmux"], state["tmux_socket"])
                await stack.enter_async_context(NativeTerminal(backend.attach_args(readonly=True)))
                for _ in range(100):
                    if await backend.clients():
                        break
                    await asyncio.sleep(0.05)
                assert any(c["readonly"] == "1" for c in await backend.clients())
            begin = len(rows)
            full = await call(client, "command_help", help_args())
            assert full["terminal"] and full["help"]["complete"] and full["help"]["status"] == "ok", full
            assert full["help"]["external_exit_code"] == 0 and not full["result"]["has_more"]
            assert full["help"]["ui_mode"] == ("stylus" if args.stylus else "legacy")
            assert ("-report_dir" if args.stylus else "-out_dir") in full["result"]["text"]
            body = full["result"]["text"].encode()
            record("full", begin, result_bytes=full["result_bytes"], milliseconds=rows[-1]["duration_ms"])

            begin = len(rows)
            summary = await call(client, "command_help", help_args(detail="summary"))
            assert summary["help"]["complete"] and "Usage:" in summary["output"]["text"]
            record("summary", begin, output_bytes=summary["output_bytes"])

            begin = len(rows)
            missing = await call(client, "command_help", help_args(command="imcp_no_such_command_72941"))
            assert missing["terminal"] and missing["help"]["status"] == "unavailable"
            assert not missing["help"]["complete"] and missing["help"]["external_exit_code"] != 0
            record("missing", begin, help=missing["help"])

            begin = len(rows)
            small = await call(client, "command_help", help_args(max_result_bytes=64))
            assert small["help"]["complete"] and small["result"]["has_more"]
            assembled = small["result"]["text"].encode()
            offset = small["result"]["next_offset"]
            while offset < small["result_bytes"]:
                page = await call(
                    client,
                    "read_job_result",
                    {"job_id": small["job_id"], "offset": offset, "max_bytes": 2048, "view": "base64"},
                )
                assembled += base64.b64decode(page["base64"])
                offset = page["next_offset"]
            assert assembled == body
            record("result_paging", begin, bytes=len(assembled))

            # Environment perturbations are scoped to this disposable session and always restored.
            await query(
                client,
                "set ::imcp_saved_env [array get ::env]; set ::env(TERM) dumb; "
                "set ::env(MANPAGER) less; set ::env(PAGER) less",
            )
            try:
                begin = len(rows)
                with_pager_env = await call(client, "command_help", help_args())
                assert with_pager_env["terminal"] and with_pager_env["help"]["status"] == "ok"
                record("noninteractive_despite_pager_environment", begin)

                await query(client, "set ::env(MANPATH) [file join $::innovus_mcp::root no_manuals]")
                begin = len(rows)
                fallback = await call(client, "command_help", help_args())
                assert fallback["terminal"] and fallback["help"]["status"] == "fallback"
                assert not fallback["help"]["complete"] and "Usage:" in fallback["output"]["text"]
                record("missing_manual_falls_back", begin, help=fallback["help"])

                # Select the other installed UI manual directory, preserving the target process UI.
                source = Path(full["help"]["source_path"])
                native_root = source.parents[2] if not args.stylus else source.parents[3]
                other = native_root / ("man" if args.stylus else "stylus/man")
                await query(client, f"set ::env(MANPATH) {tcl_word(str(other))}")
                begin = len(rows)
                mismatch = await call(client, "command_help", help_args())
                assert mismatch["help"]["status"] == "fallback" and not mismatch["help"]["complete"]
                assert "UI" in mismatch["help"]["message"]
                record("wrong_ui_manual_falls_back", begin, help=mismatch["help"])
            finally:
                await query(
                    client,
                    "foreach key [array names ::env] {if {![dict exists $::imcp_saved_env $key]} "
                    "{unset ::env($key)}}; array set ::env $::imcp_saved_env; unset ::imcp_saved_env",
                )

            # Fault injection is confined to this disposable Innovus process.
            fake_bin = output / "fake-bin"
            fake_bin.mkdir()
            fake_man = fake_bin / "man"
            real_man = shutil.which("man")
            common = f"#!{sys.executable}\nimport os,sys,subprocess,re\nreal_man={real_man!r}\n"
            await query(
                client,
                f"set ::imcp_saved_path $::env(PATH); "
                f"set ::env(PATH) [join [list {tcl_word(str(fake_bin))} $::env(PATH)] :]; "
                "unset -nocomplain ::auto_execs(man)",
            )
            try:
                fake_man.write_text(
                    common + "print('imcp diagnostic warning',file=sys.stderr,flush=True)\n"
                    "os.execv(real_man,[real_man,*sys.argv[1:]])\n"
                )
                fake_man.chmod(0o755)
                begin = len(rows)
                warning = await call(client, "command_help", help_args())
                assert warning["help"]["status"] == "ok" and warning["help"]["external_exit_code"] == 0
                assert "imcp diagnostic warning" in warning["help"]["diagnostics"]
                record("stderr_warning_is_not_failure", begin)

                fake_man.write_text(
                    common + "if '-l' in sys.argv:\n"
                    " print('imcp forced renderer failure',file=sys.stderr)\n sys.exit(37)\n"
                    "os.execv(real_man,[real_man,*sys.argv[1:]])\n"
                )
                begin = len(rows)
                failed = await call(client, "command_help", help_args())
                assert failed["state"] == "succeeded" and failed["help"]["status"] == "fallback"
                assert failed["help"]["external_exit_code"] == 37 and not failed["help"]["complete"]
                record("external_exit_status_separate_from_tcl", begin, help=failed["help"])

                fake_man.write_text(
                    common + "r=subprocess.run([real_man,*sys.argv[1:]],capture_output=True,text=True)\n"
                    "text=r.stdout\n"
                    "if '-l' in sys.argv: text=re.sub(r'(Product Version\\s+)\\S+',r'\\g<1>99.99',text)\n"
                    "sys.stdout.write(text)\nsys.stderr.write(r.stderr)\nsys.exit(r.returncode)\n"
                )
                begin = len(rows)
                wrong_version = await call(client, "command_help", help_args())
                assert wrong_version["help"]["status"] == "fallback"
                assert "differs" in wrong_version["help"]["message"]
                record("wrong_version_falls_back", begin)
            finally:
                await query(
                    client,
                    "set ::env(PATH) $::imcp_saved_path; unset ::imcp_saved_path; "
                    "unset -nocomplain ::auto_execs(man)",
                )

            begin = len(rows)
            slow = await query(client, "after 1200; set ::imcp_long_result done", wait_ms=10)
            assert not slow["terminal"]
            done = await call(client, "get_job", {"job_id": slow["job_id"], "wait_ms": 10000})
            assert done["state"] == "succeeded" and done["result"]["text"] == "done"
            record("long_job", begin)

            begin = len(rows)
            long = await query(
                client, "puts -nonewline [string repeat 0123456789abcdef 8192]", max_output_bytes=1024
            )
            raw = long["output"]["text"].encode()
            offset = long["output"]["next_offset"]
            while offset < long["output_bytes"]:
                page = await call(
                    client,
                    "read_job_output",
                    {"job_id": long["job_id"], "offset": offset, "max_bytes": 65536, "view": "base64"},
                )
                raw += base64.b64decode(page["base64"])
                offset = page["next_offset"]
            assert raw == b"0123456789abcdef" * 8192
            record("long_output", begin, bytes=len(raw))
        state = await request(directory, "session_status")
        assert (
            state["state"] == "ready" and not state["active_job"] and not state["output_capture_incomplete"]
        )
        write_json(output / "audit-after.json", state)
    except BaseException as exc:
        write_json(
            output / "summary.json",
            {"passed": False, "stylus": args.stylus, "cases": cases, "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    finally:
        write_json(
            output / "stop.json", execute([*command, "stop", "--session", "api", "--force"], cwd=output)
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--innovus", default=shutil.which("innovus") or "innovus")
    parser.add_argument("--tmux", default=default_tmux())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stylus", action="store_true")
    parser.add_argument("--viewer", action="store_true", help="Attach a real read-only tmux viewer")
    asyncio.run(main(parser.parse_args()))
