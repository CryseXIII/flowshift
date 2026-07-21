"""Lifecycle checks for the isolated overlay host and controller.

Run: ``PYTHONDONTWRITEBYTECODE=1 python src/python/test_overlay_lifecycle.py``
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from overlay_controller import DEFAULT_OVERLAY_URL, OverlayController


if sys.platform != "win32":
    print("[SKIP] test_overlay_lifecycle requires Windows named pipes.")
    raise SystemExit(0)


_checks = 0
_failures = []


def check(condition, label):
    global _checks
    _checks += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    return predicate()


HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_host.py")
logs = []
try:
    OverlayController(overlay_url="https://example.com/overlay.html")
    check(False, "remote overlay URL is rejected")
except ValueError:
    check(True, "remote overlay URL is rejected")
check(DEFAULT_OVERLAY_URL.endswith("/overlay.html"),
      "overlay URL uses the dedicated HTML entry")
controller = OverlayController(
    host_script=HOST,
    overlay_url="http://127.0.0.1:5000/overlay-test",
    python_executable=sys.executable,
    headless=True,
    startup_timeout=4.0,
    request_timeout=1.0,
    command_queue_size=8,
    restart_delays=(0.1, 0.2, 0.3),
    log=lambda level, message: logs.append((level, message)),
)

initial = controller.snapshot()
check(list(initial) == ["enabled", "process_alive", "ipc_connected", "ready", "mode",
                        "visible", "restart_count", "last_error"], "snapshot keys are exact")
check(initial["enabled"] is True, "controller starts enabled")
check(initial["process_alive"] is False, "initial process is stopped")
check(initial["ipc_connected"] is False, "initial IPC is disconnected")
check(initial["ready"] is False, "initial host is not ready")
check(initial["mode"] is None, "initial mode is empty")
check(initial["visible"] is False, "initial overlay is hidden")
check(initial["restart_count"] == 0, "initial restart count is zero")
check(initial["last_error"] is None, "initial error is empty")

check(controller.ensure_started() is True, "hello/ready handshake succeeds")
started = controller.snapshot()
first_pid = controller.process_pid
check(isinstance(first_pid, int) and first_pid != os.getpid(), "host has an owned child PID")
check(started["process_alive"] is True, "host process is alive")
check(started["ipc_connected"] is True, "IPC is connected")
check(started["ready"] is True, "headless UI is ready")
check(controller.ping() is True, "ping receives correlated pong")
check(controller.process_pid == first_pid, "ping reuses host PID")

local = {"kind": "local", "identity": "local"}
clipboard = controller.show("clipboard", local, 100, 120, {"diagnostic": "clipboard"})
check(clipboard.get("type") == "overlay_visible", "clipboard show completes")
check(clipboard.get("payload", {}).get("mode") == "clipboard", "clipboard diagnostic mode returned")
check(clipboard.get("request_id") is not None, "show response is correlated")
check(controller.snapshot()["visible"] is True, "show marks overlay visible")
check(controller.snapshot()["mode"] == "clipboard", "show records clipboard mode")
check(controller.process_pid == first_pid, "first show reuses host PID")

hidden = controller.hide()
check(hidden.get("type") == "overlay_hidden", "hide completes")
check(controller.snapshot()["visible"] is False, "hide marks overlay hidden")
check(controller.snapshot()["mode"] is None, "hide clears current overlay mode")
check(controller.process_pid == first_pid, "hide reuses host PID")

wheel = controller.show("command_wheel", local, 1910, 1070, {"diagnostic": "wheel"})
check(wheel.get("type") == "overlay_visible", "command wheel show completes")
check(wheel.get("payload", {}).get("mode") == "command_wheel",
      "command wheel diagnostic mode returned")
check(wheel.get("payload", {}).get("width") == 480, "headless placement has baseline width")
check(wheel.get("payload", {}).get("height") == 300, "headless placement has baseline height")
check(controller.process_pid == first_pid, "show/hide/show uses one host")

queued = controller.request_overlay(
    "clipboard", local, {"x": 25, "y": 35}, {"diagnostic": "async"},
)
check(queued == {"ok": True, "supported": True, "queued": True},
      "async request is accepted without waiting")
check(wait_for(lambda: controller.snapshot()["mode"] == "clipboard"),
      "async command worker completes show")
check(controller.snapshot()["visible"] is True, "async show is visible")

remote = {"kind": "remote", "identity": "device:peer-test"}
remote_controller = OverlayController(
    host_script=HOST, headless=True, restart_delays=(0.1,),
)
remote_result = remote_controller.request_overlay("clipboard", remote, (1, 2), {})
check(remote_result["ok"] is False, "remote request is rejected")
check(remote_result["supported"] is False, "remote request truthfully reports unsupported")
check("remote" in remote_result["reason"], "remote result explains routing")
check(remote_controller.process_pid is None, "remote request does not start local host")
check(remote_controller.shutdown() is True, "unused controller shuts down")

subprocess.run(
    ["taskkill", "/PID", str(first_pid), "/F", "/T"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
check(wait_for(lambda: not controller.is_alive()), "deliberate host kill is detected")
crashed = controller.snapshot()
check(crashed["ready"] is False, "crash clears ready state")
check(crashed["ipc_connected"] is False, "crash clears IPC state")
check(crashed["last_error"] is not None, "crash records last error")
check(controller._worker_thread.is_alive(), "controller command worker remains alive")
check(controller.restart_after_crash() is False, "restart respects short backoff")
time.sleep(0.15)
check(controller.restart_after_crash() is True, "restart succeeds after eligible time")
second_pid = controller.process_pid
check(isinstance(second_pid, int) and second_pid != first_pid, "restart owns a new PID")
check(controller.snapshot()["restart_count"] == 1, "successful restart count is truthful")
check(controller.ping() is True, "restarted host responds to ping")
check(controller.show("clipboard", local, -100, -100, {}).get("type") == "overlay_visible",
      "next local show works after restart")
check(any("restart schedule" in message for _, message in logs), "restart schedule is logged")
check(any("restart success" in message for _, message in logs), "restart success is logged")

check(controller.shutdown() is True, "shutdown completes")
check(controller.shutdown() is True, "repeated shutdown is idempotent")
check(controller.process_pid is None, "shutdown exposes no live PID")
check(controller.is_alive() is False, "shutdown leaves no child alive")
check(controller.snapshot()["ipc_connected"] is False, "shutdown closes IPC")
check(controller.snapshot()["ready"] is False, "shutdown clears ready")
check(controller.snapshot()["mode"] is None, "shutdown clears current overlay mode")
check(controller._worker_thread.is_alive() is False, "shutdown joins command worker")
if second_pid is not None:
    check(controller._process.poll() is not None, "owned child process has exited")

print()
if _failures:
    print(f"FAILED: {len(_failures)} of {_checks} check(s): {', '.join(_failures)}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
