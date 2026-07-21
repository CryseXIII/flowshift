"""Stress reusable OverlayController show/hide and restart lifecycles."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time
from multiprocessing.connection import Listener

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from overlay_controller import DEFAULT_OVERLAY_URL, OverlayController


STARTUP_TIMEOUT = 8.0
REQUEST_TIMEOUT = 3.0
PROCESS_TIMEOUT = 4.0
HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_host.py")
LOCAL_TARGET = {"kind": "local", "identity": "local"}
COORDINATES = ((100, 120), (-100, -100), (1910, 1070), (-1910, 40), (0, -240))


class StressFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise StressFailure(message)


def bounded_int(minimum, name):
    def parse(value):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if parsed < minimum or parsed > 100000:
            raise argparse.ArgumentTypeError(
                f"{name} must be from {minimum} through 100000"
            )
        return parsed
    return parse


def positive_float(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 < parsed <= 5.0:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 5 seconds")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=bounded_int(2, "cycles"), default=200)
    parser.add_argument(
        "--shutdown-cycles", type=bounded_int(10, "shutdown-cycles"), default=10,
    )
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--url", default=DEFAULT_OVERLAY_URL)
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--restart-backoff", type=positive_float, default=0.1)
    return parser.parse_args(argv)


def make_controller(args):
    return OverlayController(
        host_script=HOST,
        overlay_url=args.url,
        python_executable=args.python_executable,
        headless=not args.visible,
        startup_timeout=STARTUP_TIMEOUT,
        request_timeout=REQUEST_TIMEOUT,
        command_queue_size=8,
        restart_delays=(args.restart_backoff,),
    )


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    return predicate()


def verify_show(controller, index, expected_pid):
    mode = "clipboard" if index % 2 == 0 else "command_wheel"
    x, y = COORDINATES[index % len(COORDINATES)]
    response = controller.show(mode, LOCAL_TARGET, x, y, {"stress_cycle": index})
    require(isinstance(response, dict), f"show {index} returned a non-object")
    require(response.get("type") == "overlay_visible", f"show {index} did not become visible")
    require(response.get("request_id"), f"show {index} was not correlated")
    require(response.get("payload", {}).get("mode") == mode, f"show {index} mode mismatch")
    status = controller.snapshot()
    require(status["process_alive"] and status["ipc_connected"] and status["ready"],
            f"show {index} left controller unhealthy")
    require(status["visible"] and status["mode"] == mode, f"show {index} status mismatch")
    if expected_pid is not None:
        require(controller.process_pid == expected_pid, f"show {index} started a new process")
    return response


def verify_hide(controller, index, expected_pid):
    response = controller.hide()
    require(isinstance(response, dict), f"hide {index} returned a non-object")
    require(response.get("type") == "overlay_hidden", f"hide {index} did not hide")
    require(response.get("request_id"), f"hide {index} was not correlated")
    status = controller.snapshot()
    require(status["process_alive"] and status["ipc_connected"] and status["ready"],
            f"hide {index} left controller unhealthy")
    require(not status["visible"] and status["mode"] is None, f"hide {index} status mismatch")
    require(controller.process_pid == expected_pid, f"hide {index} changed the host PID")
    return response


def verify_pipe_reusable(address):
    require(address, "controller did not retain its pipe address")
    listener = None
    try:
        listener = Listener(address, family="AF_PIPE", authkey=os.urandom(32))
    except OSError as exc:
        raise StressFailure(f"pipe endpoint was not reusable after shutdown: {exc}") from exc
    finally:
        if listener is not None:
            listener.close()


def kernel32_api():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def process_is_running(process_id):
    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = kernel32_api()
    handle = kernel32.OpenProcess(synchronize, False, int(process_id))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def terminate_owned_host(controller):
    process = controller._process
    require(process is not None and process.poll() is None, "no owned host to terminate")
    host_pid = controller.process_pid
    require(host_pid is not None, "controller did not report its authenticated host PID")
    process_terminate = 0x0001
    synchronize = 0x00100000
    kernel32 = kernel32_api()
    handle = kernel32.OpenProcess(
        process_terminate | synchronize, False, int(host_pid),
    )
    require(handle, f"could not open owned host PID {host_pid}")
    try:
        require(kernel32.TerminateProcess(handle, 73),
                f"could not terminate owned host PID {host_pid}")
        kernel32.WaitForSingleObject(handle, int(PROCESS_TIMEOUT * 1000))
    finally:
        kernel32.CloseHandle(handle)
    try:
        process.wait(timeout=PROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=PROCESS_TIMEOUT)
    require(not process_is_running(host_pid), f"owned host PID {host_pid} survived termination")
    return process


def run(args):
    show_count = 0
    hide_count = 0
    restart_count = 0
    independent_count = 0
    suite_started = time.monotonic()
    controller = make_controller(args)
    first_process = None
    restarted_process = None
    primary_pipe = None
    try:
        require(controller.ensure_started(), "primary controller failed to start")
        first_pid = controller.process_pid
        first_process = controller._process
        require(first_pid and first_process is not None, "primary controller has no child PID")

        cycle_started = time.monotonic()
        for index in range(args.cycles):
            verify_show(controller, index, first_pid)
            show_count += 1
            verify_hide(controller, index, first_pid)
            hide_count += 1
        cycle_elapsed = time.monotonic() - cycle_started

        terminated = terminate_owned_host(controller)
        require(terminated.poll() is not None, "deliberately terminated child was not reaped")
        require(
            wait_for(lambda: not controller.is_alive() and not controller.snapshot()["ready"],
                     PROCESS_TIMEOUT),
            "controller did not observe deliberate host termination",
        )
        require(controller._worker_thread.is_alive(), "controller worker died with its host")
        time.sleep(args.restart_backoff + 0.05)
        restarted = verify_show(controller, args.cycles, None)
        show_count += 1
        second_pid = controller.process_pid
        restarted_process = controller._process
        require(second_pid and second_pid != first_pid, "restart did not create a new PID")
        require(restarted.get("type") == "overlay_visible", "post-restart show failed")
        require(controller.snapshot()["restart_count"] == 1, "restart_count was not exactly one")
        restart_count = 1
        verify_hide(controller, args.cycles, second_pid)
        hide_count += 1

        primary_pipe = controller.pipe_address
        require(controller.shutdown(), "primary controller shutdown failed")
        require(controller.process_pid is None and not controller.is_alive(),
                "primary controller still exposes a process after shutdown")
        require(restarted_process.poll() is not None, "restarted child was not reaped")
        verify_pipe_reusable(primary_pipe)
    finally:
        controller.shutdown()

    lifecycle_started = time.monotonic()
    live_children = {}
    for index in range(args.shutdown_cycles):
        current = make_controller(args)
        process = None
        try:
            require(current.ensure_started(), f"independent controller {index} failed to start")
            pid = current.process_pid
            process = current._process
            require(pid and process is not None, f"independent controller {index} has no child")
            live_children[pid] = process
            verify_show(current, index, pid)
            show_count += 1
            verify_hide(current, index, pid)
            hide_count += 1
            require(current.shutdown(), f"independent controller {index} shutdown failed")
            require(process.poll() is not None, f"independent child {pid} was not reaped")
            require(not process_is_running(pid), f"independent host PID {pid} survived shutdown")
            live_children.pop(pid, None)
            require(not live_children, f"live child PIDs accumulated after cycle {index}")
            independent_count += 1
        finally:
            current.shutdown()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=PROCESS_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=PROCESS_TIMEOUT)
    lifecycle_elapsed = time.monotonic() - lifecycle_started
    total_elapsed = time.monotonic() - suite_started

    print(
        f"Controller counts: primary cycles={args.cycles}, shows={show_count}, "
        f"hides={hide_count}, restarts={restart_count}, "
        f"independent shutdown cycles={independent_count}"
    )
    print(
        f"Timings: primary show/hide={cycle_elapsed:.3f}s, "
        f"independent lifecycles={lifecycle_elapsed:.3f}s, total={total_elapsed:.3f}s"
    )
    print(
        f"Mode: {'visible' if args.visible else 'headless'}; "
        f"pipe reusable=yes; live child PIDs=0"
    )
    print("PASS: overlay show/hide stress completed with all owned hosts reaped.")


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "win32":
        print("SKIP: overlay_show_hide_stress_test requires Windows named pipes.")
        return 0
    try:
        run(args)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
