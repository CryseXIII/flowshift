"""Auto-start with Windows: one switch, read from and written to Windows itself.

Viktor 26.11.: the tray tick "Auto-start with Windows" and the WebGUI switch
(Settings > Management) must be the same setting, act at the moment they are
switched, and really turn autostart on and off.

Before, both read and wrote an HKCU ``Run`` value "FlowShift" that the
installer never creates: the installer starts FlowShift at logon through the
Scheduled Task "FlowShift" (``install_flowshift.ps1``, AtLogOn, highest
privileges). So the tick showed "off" while FlowShift started anyway, "off"
changed nothing, and "on" added a second start path.

Now there is one source of truth, the actual Windows state:

- Installed FlowShift (the task exists): autostart is on when the task is
  enabled and has an enabled logon trigger. Switching changes only the
  trigger (``Set-ScheduledTask``); the task stays, so the desktop shortcut and
  the updater keep starting FlowShift with ``schtasks /Run``. A leftover HKCU
  ``Run`` value of older versions is removed on every switch (it would start
  FlowShift although autostart is off) and counts as "on" until then.
- Without the task (started from a source checkout): the HKCU ``Run`` value.

``set_enabled`` reads the state back after the change and raises
``AutoStartError`` when Windows did not take it, so neither the tray nor the
WebGUI show a state that is not real. Nothing is cached: the tray menu and
``GET /api/auto-start`` read Windows each time.

The Windows access sits behind two small classes (``ScheduledTask``,
``RunKey``); the tests replace them with fakes.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET

TASK_NAME = "FlowShift"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FlowShift"
CREATE_NO_WINDOW = 0x08000000
QUERY_TIMEOUT = 8
CHANGE_TIMEOUT = 30

NOT_ELEVATED_HINT = (
    "Windows refused the change. FlowShift needs the administrator rights of its autostart "
    "task for this: start FlowShift with its desktop shortcut and switch again."
)


class AutoStartError(Exception):
    pass


def parse_task_xml(text):
    """Task XML (``schtasks /Query /XML``) -> enabled flags of task and triggers."""
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text or "", count=1)
    root = ET.fromstring(text)

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def flag(element, default=True):
        if element is None:
            return default
        for child in element:
            if local(child.tag) == "Enabled":
                return (child.text or "").strip().lower() != "false"
        return default

    settings = next((c for c in root if local(c.tag) == "Settings"), None)
    triggers_el = next((c for c in root if local(c.tag) == "Triggers"), None)
    triggers = list(triggers_el) if triggers_el is not None else []
    return {
        "exists": True,
        "task_enabled": flag(settings),
        "triggers": len(triggers),
        "triggers_enabled": sum(1 for t in triggers if flag(t)),
    }


def _decode(raw):
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "mbcs", "cp850"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1")


def _powershell_exe():
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.exists(path) else "powershell"


def set_trigger_script(task_name, enabled):
    """PowerShell that switches the logon trigger(s) and leaves the task in place."""
    want = "$true" if enabled else "$false"
    name = task_name.replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$t = Get-ScheduledTask -TaskName '{name}'\n"
        f"$want = {want}\n"
        "if (@($t.Triggers).Count -eq 0) {\n"
        "    if (-not $want) { exit 0 }\n"
        "    $t.Triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $t.Principal.UserId)\n"
        "} else {\n"
        "    foreach ($tr in $t.Triggers) { $tr.Enabled = $want }\n"
        "}\n"
        "if ($want) { $t.Settings.Enabled = $true }\n"
        "Set-ScheduledTask -InputObject $t | Out-Null\n"
    )


class ScheduledTask:
    """The installer's autostart task, through schtasks (read) and PowerShell (write)."""

    def __init__(self, name=TASK_NAME, run=subprocess.run):
        self.name = name
        self._run = run

    def query(self):
        proc = self._run(["schtasks", "/Query", "/TN", self.name, "/XML"],
                         capture_output=True, timeout=QUERY_TIMEOUT,
                         creationflags=CREATE_NO_WINDOW)
        if proc.returncode != 0:
            return {"exists": False}
        return parse_task_xml(_decode(proc.stdout))

    def set_logon_trigger(self, enabled):
        proc = self._run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-Command", set_trigger_script(self.name, enabled)],
                         capture_output=True, timeout=CHANGE_TIMEOUT,
                         creationflags=CREATE_NO_WINDOW)
        if proc.returncode == 0:
            return True, ""
        detail = _decode(proc.stderr or proc.stdout).strip()
        return False, detail[:400]


class RunKey:
    """HKCU ...\\Run value "FlowShift" (start from a source checkout, old versions)."""

    def __init__(self, key=RUN_KEY, value=RUN_VALUE):
        self.key = key
        self.value = value

    def get(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_READ) as handle:
                winreg.QueryValueEx(handle, self.value)
                return True
        except FileNotFoundError:
            return False

    def set(self, command):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, self.value, 0, winreg.REG_SZ, command)

    def delete(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, self.value)
        except FileNotFoundError:
            pass


def default_run_command():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            exe = candidate
    tray = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.py")
    return f'"{exe}" "{tray}" --tray'


class AutoStart:
    def __init__(self, task, run_key, run_command=default_run_command):
        self.task = task
        self.run_key = run_key
        self.run_command = run_command
        self._lock = threading.Lock()

    def _task_state(self):
        try:
            return self.task.query()
        except Exception as exc:
            return {"exists": False, "error": str(exc)}

    def _run_key_state(self):
        try:
            return bool(self.run_key.get())
        except Exception:
            return False

    def state(self):
        task = self._task_state()
        run = self._run_key_state()
        if task.get("exists"):
            task_on = bool(task.get("task_enabled")) and task.get("triggers_enabled", 0) > 0
            return {"enabled": task_on or run, "mechanism": "scheduled_task",
                    "task": True, "task_trigger": task_on, "run_key": run}
        return {"enabled": run, "mechanism": "run_key", "task": False,
                "task_trigger": False, "run_key": run}

    def set_enabled(self, enabled):
        """Switch autostart; returns the state read back from Windows."""
        enabled = bool(enabled)
        with self._lock:
            before = self.state()
            problem = ""
            if before["task"]:
                if before["task_trigger"] != enabled:
                    try:
                        ok, problem = self.task.set_logon_trigger(enabled)
                    except Exception as exc:
                        ok, problem = False, str(exc)
                    if not ok and not problem:
                        problem = "the task did not change"
                if before["run_key"]:
                    # The task is the one start path; an old Run value would
                    # start FlowShift a second time, or although "off".
                    self._delete_run_key()
            else:
                try:
                    if enabled:
                        self.run_key.set(self.run_command())
                    else:
                        self.run_key.delete()
                except Exception as exc:
                    problem = str(exc)
            after = self.state()
        if after["enabled"] != enabled:
            word = "on" if enabled else "off"
            hint = NOT_ELEVATED_HINT if re.search(r"access|denied|zugriff|0x80070005", problem, re.I) else ""
            message = f"Auto-start with Windows could not be switched {word}."
            if hint:
                message += " " + hint
            elif problem:
                message += f" Windows reported: {problem}"
            raise AutoStartError(message)
        return after

    def _delete_run_key(self):
        try:
            self.run_key.delete()
        except Exception:
            pass


MANAGER = AutoStart(ScheduledTask(), RunKey())
# Only Windows has a task and a Run key; tests switch this on with fakes in MANAGER.
SUPPORTED = sys.platform == "win32"


def state():
    if not SUPPORTED:
        return {"enabled": False, "mechanism": "unavailable", "task": False,
                "task_trigger": False, "run_key": False}
    return MANAGER.state()


def is_enabled():
    return bool(state()["enabled"])


def set_enabled(enabled):
    if not SUPPORTED:
        raise AutoStartError("Auto-start with Windows exists only on Windows.")
    return MANAGER.set_enabled(enabled)
