from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

from .command_help import help_script, read_help_metadata
from .common import DEFAULT_PROMPT, MAX_PAGE, MAX_REQUEST, TERMINAL, BrokerError, atomic_json, tcl_word
from .tmux_backend import Tmux


class HealthProtocol(asyncio.DatagramProtocol):
    def __init__(self, broker):
        self.broker = broker

    def datagram_received(self, data, address):
        try:
            self.broker.capture = json.loads(data)
        except (ValueError, TypeError):
            return
        if self.broker.capture.get("error"):
            self.broker.unavailable("output_capture_incomplete")


class Broker:
    def __init__(self, directory: Path):
        self.directory = directory
        self.config = json.loads((directory / "session.json").read_text())
        self.tmux = Tmux(self.config["tmux"], self.config["tmux_socket"])
        self.state = "starting"
        self.ownership = "managed"
        self.agent_paused = False
        self.jobs: dict[str, dict] = {}
        self.queue: list[str] = []
        self.requests: dict[str, tuple[str, str]] = {}
        self.job_done: dict[str, asyncio.Event] = {}
        self.active: str | None = None
        self.native: dict | None = None
        self.native_owner_pid: int | None = None
        self.capture: dict = {}
        self.error: str | None = None
        self.log_offset = 0
        self.buffer = b""
        self.buffer_offset = 0
        self.stream_tail = b""
        self.prompt = re.compile(self.config.get("prompt", DEFAULT_PROMPT).encode())
        self.prefix = f"\x1eIMCP:{self.config['token']}:".encode()
        # Accept legacy frames as well as nonprinting OSC frames. Each envelope
        # is consumed in full so neither its bytes nor terminal banners enter job output.
        self.markers = tuple(
            (
                prefix,
                terminator,
                re.compile(
                    re.escape(prefix)
                    + rb"(CAPTURE|READY|START|DONE|RELEASE):([a-z0-9]+)"
                    + re.escape(terminator)
                ),
            )
            for prefix, terminator in (
                (self.prefix, b"\x1f"),
                (f"\x1b]777;IMCP:{self.config['token']}:".encode(), b"\x07"),
            )
        )
        self.start_seen = asyncio.Event()
        self.prompt_seen = asyncio.Event()
        self.waiters: dict[str, asyncio.Future] = {}
        self.acknowledged: set[str] = set()
        self.shutdown = asyncio.Event()
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.health_transport = None
        self.server = None

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def event(self, kind: str, **fields):
        with (self.directory / "events.jsonl").open("a") as out:
            out.write(json.dumps({"time": time.time(), "event": kind, **fields}) + "\n")
        self.changed.set()

    def persist(self):
        self.config.update(
            broker_pid=os.getpid(),
            state=self.state,
            ownership=self.ownership,
            agent_paused=self.agent_paused,
            error=self.error,
        )
        atomic_json(self.directory / "session.json", self.config)

    def save_job(self, job):
        path = self.directory / "jobs" / job["job_id"]
        atomic_json(path / "job.json", job)
        atomic_json(path / "output_index.json", {"spans": job["spans"]})

    def unavailable(self, reason):
        self.error = reason
        self.state = "unavailable"
        if self.active:
            job = self.jobs[self.active]
            job.update(
                state="unknown",
                error=reason,
                finished_at=time.time(),
                result_bytes=None,
                error_info_bytes=None,
                error_code_bytes=None,
            )
            self.save_job(job)
            self.notify_job(job)
            self.active = None
        self.persist()
        self.changed.set()

    async def start(self):
        loop = asyncio.get_running_loop()
        self.health_transport, _ = await loop.create_datagram_endpoint(
            lambda: HealthProtocol(self), local_addr=self.config["health_socket"], family=socket.AF_UNIX
        )
        self.server = await asyncio.start_unix_server(
            self.handle, path=self.config["broker_socket"], limit=MAX_REQUEST
        )
        os.chmod(self.config["broker_socket"], 0o600)
        self.persist()
        self.task(self.follow())
        self.task(self.scheduler())
        self.task(self.watch())
        self.task(self.initialize())

    async def initialize(self):
        try:
            cfg = self.directory / "tmux.conf"
            cfg.write_text(
                "set -g exit-unattached off\nset -g remain-on-exit on\n"
                "set -g history-limit 2000\nset -g window-size smallest\n"
                "set -g status-right 'MANAGED | C-b h: take control'\n"
            )
            pane = await self.tmux.cmd(
                "-f",
                str(cfg),
                "new-session",
                "-d",
                "-s",
                "innovus",
                "-x",
                "240",
                "-y",
                "50",
                "-P",
                "-F",
                "#{pane_id}",
                sys.executable,
                "-m",
                "innovus_mcp.launch_gate",
                str(self.directory),
            )
            self.tmux.pane = pane
            self.config["pane_id"] = pane
            self.persist()
            takeover = self.tmux.pipe_command(
                [sys.executable, "-m", "innovus_mcp.human", str(self.directory)]
            )
            # Read-only clients may run detach-client, but cannot run run-shell.
            # Replace the client with the takeover helper, then attach after broker grant.
            await self.tmux.cmd("bind-key", "h", "detach-client", "-E", takeover)
            pipe = self.tmux.pipe_command(
                [sys.executable, "-m", "innovus_mcp.capture_writer", str(self.directory)]
            )
            await self.tmux.cmd("pipe-pane", "-O", "-t", pane, pipe)
            await asyncio.wait_for(self.start_seen.wait(), 15)
            (self.directory / "launch.ready").touch()
            await asyncio.wait_for(self.prompt_seen.wait(), self.config["startup_timeout"])
            driver = Path(__file__).parent / "tcl" / "driver.tcl"
            identity = uuid.uuid4().hex
            command = (
                f"source {tcl_word(str(driver))} -quiet; ::innovus_mcp::init "
                f"{tcl_word(str(self.directory))} {self.config['token']} {identity}"
            )
            await self.exchange(command, identity)
            self.state = "ready"
            self.persist()
            self.event("session_ready")
        except Exception as exc:
            self.state = "startup_failed"
            self.error = str(exc) or type(exc).__name__
            self.persist()
            self.event("startup_failed", error=self.error)

    async def exchange(self, script, identity=None, timeout=15):
        identity = identity or uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiters[identity] = future
        self.prompt_seen.clear()
        self.stream_tail = b""
        try:
            await self.tmux.send(script)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiters.pop(identity, None)
            self.acknowledged.discard(identity)

    async def follow(self):
        path = self.directory / "pane.raw"
        while not self.shutdown.is_set():
            try:
                chunk = await asyncio.to_thread(self._read, path, self.log_offset, 262144)
                if chunk:
                    self.log_offset += len(chunk)
                    self.buffer += chunk
                    self.parse_stream()
                    continue
            except FileNotFoundError:
                pass
            except Exception as exc:
                self.unavailable(f"log_follow_error: {exc}")
            await asyncio.sleep(0.03)

    @staticmethod
    def _read(path, offset, size):
        with path.open("rb") as source:
            source.seek(offset)
            return source.read(size)

    def parse_stream(self):
        while self.buffer:
            candidates = [
                (start, terminator, marker)
                for prefix, terminator, marker in self.markers
                if (start := self.buffer.find(prefix)) != -1
            ]
            if not candidates:
                keep = 0
                for prefix, _, _ in self.markers:
                    for n in range(1, min(len(prefix), len(self.buffer)) + 1):
                        if self.buffer.endswith(prefix[:n]):
                            keep = max(keep, n)
                length = len(self.buffer) - keep
                if length:
                    self.data(self.buffer[:length], self.buffer_offset)
                    self.buffer = self.buffer[length:]
                    self.buffer_offset += length
                break
            start, terminator, marker = min(candidates, key=lambda candidate: candidate[0])
            if start:
                self.data(self.buffer[:start], self.buffer_offset)
                self.buffer = self.buffer[start:]
                self.buffer_offset += start
            end = self.buffer.find(terminator)
            if end == -1 and len(self.buffer) < 200:
                break
            match = marker.match(self.buffer)
            if not match:
                self.data(self.buffer[:1], self.buffer_offset)
                self.buffer = self.buffer[1:]
                self.buffer_offset += 1
                continue
            self.frame(match[1].decode(), match[2].decode(), self.buffer_offset)
            self.buffer = self.buffer[match.end() :]
            self.buffer_offset += match.end()

    def data(self, chunk: bytes, offset: int):
        self.stream_tail = (self.stream_tail + chunk)[-4096:]
        if self.active:
            job = self.jobs[self.active]
            if job.get("started_marker") and not job.get("done_marker"):
                spans = job["spans"]
                if spans and spans[-1][1] == offset:
                    spans[-1][1] += len(chunk)
                else:
                    spans.append([offset, offset + len(chunk)])
                job["output_bytes"] += len(chunk)
                previous = job.get("diagnostic_tail", "").encode("latin1")
                window = previous + chunk
                for match in re.finditer(rb"\*\*(ERROR|WARN(?:ING)?)\b", window, re.IGNORECASE):
                    if match.end() <= len(previous):
                        continue
                    key = "error_count" if match[1].upper() == b"ERROR" else "warning_count"
                    job[key] = job.get(key, 0) + 1
                    samples = job.setdefault("diagnostic_samples", [])
                    if len(samples) < 20:
                        samples.append(window[match.start() : match.start() + 160].decode("utf-8", "replace"))
                job["diagnostic_tail"] = window[-16:].decode("latin1")
        if self.prompt.search(self.stream_tail):
            self.prompt_seen.set()
            for identity in list(self.acknowledged):
                future = self.waiters.get(identity)
                if future and not future.done():
                    future.set_result(True)
            if self.active and self.jobs[self.active].get("done_marker"):
                self.finish_job(self.jobs[self.active])

    def frame(self, kind, identity, offset):
        self.stream_tail = b""
        if kind == "CAPTURE":
            self.start_seen.set()
        elif kind == "READY":
            if identity in self.waiters:
                self.acknowledged.add(identity)
        elif kind == "START":
            if identity != self.active:
                self.unavailable("unexpected_job_start")
            else:
                self.jobs[identity]["started_marker"] = True
        elif kind == "DONE":
            if identity in self.jobs and self.jobs[identity]["state"] == "unknown":
                return
            if identity != self.active:
                self.unavailable("unexpected_job_completion")
            else:
                self.jobs[identity]["done_marker"] = True
        elif kind == "RELEASE" and self.ownership == "native" and self.native:
            if self.native["native_interval_id"] == identity:
                self.ownership = "return_pending"
                self.prompt_seen.clear()
                self.native["end_offset"] = offset
                self.task(self.return_native())

    def finish_job(self, job):
        try:
            path = self.directory / "jobs" / job["job_id"] / "completion.meta"
            values = dict(line.split("=", 1) for line in path.read_text().splitlines())
            if values.get("version") != "1":
                raise ValueError("unsupported Tcl completion manifest")
            code = int(values["code"])
            for artifact in ("result", "error_info", "error_code"):
                artifact_path = path.parent / f"{artifact}.txt"
                job[f"{artifact}_bytes"] = artifact_path.stat().st_size if artifact_path.is_file() else None
            job.update(
                state="succeeded" if code == 0 else "failed",
                tcl_code=code,
                error=None if code == 0 else ("tcl_error" if code == 1 else "tcl_non_ok"),
                finished_at=time.time(),
            )
            if "help_request" in job:
                try:
                    job["help"] = read_help_metadata(path.parent / "help.meta")
                except (OSError, ValueError, KeyError) as exc:
                    job["help"] = {
                        **job["help_request"],
                        "status": "unavailable",
                        "complete": False,
                        "message": f"Help receipt unavailable: {exc}",
                    }
            self.save_job(job)
            self.active = None
            self.state = "ready"
            self.persist()
            self.event("job_finished", job_id=job["job_id"], state=job["state"])
            self.notify_job(job)
        except Exception as exc:
            self.unavailable(f"invalid_completion: {exc}")

    async def scheduler(self):
        while not self.shutdown.is_set():
            async with self.lock:
                if self.state == "ready" and self.ownership == "managed" and not self.active:
                    eligible = next(
                        (
                            j
                            for j in self.queue
                            if not (self.agent_paused and self.jobs[j]["origin"] == "agent")
                        ),
                        None,
                    )
                    if eligible:
                        self.queue.remove(eligible)
                        job = self.jobs[eligible]
                        job.update(state="running", started_at=time.time())
                        self.active = eligible
                        self.state = "busy"
                        self.prompt_seen.clear()
                        self.stream_tail = b""
                        self.save_job(job)
                        try:
                            await self.tmux.send(f"::innovus_mcp::run {eligible}")
                        except Exception as exc:
                            self.unavailable(f"delivery_uncertain: {exc}")
            await asyncio.sleep(0.03)

    async def watch(self):
        while not self.shutdown.is_set():
            await asyncio.sleep(1)
            if self.state in {"starting", "startup_failed", "stopping", "stopped"}:
                continue
            try:
                dead = await self.tmux.cmd("display-message", "-p", "-t", self.tmux.pane, "#{pane_dead}")
                if dead == "1":
                    self.unavailable("innovus_exited")
                    self.state = "exited"
                    self.persist()
                    continue
                if self.capture.get("pid"):
                    os.kill(self.capture["pid"], 0)
                pipe = await self.tmux.cmd("display-message", "-p", "-t", self.tmux.pane, "#{pane_pipe}")
                if pipe != "1":
                    self.unavailable("capture_pipe_lost")
            except (OSError, BrokerError) as exc:
                if self.state not in {"exited", "unavailable"}:
                    self.unavailable(f"session_lost: {exc}")

    async def acquire_native(self, owner_pid=None):
        async with self.lock:
            if owner_pid is not None and (
                isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0
            ):
                raise BrokerError("invalid_owner", "Native owner PID must be a positive integer")
            if self.ownership == "native":
                writers = [c for c in await self.tmux.clients() if c["readonly"] != "1"]
                if writers:
                    raise BrokerError("native_in_use", "A writable human client is already attached")
                if self.native_owner_pid is not None and self.native_owner_pid != owner_pid:
                    try:
                        os.kill(self.native_owner_pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise BrokerError("native_in_use", "Another terminal is completing its takeover")
                self.native_owner_pid = owner_pid
                self.native["owner_pid"] = owner_pid
                self.event("native_reattached", **self.native)
                return self.status()
            if self.ownership != "managed" or self.state not in {"ready", "busy"}:
                raise BrokerError("not_ready", "Session cannot grant native ownership")
            self.ownership = "handoff_pending"
            self.agent_paused = True
            self.native_owner_pid = owner_pid
            await self.tmux.cmd("set-option", "-g", "status-right", "WAITING FOR HUMAN TAKEOVER")
            self.persist()
            self.task(self.grant_native())
            return self.status()

    async def grant_native(self):
        try:
            while self.active:
                await asyncio.sleep(0.05)
            if self.state != "ready":
                raise BrokerError("not_ready", "No confirmed Innovus prompt for handoff")
            identity = uuid.uuid4().hex
            await self.exchange(f"::innovus_mcp::set_native {identity}", identity)
            self.native = {
                "native_interval_id": identity,
                "start_offset": self.log_offset,
                "started_at": time.time(),
                "owner_pid": self.native_owner_pid,
            }
            self.ownership = "native"
            await self.tmux.cmd("set-option", "-w", "-t", "innovus:0", "window-size", "latest")
            await self.tmux.cmd("set-option", "-g", "status-right", "NATIVE HUMAN CONTROL")
            self.persist()
            self.event("native_granted", **self.native)
        except Exception as exc:
            self.unavailable(f"handoff_failed: {exc}")

    async def return_native(self):
        try:
            await self.tmux.readonly_writers()
            if any(c["readonly"] != "1" for c in await self.tmux.clients()):
                raise BrokerError("native_in_use", "Writable client remains attached")
            await asyncio.wait_for(self.prompt_seen.wait(), 15)
            await self.tmux.cmd("set-option", "-w", "-t", "innovus:0", "window-size", "manual")
            await self.tmux.cmd("resize-window", "-t", "innovus:0", "-x", "240", "-y", "50")
            identity = uuid.uuid4().hex
            await self.exchange(f"::innovus_mcp::ping {identity}", identity)
            self.native["finished_at"] = time.time()
            with (self.directory / "native_intervals.jsonl").open("a") as out:
                out.write(json.dumps(self.native) + "\n")
            self.event("native_returned", **self.native)
            self.ownership = "managed"
            self.agent_paused = False
            self.native_owner_pid = None
            await self.tmux.cmd("set-option", "-g", "status-right", "MANAGED | C-b h: take control")
            self.persist()
        except Exception as exc:
            self.unavailable(f"return_failed: {exc}")

    def status(self):
        return {
            "session": self.config["name"],
            "session_id": self.config["session_id"],
            "state": self.state,
            "ownership": self.ownership,
            "agent_paused": self.agent_paused,
            "active_job": self.active,
            "queue": self.queue.copy(),
            "error": self.error,
            "native_interval": self.native,
            "capture": self.capture,
            "output_capture_incomplete": bool(self.capture.get("error") or self.capture.get("lost_bytes")),
            "log_follow_offset": self.log_offset,
            "tmux_socket": self.config["tmux_socket"],
            "tmux": self.config["tmux"],
            "pane_id": self.tmux.pane,
            "attach_command": self.tmux.attach_command(),
            "artifacts": str(self.directory),
        }

    async def dispatch(self, method, params):
        if method == "session_status":
            return self.status()
        if method == "command_help":
            command, detail = params["command"], params.get("detail", "full")
            script = help_script(command, detail)
            return await self.dispatch(
                "submit",
                {
                    **params,
                    "script": script,
                    "origin": "agent",
                    "intent": "query",
                    "help_request": {"command": command, "requested_detail": detail},
                },
            )
        if method == "submit":
            options = self.observation_options(params)
            async with self.lock:
                script, req = params["script"], params["request_id"]
                origin, intent = params.get("origin", "agent"), params.get("intent", "query")
                if not isinstance(script, str) or len(script.encode()) > 1024 * 1024:
                    raise BrokerError("invalid_script", "Script must be a string of at most 1 MiB")
                if not isinstance(req, str) or not 1 <= len(req) <= 128:
                    raise BrokerError("invalid_request_id", "Supply a request ID of 1-128 characters")
                if origin not in {"human", "agent"} or intent not in {"query", "mutate"}:
                    raise BrokerError("invalid_intent", "Invalid origin or intent")
                digest = hashlib.sha256(json.dumps([script, origin, intent]).encode()).hexdigest()
                if req in self.requests:
                    old_digest, identity = self.requests[req]
                    if old_digest != digest:
                        raise BrokerError(
                            "request_conflict", "Request ID already used with a different payload"
                        )
                    job = self.jobs[identity]
                else:
                    if self.ownership != "managed":
                        raise BrokerError(
                            "native_control", "Native handoff/control pauses managed submission"
                        )
                    if self.state not in {"ready", "busy"}:
                        raise BrokerError("not_ready", self.error or "Innovus is not ready")
                    if origin == "agent" and self.agent_paused:
                        raise BrokerError("agent_paused", "Human paused agent dispatch")
                    if len(self.queue) >= self.config.get("queue_size", 32):
                        raise BrokerError("queue_full", "Pending job queue is full")
                    identity = uuid.uuid4().hex
                    job = dict(
                        job_id=identity,
                        request_id=req,
                        sequence=len(self.jobs) + 1,
                        origin=origin,
                        intent=intent,
                        state="queued",
                        submitted_at=time.time(),
                        preview=script[:256],
                        output_bytes=0,
                        result_bytes=None,
                        error_info_bytes=None,
                        error_code_bytes=None,
                        spans=[],
                        interrupt_requested=False,
                        error_count=0,
                        warning_count=0,
                    )
                    if "help_request" in params:
                        job["help_request"] = params["help_request"]
                        job["help"] = {**params["help_request"], "status": "pending", "complete": False}
                    path = self.directory / "jobs" / identity
                    path.mkdir(parents=True)
                    (path / "request.tcl").write_text(script, encoding="utf-8")
                    self.save_job(job)
                    self.jobs[identity] = job
                    self.requests[req] = (digest, identity)
                    self.queue.append(identity)
                    self.event("job_accepted", job_id=identity, origin=origin, intent=intent)
            # Waiting and disk-backed response reads must not hold the submission lock.
            if options["wait_ms"] == 0:
                return self.job_view(job)
            return await self.observe_job(job, **options)
        if method == "get_job":
            job = self.get_job(params["job_id"])
            options = self.observation_options(params)
            return await self.observe_job(job, **options, summary=True)
        if method == "list_jobs":
            limit = min(max(int(params.get("limit", 20)), 1), 100)
            rows = [j for j in self.jobs.values() if j["sequence"] > int(params.get("after_sequence", 0))]
            rows = rows[:limit]
            return {
                "jobs": [self.job_view(j) for j in rows],
                "next_sequence": rows[-1]["sequence"] if rows else params.get("after_sequence", 0),
            }
        if method == "read_job_output":
            job = self.get_job(params["job_id"])
            snapshot = {**job, "spans": [span.copy() for span in job["spans"]]}
            return await asyncio.to_thread(
                self.output_page, snapshot, **{k: v for k, v in params.items() if k != "job_id"}
            )
        if method == "read_job_result":
            job = self.get_job(params["job_id"])
            artifact = params.get("artifact", "result")
            names = {"result": "result.txt", "error_info": "error_info.txt", "error_code": "error_code.txt"}
            if artifact not in names:
                raise BrokerError("invalid_artifact", "Choose result, error_info or error_code")
            path = self.directory / "jobs" / job["job_id"] / names[artifact]
            if job["state"] not in TERMINAL:
                return {"available": False, "state": job["state"]}
            return await asyncio.to_thread(self.file_page, path, params)
        if method == "read_session_output":
            return await asyncio.to_thread(self.session_page, params)
        if method == "cancel_job":
            async with self.lock:
                job = self.get_job(params["job_id"])
                if job["state"] == "queued":
                    self.queue.remove(job["job_id"])
                    job.update(state="cancelled", finished_at=time.time())
                    self.save_job(job)
                    self.notify_job(job)
                return self.job_view(job)
        if method == "interrupt_job":
            async with self.lock:
                job = self.get_job(params["job_id"])
                if self.ownership not in {"managed", "handoff_pending"}:
                    raise BrokerError("native_control", "Human owns terminal input")
                sent = False
                if job["state"] in TERMINAL:
                    reason = "terminal"
                elif job["job_id"] != self.active:
                    reason = "not_active"
                elif job["interrupt_requested"]:
                    reason = "already_requested"
                else:
                    job["interrupt_requested"] = True
                    await self.tmux.interrupt()
                    self.save_job(job)
                    sent, reason = True, "sent"
                return {**self.job_view(job), "interrupt_sent": sent, "reason": reason}
        if method == "acquire_native":
            return await self.acquire_native(params.get("owner_pid"))
        if method in {"pause_agent", "resume_agent"}:
            async with self.lock:
                if method == "resume_agent" and (
                    self.ownership != "managed" or self.state not in {"ready", "busy"}
                ):
                    raise BrokerError(
                        "native_control", "Return ownership and confirm readiness before resuming"
                    )
                self.agent_paused = method == "pause_agent"
                if self.ownership == "managed":
                    await self.tmux.cmd(
                        "set-option",
                        "-g",
                        "status-right",
                        "MANAGED - AGENT PAUSED" if self.agent_paused else "MANAGED - AGENT ENABLED",
                    )
                self.persist()
                return self.status()
        if method == "stop":
            return await self.stop(bool(params.get("force", False)))
        raise BrokerError("unknown_method", method)

    def get_job(self, identity):
        if identity not in self.jobs:
            raise BrokerError("unknown_job", str(identity))
        return self.jobs[identity]

    @staticmethod
    def observation_options(params):
        options = {}
        for name, default, upper in (
            ("wait_ms", 0, 10000),
            ("max_output_bytes", 16384, MAX_PAGE),
            ("max_result_bytes", 4096, MAX_PAGE),
        ):
            value = params.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise BrokerError("invalid_observation", f"{name} must be an integer")
            options[name] = min(max(value, 0), upper)
        return options

    def notify_job(self, job):
        done = self.job_done.pop(job["job_id"], None)
        if done is not None:
            done.set()

    async def observe_job(self, job, wait_ms, max_output_bytes, max_result_bytes, summary=False):
        if wait_ms and job["state"] not in TERMINAL:
            done = self.job_done.setdefault(job["job_id"], asyncio.Event())
            try:
                await asyncio.wait_for(done.wait(), wait_ms / 1000)
            except TimeoutError:
                pass  # The observation deadline never cancels the Innovus job.
        snapshot = {
            **job,
            "spans": [span.copy() for span in job["spans"]],
            "diagnostic_samples": job.get("diagnostic_samples", []).copy(),
        }
        return await asyncio.to_thread(
            self.observed_view, snapshot, max_output_bytes, max_result_bytes, summary
        )

    def observed_view(self, job, max_output_bytes, max_result_bytes, summary):
        result = self.job_view(job)
        if summary:
            # Small output is returned once. Large summaries cover disjoint byte ranges.
            max_output_bytes = min(max_output_bytes, 6144)
        if max_output_bytes:
            split = summary and job["output_bytes"] > max_output_bytes and max_output_bytes > 2048
            head_bytes = 2048 if split else max_output_bytes
            result["output"] = self.output_page(job, offset=0, max_bytes=head_bytes)
            if split:
                tail_bytes = max_output_bytes - head_bytes
                result["output_tail"] = self.output_page(
                    job, offset=job["output_bytes"] - tail_bytes, max_bytes=tail_bytes
                )
        if summary:
            result["summary_truncated"] = job["output_bytes"] > max_output_bytes
            budget = 2048
            result["diagnostic_samples"] = []
            for sample in job.get("diagnostic_samples", []):
                text = sample.encode("utf-8")[:budget].decode("utf-8", "ignore")
                result["diagnostic_samples"].append(text)
                budget -= len(text.encode("utf-8"))
                if budget <= 0:
                    break
        if (
            max_result_bytes
            and job["state"] in {"succeeded", "failed"}
            and job.get("result_bytes") is not None
        ):
            result["result"] = self.file_page(
                self.directory / "jobs" / job["job_id"] / "result.txt", {"max_bytes": max_result_bytes}
            )
        return result

    def job_view(self, job):
        result = {
            k: v
            for k, v in job.items()
            if k not in {"spans", "started_marker", "done_marker", "diagnostic_tail", "diagnostic_samples"}
        }
        result["terminal"] = job["state"] in TERMINAL
        result["output_capture_incomplete"] = self.status()["output_capture_incomplete"]
        return result

    @staticmethod
    def page(data, offset, available, view="text"):
        if view not in {"text", "base64"}:
            raise BrokerError("invalid_view", "Choose text or base64")
        result = {
            "offset": offset,
            "next_offset": offset + len(data),
            "available_bytes": available,
            "has_more": offset + len(data) < available,
        }
        if view == "base64":
            result["base64"] = base64.b64encode(data).decode()
        else:
            text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            result["text"] = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", "", text)
        return result

    def output_page(self, job, offset=0, max_bytes=16384, view="text"):
        offset = max(0, int(offset))
        size = min(MAX_PAGE, max(1, int(max_bytes)))
        available = job["output_bytes"]
        spans = [span.copy() for span in job["spans"]]
        remaining, logical = size, 0
        data = bytearray()
        if spans:
            with (self.directory / "pane.raw").open("rb") as source:
                for start, end in spans:
                    length = end - start
                    if offset < logical + length and remaining:
                        inside = max(0, offset - logical)
                        source.seek(start + inside)
                        chunk = source.read(min(remaining, length - inside))
                        data.extend(chunk)
                        remaining -= len(chunk)
                    logical += length
        return {
            **self.page(bytes(data), offset, available, view),
            "terminal": job["state"] in TERMINAL,
            "output_capture_incomplete": self.status()["output_capture_incomplete"],
        }

    def file_page(self, path, params):
        offset = max(0, int(params.get("offset", 0)))
        size = min(MAX_PAGE, max(1, int(params.get("max_bytes", 16384))))
        if not path.exists():
            return {"available": False}
        return {
            "available": True,
            **self.page(
                self._read(path, offset, size), offset, path.stat().st_size, params.get("view", "text")
            ),
        }

    def session_page(self, params):
        interval_id = params.get("native_interval_id")
        if not interval_id:
            return self.file_page(self.directory / "pane.raw", params)
        intervals = []
        path = self.directory / "native_intervals.jsonl"
        if path.exists():
            intervals = [json.loads(line) for line in path.read_text().splitlines()]
        if self.native:
            intervals.append(self.native)
        interval = next((i for i in intervals if i["native_interval_id"] == interval_id), None)
        if not interval:
            raise BrokerError("unknown_interval", str(interval_id))
        start = interval["start_offset"]
        end = interval.get("end_offset", self.log_offset)
        relative = max(0, int(params.get("offset", 0)))
        size = min(MAX_PAGE, max(1, int(params.get("max_bytes", 16384))), max(0, end - start - relative))
        data = self._read(self.directory / "pane.raw", start + relative, size)
        return self.page(data, relative, end - start, params.get("view", "text"))

    async def stop(self, force):
        async with self.lock:
            if not force:
                if self.active or self.ownership != "managed" or self.state != "ready":
                    raise BrokerError("busy", "Stop requires an idle managed session; --force terminates it")
                self.state = "stopping"
                await self.tmux.send("exit")
                for _ in range(50):
                    dead = await self.tmux.cmd("display-message", "-p", "-t", self.tmux.pane, "#{pane_dead}")
                    if dead == "1":
                        break
                    await asyncio.sleep(0.1)
                else:
                    self.unavailable("graceful_exit_unconfirmed")
                    raise BrokerError("exit_unconfirmed", "Innovus did not exit; no force was applied")
            await self.tmux.cmd("kill-server")
            if self.active:
                self.unavailable("session_force_stopped")
            for identity in self.queue:
                self.jobs[identity].update(state="cancelled", finished_at=time.time())
                self.save_job(self.jobs[identity])
                self.notify_job(self.jobs[identity])
            self.queue.clear()
            self.state = "stopped"
            self.error = None
            self.persist()
            asyncio.get_running_loop().call_later(0.2, self.shutdown.set)
            return self.status()

    async def handle(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 15)
            if not raw or len(raw) >= MAX_REQUEST:
                raise BrokerError("invalid_request", "Missing or oversized JSON request")
            envelope = json.loads(raw)
            if envelope.get("version") != 1:
                raise BrokerError("protocol_version", "Expected broker protocol version 1")
            response = {"result": await self.dispatch(envelope["method"], envelope.get("params", {}))}
        except BrokerError as exc:
            response = {"error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            response = {"error": {"code": "invalid_request", "message": str(exc)}}
        try:
            writer.write(json.dumps(response, ensure_ascii=True).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self):
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown.set)
        await self.shutdown.wait()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.state != "stopped":
            self.unavailable("broker_stopped_no_reattachment")
        self.health_transport.close()


def main():
    asyncio.run(Broker(Path(sys.argv[1]).resolve()).run())


if __name__ == "__main__":
    main()
