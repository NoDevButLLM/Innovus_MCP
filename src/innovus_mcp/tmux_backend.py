from __future__ import annotations

import asyncio
import os
import shlex
import uuid

from .common import BrokerError


class Tmux:
    def __init__(self, executable: str, socket: str):
        self.executable, self.socket = executable, socket
        self.pane = "%0"

    async def cmd(self, *args: str, timeout: float = 5, input_data: bytes | None = None) -> str:
        env = os.environ.copy()
        env.pop("TMUX", None)
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "-S",
            self.socket,
            *map(str, args),
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(input_data), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise BrokerError("tmux_timeout", "tmux delivery/status outcome is uncertain") from None
        if process.returncode:
            raise BrokerError("tmux_error", f"{args[0]}: {err.decode(errors='replace').strip()}")
        return out.decode(errors="replace").strip()

    async def send(self, script: str):
        await self.write_input(script.encode("utf-8") + b"\r")

    async def write_input(self, data: bytes):
        # tmux 3.7 send-keys may select an attached read-only client and reject
        # broker input. A private paste buffer targets the pane independently.
        buffer = "imcp-" + uuid.uuid4().hex
        await self.cmd("load-buffer", "-b", buffer, "-", input_data=data)
        await self.cmd("paste-buffer", "-r", "-d", "-b", buffer, "-t", self.pane)

    async def interrupt(self):
        await self.write_input(b"\x03")

    async def clients(self):
        text = await self.cmd("list-clients", "-F", "#{client_name}|#{client_pid}|#{client_readonly}")
        return [
            dict(zip(("name", "pid", "readonly"), line.split("|"), strict=True))
            for line in text.splitlines()
            if line
        ]

    async def readonly_writers(self):
        for client in await self.clients():
            if client["readonly"] != "1":
                await self.cmd("switch-client", "-c", client["name"], "-r")

    def attach_command(self):
        return shlex.join(self.attach_args(readonly=True))

    def attach_args(self, readonly: bool):
        return [
            self.executable,
            "-S",
            self.socket,
            "attach-session",
            *(["-r"] if readonly else []),
            "-t",
            "innovus",
        ]

    @staticmethod
    def pipe_command(args: list[str]) -> str:
        # pipe-pane expands tmux formats before invoking a shell.
        return ("exec " + shlex.join(args)).replace("#", "##")
