"""Elevated runtime via a Windows Scheduled Task (one-time UAC, then no prompts).

Problem: launching the runtime with ``ShellExecute(..., "runas", ...)`` on every
start triggers a UAC prompt each time. Instead we register a Scheduled Task that
runs the runtime with **highest privileges**. Creating the task needs admin once
(a single UAC prompt at install time); afterwards ``schtasks /Run`` starts the
elevated runtime **without** any UAC prompt.

This module only builds the commands and runs them without a visible console
window. The command builders are pure and unit-tested.
"""
from __future__ import annotations

import os
import subprocess
import sys

from version import CREATE_NO_WINDOW

TASK_NAME = "FlowShiftElevatedRuntime"


def pythonw_exe():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def _tr(python_exe, script):
    # schtasks /TR value: the command line the task runs.
    return f'"{python_exe}" "{script}" --tray'


def create_task_cmd(python_exe, script):
    """schtasks command to (re)create the elevated task. Needs admin to run."""
    return [
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/SC", "ONCE", "/ST", "00:00", "/RL", "HIGHEST", "/F",
        "/TR", _tr(python_exe, script),
    ]


def run_task_cmd():
    """schtasks command to start the elevated runtime (no UAC prompt)."""
    return ["schtasks", "/Run", "/TN", TASK_NAME]


def delete_task_cmd():
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def query_task_cmd():
    return ["schtasks", "/Query", "/TN", TASK_NAME]


# ── Runners (Windows) ───────────────────────────────────────────────
def _run(cmd, timeout=10):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def is_installed():
    try:
        return _run(query_task_cmd(), timeout=6).returncode == 0
    except Exception:
        return False


def run_task():
    """Start the elevated runtime via the task. Returns (ok, message)."""
    try:
        r = _run(run_task_cmd())
        if r.returncode == 0:
            return True, "elevated task started"
        return False, (r.stderr or r.stdout or "schtasks /Run failed").strip()
    except Exception as e:
        return False, str(e)


def remove_task():
    """Delete the task (needs admin). Returns (ok, message)."""
    try:
        r = _run(delete_task_cmd())
        if r.returncode == 0:
            return True, "elevated task removed"
        return False, (r.stderr or r.stdout or "schtasks /Delete failed").strip()
    except Exception as e:
        return False, str(e)


def install_task_elevated(script):
    """Create the elevated task, elevating **once** via ShellExecute runas.

    Returns (ok, message). ``ok`` here means "the create request was dispatched";
    the caller should poll ``is_installed()`` afterwards.
    """
    py = pythonw_exe()
    args = create_task_cmd(py, script)
    # ShellExecute needs the parameters as one string (skip argv[0]=schtasks).
    params = subprocess.list2cmdline(args[1:])
    return _shellexecute_runas(params)


def remove_task_elevated():
    """Delete the task, elevating once via ShellExecute runas (needs admin)."""
    params = subprocess.list2cmdline(delete_task_cmd()[1:])
    return _shellexecute_runas(params)


def _shellexecute_runas(params):
    import ctypes
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "schtasks", params, None, 0)
        if int(rc) > 32:
            return True, "elevated request dispatched (bitte Status prüfen)"
        return False, f"ShellExecute returned {rc}"
    except Exception as e:
        return False, str(e)
