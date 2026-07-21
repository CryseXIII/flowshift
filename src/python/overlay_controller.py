"""Thread-safe lifecycle controller for the isolated FlowShift overlay host."""
from __future__ import annotations

import base64
import ipaddress
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Client
from urllib.parse import urlparse

import overlay_protocol as protocol


DEFAULT_OVERLAY_URL = "http://127.0.0.1:5000/overlay.html"
MAX_COMMAND_QUEUE_SIZE = 64
CREATE_NO_WINDOW = 0x08000000


def _timeout(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite number greater than zero")
    return float(value)


def _target_result(reason, supported=False):
    return {"ok": False, "supported": supported, "reason": str(reason)}


class _Command:
    __slots__ = ("name", "args", "event", "result", "error")

    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.event = threading.Event()
        self.result = None
        self.error = None


class OverlayController:
    """Own one reusable overlay process and isolate callers from IPC work."""

    def __init__(
            self, host_script=None, overlay_url=DEFAULT_OVERLAY_URL,
            python_executable=None, enabled=True, headless=False,
            startup_timeout=10.0, request_timeout=2.0,
            command_queue_size=64, restart_delays=(1, 2, 5), log=None):
        self.host_script = os.path.abspath(
            host_script or os.path.join(os.path.dirname(__file__), "overlay_host.py")
        )
        self.overlay_url = self._validate_url(overlay_url)
        self.python_executable = os.path.abspath(python_executable or sys.executable)
        self.enabled = bool(enabled)
        self.headless = bool(headless)
        self.startup_timeout = _timeout(startup_timeout, "startup_timeout")
        self.request_timeout = _timeout(request_timeout, "request_timeout")
        if (isinstance(command_queue_size, bool)
                or not isinstance(command_queue_size, int)
                or not 1 <= command_queue_size <= MAX_COMMAND_QUEUE_SIZE):
            raise ValueError("command_queue_size must be an integer from 1 to 64")
        try:
            delays = tuple(min(_timeout(value, "restart delay"), 5.0)
                           for value in restart_delays)
        except TypeError as exc:
            raise ValueError("restart_delays must be a non-empty iterable") from exc
        if not delays:
            raise ValueError("restart_delays must not be empty")
        self.restart_delays = delays
        self._log_callback = log or (lambda level, message: None)

        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._process = None
        self._host_pid = None
        self._pipe_address = None
        self._connection = None
        self._tracker = protocol.RequestTracker(max_pending=command_queue_size + 4)
        self._reader_thread = None
        self._watcher_threads = []
        self._stderr_threads = []
        self._ready = False
        self._visible = False
        self._mode = None
        self._last_error = None
        self._restart_count = 0
        self._restart_schedule_count = 0
        self._restart_at = 0.0
        self._scheduled_pid = None
        self._ever_ready = False
        self._shutting_down = False
        self._shutdown_complete = False
        self._command_queue = queue.Queue(maxsize=command_queue_size)
        self._worker_thread = threading.Thread(
            target=self._command_worker, name="overlay-command-worker", daemon=True,
        )
        self._worker_thread.start()
        self._log("DEBUG", "overlay controller init")

    @staticmethod
    def _validate_url(value):
        value = str(value or "").strip()
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("overlay_url must be a valid HTTP(S) URL") from exc
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password
                or port is not None and not 1 <= port <= 65535):
            raise ValueError("overlay_url must be an absolute HTTP(S) URL without credentials")
        hostname = parsed.hostname.lower()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if not loopback:
            raise ValueError("overlay_url must use a loopback host")
        return value

    def _log(self, level, message):
        try:
            self._log_callback(str(level), str(message))
        except Exception:
            pass

    @property
    def process_pid(self):
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return None
            return self._host_pid or process.pid

    @property
    def owned_process_pid(self):
        return self.process_pid

    @property
    def pipe_address(self):
        with self._lock:
            return self._pipe_address

    def is_alive(self):
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def snapshot(self):
        with self._lock:
            alive = self._process is not None and self._process.poll() is None
            return {
                "enabled": self.enabled,
                "process_alive": alive,
                "ipc_connected": self._connection is not None,
                "ready": self._ready,
                "mode": self._mode,
                "visible": self._visible,
                "restart_count": self._restart_count,
                "last_error": self._last_error,
            }

    def _schedule_restart(self, reason, pid=None):
        with self._lock:
            if self._shutting_down or not self.enabled:
                return
            if pid is not None and self._scheduled_pid == pid:
                return
            index = min(self._restart_schedule_count, len(self.restart_delays) - 1)
            delay = self.restart_delays[index]
            self._restart_schedule_count += 1
            self._restart_at = time.monotonic() + delay
            self._scheduled_pid = pid
            self._last_error = str(reason)
        self._log("WARN", f"overlay restart schedule in {delay:g}s: {reason}")

    def _disconnect(self, connection, reason, schedule=True, pid=None):
        should_log = False
        with self._lock:
            if connection is not None and self._connection is not connection:
                return
            if self._connection is not None:
                should_log = True
            self._connection = None
            self._ready = False
            self._visible = False
            self._mode = None
            self._last_error = str(reason)
        self._tracker.cancel_all(reason)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if should_log:
            self._log("WARN" if schedule else "DEBUG", f"overlay IPC disconnect: {reason}")
        if schedule:
            self._schedule_restart(reason, pid=pid)

    def _reader(self, connection, pid):
        while True:
            try:
                message = protocol.receive_message(connection)
            except protocol.OverlayProtocolError as exc:
                with self._lock:
                    if self._shutting_down:
                        return
                self._disconnect(connection, exc.message, schedule=True, pid=pid)
                return
            message_type = message["type"]
            request_id = message["request_id"]
            with self._lock:
                if self._connection is not connection:
                    return
                if message_type == "overlay_visible":
                    self._visible = True
                    self._mode = message["payload"].get("mode", self._mode)
                elif message_type == "overlay_hidden":
                    self._visible = False
                    self._mode = None
            if request_id is not None:
                try:
                    self._tracker.resolve(message)
                except protocol.OverlayProtocolError as exc:
                    self._log("WARN", f"overlay invalid IPC: {exc.code}: {exc.message}")
            elif message_type == "hello":
                if message["payload"]["role"] != "host":
                    self._log("WARN", "overlay invalid IPC: inconsistent host hello")
                else:
                    with self._lock:
                        self._host_pid = message["payload"]["pid"]
            elif message_type not in ("overlay_event", "overlay_hidden"):
                self._log("WARN", f"overlay invalid IPC: unsolicited {message_type}")

    def _watch_process(self, process):
        code = process.wait()
        with self._lock:
            connection = self._connection if self._process is process else None
            shutting_down = self._shutting_down
        self._log(
            "DEBUG" if shutting_down or not code else "WARN",
            f"overlay host exit pid={process.pid} code={code}",
        )
        if not shutting_down and self._process is process:
            self._disconnect(
                connection, f"overlay host exited with code {code}",
                schedule=True, pid=process.pid,
            )

    def _read_stderr(self, process):
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                text = line.strip()
                if text:
                    self._log("WARN", f"overlay host: {text}")
                    with self._lock:
                        if self._process is process and not self._ready:
                            self._last_error = text
        finally:
            stream.close()

    def _request(self, message_type, payload=None, timeout=None):
        request_id = protocol.new_request_id()
        self._tracker.register(request_id)
        with self._lock:
            connection = self._connection
            pid = self._process.pid if self._process is not None else None
        if connection is None:
            self._tracker.cancel_all("overlay IPC is not connected")
            return self._tracker.wait(request_id, 0)
        try:
            with self._send_lock:
                protocol.send_message(connection, message_type, payload, request_id)
        except protocol.OverlayProtocolError as exc:
            self._disconnect(connection, exc.message, schedule=True, pid=pid)
            return self._tracker.wait(request_id, 0)
        try:
            response = self._tracker.wait(request_id, timeout or self.request_timeout)
        except protocol.OverlayProtocolError as exc:
            if exc.code == "timeout":
                self._log("WARN", f"overlay timeout: {message_type}")
            raise
        if response["type"] == "error":
            error = response["payload"]
            raise protocol.OverlayProtocolError(
                error["code"], error["message"], request_id,
            )
        return response

    def _terminate_process(self, process, grace=1.0):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass

    def ensure_started(self):
        if not self.enabled:
            return False
        with self._start_lock:
            with self._lock:
                if self._shutting_down:
                    return False
                if (self._process is not None and self._process.poll() is None
                        and self._connection is not None and self._ready):
                    return True
                if self._ever_ready and time.monotonic() < self._restart_at:
                    return False
                old_process = self._process
                old_connection = self._connection
                is_restart = self._ever_ready
            if old_connection is not None:
                self._disconnect(old_connection, "replacing disconnected overlay host", schedule=False)
            self._terminate_process(old_process)

            pipe = rf"\\.\pipe\FlowShiftOverlay-{os.getpid()}-{uuid.uuid4().hex}"
            auth_key = os.urandom(32)
            with self._lock:
                self._pipe_address = pipe
            command = [
                self.python_executable,
                self.host_script,
                "--pipe", pipe,
                "--auth-key", base64.b64encode(auth_key).decode("ascii"),
                "--url", self.overlay_url,
                "--parent-pid", str(os.getpid()),
                "--startup-timeout", str(self.startup_timeout),
            ]
            if self.headless:
                command.append("--headless")
            creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self._log("INFO", f"overlay host start headless={self.headless}")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=os.path.dirname(self.host_script) or None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=creationflags,
                )
            except (OSError, ValueError) as exc:
                self._schedule_restart(f"could not start overlay host: {exc}")
                self._log("ERROR", f"overlay restart failure: {exc}")
                return False

            with self._lock:
                self._process = process
                self._host_pid = None
                self._connection = None
                self._ready = False
                self._visible = False
                self._scheduled_pid = None
            stderr_thread = threading.Thread(
                target=self._read_stderr, args=(process,),
                name=f"overlay-stderr-{process.pid}", daemon=True,
            )
            watcher = threading.Thread(
                target=self._watch_process, args=(process,),
                name=f"overlay-process-{process.pid}", daemon=True,
            )
            with self._lock:
                self._stderr_threads.append(stderr_thread)
                self._watcher_threads.append(watcher)
            stderr_thread.start()
            watcher.start()

            deadline = time.monotonic() + self.startup_timeout
            connection = None
            last_error = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    connection = Client(pipe, family="AF_PIPE", authkey=auth_key)
                    break
                except (OSError, EOFError) as exc:
                    last_error = exc
                    time.sleep(0.025)
            if connection is None:
                reason = (f"overlay startup failed: {last_error}"
                          if last_error else f"overlay host exited with code {process.poll()}")
                self._terminate_process(process)
                self._schedule_restart(reason, pid=process.pid)
                self._log("ERROR", f"overlay restart failure: {reason}")
                return False

            with self._lock:
                if self._process is not process or self._shutting_down:
                    connection.close()
                    self._terminate_process(process)
                    return False
                self._connection = connection
            self._log("DEBUG", f"overlay IPC connect pid={process.pid}")
            reader = threading.Thread(
                target=self._reader, args=(connection, process.pid),
                name=f"overlay-reader-{process.pid}", daemon=True,
            )
            with self._lock:
                self._reader_thread = reader
            reader.start()
            try:
                response = self._request(
                    "hello", {"role": "runtime", "pid": os.getpid()},
                    timeout=self.startup_timeout,
                )
                with self._lock:
                    host_pid = self._host_pid
                if (response["type"] != "ready" or host_pid is None
                        or response["payload"]["pid"] != host_pid):
                    raise protocol.OverlayProtocolError(
                        "invalid_handshake", "host returned an inconsistent ready message"
                    )
            except protocol.OverlayProtocolError as exc:
                self._disconnect(connection, exc.message, schedule=True, pid=process.pid)
                self._terminate_process(process)
                self._log("ERROR", f"overlay restart failure: {exc.message}")
                return False

            with self._lock:
                self._ready = True
                self._last_error = None
                self._restart_at = 0.0
                if is_restart:
                    self._restart_count += 1
                self._ever_ready = True
            self._log("INFO", f"overlay host ready pid={host_pid}")
            if is_restart:
                self._log("INFO", f"overlay restart success pid={host_pid}")
            return True

    def _command_worker(self):
        while True:
            command = self._command_queue.get()
            if command is None:
                self._command_queue.task_done()
                return
            try:
                if command.name == "ping":
                    command.result = self._do_ping()
                elif command.name == "show":
                    command.result = self._do_show(*command.args)
                elif command.name == "hide":
                    command.result = self._do_hide()
                else:
                    raise ValueError(f"unknown overlay command: {command.name}")
            except Exception as exc:
                command.error = exc
                with self._lock:
                    self._last_error = str(exc)
                self._log("WARN", f"overlay {command.name} completion failed: {exc}")
            finally:
                command.event.set()
                self._command_queue.task_done()

    def _submit(self, name, args=(), wait=True):
        with self._lock:
            if self._shutting_down:
                return _target_result("overlay controller is shut down")
        command = _Command(name, args)
        try:
            self._command_queue.put_nowait(command)
        except queue.Full:
            self._log("WARN", f"overlay {name} request rejected: command queue full")
            return _target_result("overlay command queue is full", supported=True)
        if not wait:
            return {"ok": True, "supported": True, "queued": True}
        command.event.wait(self.startup_timeout + self.request_timeout + 1.0)
        if not command.event.is_set():
            return _target_result("overlay command worker timed out", supported=True)
        if command.error is not None:
            return _target_result(command.error, supported=True)
        return command.result

    def _do_ping(self):
        if not self.ensure_started():
            return False
        self._log("DEBUG", "overlay ping request")
        response = self._request("ping")
        return response["type"] == "pong"

    def ping(self):
        return self._submit("ping")

    @staticmethod
    def _validate_show(mode, target, x, y, payload):
        message = protocol.make_message("show_overlay", {
            "mode": mode,
            "target": target,
            "x": x,
            "y": y,
            "data": payload,
        })
        return message["payload"]

    def _do_show(self, mode, target, x, y, payload):
        validated = self._validate_show(mode, target, x, y, payload)
        if target["kind"] == "remote":
            return _target_result("remote overlay targets are unsupported")
        if not self.ensure_started():
            return _target_result(self.snapshot()["last_error"] or "overlay host unavailable",
                                  supported=True)
        self._log("DEBUG", f"overlay show request mode={mode}")
        response = self._request("show_overlay", validated)
        with self._lock:
            self._visible = response["type"] == "overlay_visible"
            self._mode = mode
        self._log("DEBUG", f"overlay show completion mode={mode}")
        return response

    def show(self, mode, target, x, y, payload):
        try:
            validated = self._validate_show(mode, target, x, y, payload)
        except protocol.OverlayProtocolError as exc:
            return _target_result(exc.message)
        if validated["target"]["kind"] == "remote":
            return _target_result("remote overlay targets are unsupported")
        return self._submit("show", (mode, target, x, y, payload))

    def _do_hide(self):
        if not self.ensure_started():
            return _target_result(self.snapshot()["last_error"] or "overlay host unavailable",
                                  supported=True)
        self._log("DEBUG", "overlay hide request")
        response = self._request("hide_overlay")
        with self._lock:
            self._visible = False
            self._mode = None
        self._log("DEBUG", "overlay hide completion")
        return response

    def hide(self):
        return self._submit("hide")

    @staticmethod
    def _cursor_xy(cursor_position):
        if isinstance(cursor_position, dict):
            return cursor_position.get("x"), cursor_position.get("y")
        if isinstance(cursor_position, (tuple, list)) and len(cursor_position) == 2:
            return cursor_position[0], cursor_position[1]
        raise ValueError("cursor_position must contain integer x and y coordinates")

    def request_overlay(self, mode, target, cursor_position, payload):
        try:
            x, y = self._cursor_xy(cursor_position)
            validated = self._validate_show(mode, target, x, y, payload)
        except (ValueError, protocol.OverlayProtocolError) as exc:
            return _target_result(exc)
        if validated["target"]["kind"] == "remote":
            return _target_result("remote overlay targets are unsupported")
        return self._submit("show", (mode, target, x, y, payload), wait=False)

    def restart_after_crash(self):
        return self.ensure_started()

    def shutdown(self, grace_period=2.0):
        grace_period = _timeout(grace_period, "grace_period")
        with self._lock:
            if self._shutdown_complete:
                return True
            self._shutting_down = True
            process = self._process
            connection = self._connection
        self._log("INFO", "overlay shutdown")
        if connection is not None and process is not None and process.poll() is None:
            try:
                self._request("shutdown", timeout=min(self.request_timeout, grace_period))
            except protocol.OverlayProtocolError as exc:
                self._log("WARN", f"overlay shutdown request failed: {exc.message}")
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=grace_period)
            except subprocess.TimeoutExpired:
                self._terminate_process(process, grace=min(grace_period, 1.0))
        self._disconnect(connection, "overlay controller shut down", schedule=False)
        try:
            self._command_queue.put_nowait(None)
        except queue.Full:
            try:
                dropped = self._command_queue.get_nowait()
                if dropped is not None:
                    dropped.error = RuntimeError("overlay controller shut down")
                    dropped.event.set()
                self._command_queue.task_done()
            except queue.Empty:
                pass
            self._command_queue.put_nowait(None)
        self._worker_thread.join(timeout=grace_period)
        threads = []
        with self._lock:
            if self._reader_thread is not None:
                threads.append(self._reader_thread)
            threads.extend(self._watcher_threads)
            threads.extend(self._stderr_threads)
        deadline = time.monotonic() + grace_period
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._connection = None
            self._ready = False
            self._visible = False
            self._mode = None
            self._last_error = None
            self._shutdown_complete = True
        return process is None or process.poll() is not None
