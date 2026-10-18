"""A real PTY for testing native tmux clients, not an application terminal backend."""

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import termios


class NativeTerminal:
    def __init__(self, args):
        self.args = args
        self.output = bytearray()
        self.pid = None
        self.master = None

    async def __aenter__(self):
        pid, master = pty.fork()
        if pid == 0:
            env = os.environ.copy()
            env.pop("TMUX", None)
            env["TERM"] = "xterm-256color"
            os.execvpe(self.args[0], self.args, env)
        self.pid, self.master = pid, master
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 240, 0, 0))
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self.drain)
        return self

    def drain(self):
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if data:
            self.output.extend(data)
            del self.output[:-262144]
        else:
            asyncio.get_running_loop().remove_reader(self.master)

    def write(self, data):
        os.write(self.master, data)

    async def __aexit__(self, *exc):
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        for _ in range(100):
            if os.waitpid(self.pid, os.WNOHANG)[0]:
                return
            await asyncio.sleep(0.01)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
