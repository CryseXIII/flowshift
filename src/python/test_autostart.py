"""Auto-start with Windows: one Windows state for tray tick and WebGUI switch.

Viktor 26.11.: "ich weiss nicht ob das wirklich funktioniert wenn ichs auch
ausschalte. das sollte dasselbe setting sein wie in der webgui". The tests run
``autostart.AutoStart`` against fakes of the scheduled task and the Run key
(the only Windows access) and check the end state after every switch, plus
that tray menu, tray toggle and ``/api/auto-start`` all go through it.
"""
import ctypes
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autostart

_MISSING = object()


def _import_tray():
    """tray.py loads Windows ctypes APIs at import; off Windows with a stand-in."""
    if "tray" in sys.modules or sys.platform == "win32":
        import tray
        return tray
    saved = {name: getattr(ctypes, name, _MISSING) for name in ("windll", "WINFUNCTYPE")}
    ctypes.windll = mock.MagicMock(name="windll")
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    try:
        import tray
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                delattr(ctypes, name)
            else:
                setattr(ctypes, name, value)
    return tray


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\FlowShift</URI></RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>{trigger}</Enabled>
      <UserId>PC\\viktor</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>PC\\viktor</UserId><LogonType>InteractiveToken</LogonType>
  <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>{settings}<AllowStartOnDemand>true</AllowStartOnDemand></Settings>
  <Actions Context="Author"><Exec><Command>pythonw.exe</Command><Arguments>"tray.py" --tray</Arguments></Exec></Actions>
</Task>
"""


class FakeTask:
    """The scheduled task "FlowShift" as Windows keeps it."""

    def __init__(self, exists=True, trigger=True, task_enabled=True, refuse=None):
        self.exists = exists
        self.trigger = trigger
        self.task_enabled = task_enabled
        self.refuse = refuse
        self.calls = []

    def query(self):
        if not self.exists:
            return {"exists": False}
        return {"exists": True, "task_enabled": self.task_enabled, "triggers": 1,
                "triggers_enabled": 1 if self.trigger else 0}

    def set_logon_trigger(self, enabled):
        self.calls.append(enabled)
        if self.refuse:
            return False, self.refuse
        self.trigger = enabled
        if enabled:
            self.task_enabled = True
        return True, ""


class FakeRunKey:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value is not None

    def set(self, command):
        self.value = command

    def delete(self):
        self.value = None


def manager(task=None, run=None):
    return autostart.AutoStart(task or FakeTask(), run or FakeRunKey(), lambda: '"pythonw.exe" "tray.py" --tray')


class InstalledTaskTest(unittest.TestCase):
    def test_installed_default_reads_on(self):
        # The installer registers the task with an enabled logon trigger and no
        # Run value; the old code read only the Run value and showed "off".
        self.assertTrue(manager().state()["enabled"])
        self.assertEqual(manager().state()["mechanism"], "scheduled_task")

    def test_off_disables_trigger_and_keeps_task(self):
        task, run = FakeTask(), FakeRunKey()
        result = manager(task, run).set_enabled(False)
        self.assertFalse(result["enabled"])
        self.assertTrue(task.exists, "the task stays for the desktop shortcut and the updater")
        self.assertFalse(task.trigger)
        self.assertEqual(task.calls, [False])

    def test_on_again_enables_trigger(self):
        task = FakeTask(trigger=False)
        result = manager(task).set_enabled(True)
        self.assertTrue(result["enabled"])
        self.assertTrue(task.trigger)

    def test_on_also_enables_a_disabled_task(self):
        task = FakeTask(trigger=True, task_enabled=False)
        m = manager(task)
        self.assertFalse(m.state()["enabled"])
        self.assertTrue(m.set_enabled(True)["enabled"])
        self.assertTrue(task.task_enabled)

    def test_switching_to_current_state_changes_nothing(self):
        task = FakeTask(trigger=True)
        manager(task).set_enabled(True)
        self.assertEqual(task.calls, [])

    def test_old_run_value_is_removed_and_off_really_is_off(self):
        # Older tray versions wrote the Run value; with it FlowShift would
        # still start at logon although the trigger is off.
        task, run = FakeTask(trigger=True), FakeRunKey("old")
        m = manager(task, run)
        result = m.set_enabled(False)
        self.assertFalse(result["enabled"])
        self.assertIsNone(run.value)
        self.assertFalse(task.trigger)

    def test_old_run_value_counts_as_on_until_switched(self):
        m = manager(FakeTask(trigger=False), FakeRunKey("old"))
        self.assertTrue(m.state()["enabled"])

    def test_on_with_task_writes_no_run_value(self):
        run = FakeRunKey()
        manager(FakeTask(trigger=False), run).set_enabled(True)
        self.assertIsNone(run.value, "one start path only")

    def test_refused_change_raises_with_hint_and_state_stays_real(self):
        task = FakeTask(trigger=True, refuse="Access is denied. (0x80070005)")
        m = manager(task)
        with self.assertRaises(autostart.AutoStartError) as ctx:
            m.set_enabled(False)
        self.assertIn("could not be switched off", str(ctx.exception))
        self.assertIn("desktop shortcut", str(ctx.exception))
        self.assertTrue(m.state()["enabled"])

    def test_other_refusal_reports_windows_text(self):
        m = manager(FakeTask(trigger=False, refuse="The task XML is malformed"))
        with self.assertRaisesRegex(autostart.AutoStartError, "Windows reported: The task XML is malformed"):
            m.set_enabled(True)

    def test_round_trip_on_off_on(self):
        task, run = FakeTask(), FakeRunKey()
        m = manager(task, run)
        self.assertFalse(m.set_enabled(False)["enabled"])
        self.assertFalse(m.state()["enabled"])
        self.assertTrue(m.set_enabled(True)["enabled"])
        self.assertTrue(m.state()["enabled"])
        self.assertEqual(task.calls, [False, True])


class SourceCheckoutTest(unittest.TestCase):
    def test_without_task_uses_run_value(self):
        task, run = FakeTask(exists=False), FakeRunKey()
        m = manager(task, run)
        self.assertFalse(m.state()["enabled"])
        self.assertEqual(m.set_enabled(True)["mechanism"], "run_key")
        self.assertEqual(run.value, '"pythonw.exe" "tray.py" --tray')
        self.assertFalse(m.set_enabled(False)["enabled"])
        self.assertIsNone(run.value)
        self.assertEqual(task.calls, [])

    def test_failing_query_falls_back_to_run_value(self):
        task = FakeTask()
        task.query = mock.Mock(side_effect=OSError("schtasks missing"))
        self.assertEqual(manager(task, FakeRunKey("x")).state(),
                         {"enabled": True, "mechanism": "run_key", "task": False,
                          "task_trigger": False, "run_key": True})


class TaskXmlTest(unittest.TestCase):
    def test_enabled_trigger(self):
        info = autostart.parse_task_xml(TASK_XML.format(trigger="true", settings=""))
        self.assertEqual(info, {"exists": True, "task_enabled": True, "triggers": 1, "triggers_enabled": 1})

    def test_disabled_trigger(self):
        info = autostart.parse_task_xml(TASK_XML.format(trigger="false", settings=""))
        self.assertEqual(info["triggers_enabled"], 0)

    def test_disabled_task(self):
        info = autostart.parse_task_xml(TASK_XML.format(trigger="true", settings="<Enabled>false</Enabled>"))
        self.assertFalse(info["task_enabled"])

    def test_query_decodes_utf16_and_reports_missing_task(self):
        xml = TASK_XML.format(trigger="false", settings="")
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[3] == "Missing":
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"ERROR: not found")
            return types.SimpleNamespace(returncode=0, stdout=xml.encode("utf-16"), stderr=b"")

        self.assertEqual(autostart.ScheduledTask("FlowShift", run).query()["triggers_enabled"], 0)
        self.assertEqual(calls[0], ["schtasks", "/Query", "/TN", "FlowShift", "/XML"])
        self.assertEqual(autostart.ScheduledTask("Missing", run).query(), {"exists": False})

    def test_set_trigger_script_changes_trigger_not_task(self):
        off = autostart.set_trigger_script("FlowShift", False)
        self.assertIn("Get-ScheduledTask -TaskName 'FlowShift'", off)
        self.assertIn("$want = $false", off)
        self.assertIn("$tr.Enabled = $want", off)
        self.assertIn("Set-ScheduledTask -InputObject $t", off)
        self.assertNotIn("Unregister", off)
        self.assertNotIn("Disable-ScheduledTask", off)
        self.assertIn("$want = $true", autostart.set_trigger_script("FlowShift", True))

    def test_set_logon_trigger_reports_powershell_error(self):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"Access is denied."))
        ok, detail = autostart.ScheduledTask("FlowShift", run).set_logon_trigger(False)
        self.assertFalse(ok)
        self.assertEqual(detail, "Access is denied.")


class SharedSwitchTest(unittest.TestCase):
    """Tray tick and WebGUI switch read and write the same Windows state."""

    def setUp(self):
        self.task, self.run = FakeTask(), FakeRunKey()
        patcher_manager = mock.patch.object(autostart, "MANAGER", manager(self.task, self.run))
        patcher_supported = mock.patch.object(autostart, "SUPPORTED", True)
        patcher_manager.start()
        patcher_supported.start()
        self.addCleanup(patcher_manager.stop)
        self.addCleanup(patcher_supported.stop)

    def test_webgui_switch_then_tray_menu_shows_it(self):
        tray = _import_tray()
        import web_api
        web_api._auto_start_set(False)
        self.assertFalse(self.task.trigger)
        items = dict(tray.tray_menu_items(autostart.is_enabled(), "http://127.0.0.1:5000"))
        self.assertEqual(items[tray.ID_STARTUP], "  Auto-start with Windows")
        web_api._auto_start_set(True)
        items = dict(tray.tray_menu_items(autostart.is_enabled(), "http://127.0.0.1:5000"))
        self.assertEqual(items[tray.ID_STARTUP], "v Auto-start with Windows")

    def test_tray_toggle_then_webgui_reads_it(self):
        tray = _import_tray()
        import web_api
        with mock.patch.object(tray, "notify_user") as notify:
            self.assertFalse(tray.toggle_autostart())
            self.assertFalse(web_api._auto_start_get())
            self.assertTrue(tray.toggle_autostart())
            self.assertTrue(web_api._auto_start_get())
        notify.assert_not_called()
        self.assertEqual(self.task.calls, [False, True])

    def test_tray_menu_click_switches_off_the_background(self):
        import time
        tray = _import_tray()
        with mock.patch.object(tray, "notify_user"):
            tray._handle_menu(tray.ID_STARTUP)
            deadline = time.monotonic() + 5
            while self.task.trigger and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertFalse(self.task.trigger)
        self.assertFalse(autostart.is_enabled())

    def test_tray_toggle_refused_tells_the_user(self):
        tray = _import_tray()
        self.task.refuse = "Access is denied."
        with mock.patch.object(tray, "notify_user") as notify:
            self.assertTrue(tray.toggle_autostart(), "state stays as Windows has it")
        notify.assert_called_once()
        self.assertIn("could not be switched off", notify.call_args[0][1])

    def _api(self, method, body=None):
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import web_api
        saved_refs = web_api._refs
        web_api._refs = {}
        web_api.init()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            payload = json.dumps(body) if body is not None else None
            connection.putrequest(method, "/api/auto-start", skip_host=True)
            headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
            if payload is not None:
                headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders(payload.encode("utf-8") if payload is not None else None)
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
            return response.status, data
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            web_api._refs = saved_refs

    def test_api_switch_reads_back_windows_state(self):
        self.assertEqual(self._api("GET"), (200, {"enabled": True}))
        self.assertEqual(self._api("POST", {"enabled": False}), (200, {"enabled": False}))
        self.assertFalse(self.task.trigger)
        self.assertEqual(self._api("GET"), (200, {"enabled": False}))

    def test_api_post_refused_returns_409_with_real_state(self):
        self.task.refuse = "Access is denied."
        status, data = self._api("POST", {"enabled": False})
        self.assertEqual(status, 409)
        self.assertTrue(data["enabled"])
        self.assertIn("could not be switched off", data["error"])


class TrayMenuTest(unittest.TestCase):
    def test_menu_entries(self):
        tray = _import_tray()
        texts = [text for _id, text in tray.tray_menu_items(True, "http://127.0.0.1:5000")]
        self.assertEqual(texts, ["Clipboard", "Web GUI (http://127.0.0.1:5000)", "Open FlowShift Directory",
                                 "Open Data Directory", "v Auto-start with Windows", None, "Exit"])
        self.assertNotIn("Settings", texts)
        self.assertFalse(any(t and "forwarding" in t for t in texts))

    def test_folders(self):
        tray = _import_tray()
        opened = []
        with mock.patch.object(tray, "_open_folder", opened.append), \
                mock.patch.object(tray, "CONFIG_FILE", os.path.join("C:/ProgramData/FlowShift", "config.json")):
            tray._handle_menu(tray.ID_DATA_DIR)
            tray._handle_menu(tray.ID_WEB_DIR)
        self.assertEqual(opened[0], os.path.abspath("C:/ProgramData/FlowShift"))
        self.assertEqual(opened[1], os.path.dirname(os.path.dirname(os.path.abspath(tray.__file__))))


if __name__ == "__main__":
    unittest.main()
