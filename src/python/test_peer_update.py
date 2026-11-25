"""Updates from a connected FlowShift (Viktor 25.11.): two instances, two versions.

The sender is an installed-layout copy of this repository with a higher
VERSION; it builds its package and serves it on a real TCP listener the way
the FlowShift port does (first framed message = package request). The
receiver is a PeerUpdateController with a lower version that dials that
listener. The tests cover acceptance, refusal (unpaired, older, development
version on Stable), cancel, a broken transfer that resumes, and manipulated
packages, and end at the handoff to the external updater (plan with
package_kind "peer").
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import peer_update as pu
import update_handoff
from runtime_model import FramedReader
from test_usb_package import make_installed_root

LOW = "0.7.0-dev.40"
HIGH = "0.7.0-dev.90"
SENDER_ID = "aa11bb22"
RECEIVER_ID = "cc33dd44"


def installed_root(target, version):
    root = make_installed_root(target)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "webgui" / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


class Sender:
    """The higher FlowShift: package cache plus a listener on a free port."""

    def __init__(self, root, outbox, peers, own_id=SENDER_ID, cache=None):
        self.cache = cache or pu.PackageCache(root, outbox)
        self.peers = peers
        self.own_id = own_id
        self.requests = []
        self.results = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                first = FramedReader(conn).read_message(5.0)
                self.requests.append(first)
                if first and first.get("type") == pu.T_CHANNEL_HELLO:
                    self.results.append(pu.serve_socket(conn, first, self.cache, self.peers, self.own_id))

    def close(self):
        self.stop.set()
        self.server.close()
        self.thread.join(2)


class PeerUpdateTwoInstances(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.sender_root = installed_root(base / "sender" / "FlowShift", HIGH)
        cls.package = pu.build_package(cls.sender_root, base / "built")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.base = Path(self.work.name)
        self.data_dir = self.base / "receiver-data"
        self.outbox = self.base / "outbox"
        self.outbox.mkdir()
        shutil.copy2(self.package["path"], self.outbox / Path(self.package["path"]).name)
        self.sender = Sender(self.sender_root, self.outbox,
                             peers=[{"name": "Receiver", "host": "127.0.0.1", "device_id": RECEIVER_ID}],
                             cache=self.fixed_cache(self.package))
        self.settings = {"channel": "development", "enabled": True,
                         "from_peers": {"policy": "notify", "allow_development": False}}
        self.handoffs = []
        self.notices = []
        self.lines = []
        self.safe = [True]
        self.shutdown = threading.Event()
        self.reserved = []

    def tearDown(self):
        self.sender.close()
        self.work.cleanup()

    def fixed_cache(self, package):
        class Fixed:
            def get(self_inner):
                return dict(package)

            def offer_message(self_inner, device_name=""):
                return {"type": pu.T_OFFER, "version": package["version"], "size": package["size"],
                        "sha256": package["sha256"], "device_name": device_name}
        return Fixed()

    def peer(self):
        return {"name": "Viktor-PC", "host": "127.0.0.1", "port": self.sender.port, "device_id": SENDER_ID}

    def controller(self, installed=LOW, **extra):
        def handoff(from_version, to_version, path, size, sha256):
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), sha256)
            self.handoffs.append((from_version, to_version, Path(path).name, size, sha256))

        options = dict(
            data_dir=self.data_dir, installed_version=installed,
            settings_getter=lambda: self.settings, own_device_id_getter=lambda: RECEIVER_ID,
            open_channel=lambda peer, hello: pu.open_socket_channel(pu.peer_endpoint(peer), hello),
            handoff=handoff, safety_query=lambda: {"safe": self.safe[0], "reason": "clipboard_transfer_active"},
            reserve=lambda: self.reserved.append(1) or {"safe": self.safe[0], "reason": "clipboard_transfer_active"},
            release=lambda: self.reserved.append(-1),
            shutdown_callback=self.shutdown.set,
            notify=lambda title, text: self.notices.append((title, text)),
            log=lambda level, text: self.lines.append(text),
            announce_seconds=0.0, safety_poll=0.05, retry_pause=0.01)
        options.update(extra)
        self.ctrl = pu.PeerUpdateController(**options)
        return self.ctrl

    def offer(self):
        return self.sender.cache.offer_message("Viktor-PC")

    def wait_state(self, ctrl, states, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ctrl.snapshot()["state"] in states:
                return ctrl.snapshot()
            time.sleep(0.02)
        self.fail(f"state stayed {ctrl.snapshot()['state']}, expected {states}")

    # ── acceptance ──
    def test_install_policy_fetches_checks_installs_and_restarts(self):
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        self.assertEqual(ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "accepted")
        self.assertTrue(self.shutdown.wait(30), self.lines)
        self.assertEqual(self.handoffs, [(LOW, HIGH, f"FlowShift-Peer-{HIGH}.zip",
                                          self.package["size"], self.package["sha256"])])
        self.assertEqual(ctrl.snapshot()["state"], pu.INSTALLING)
        last = json.loads((self.data_dir / "updates" / pu.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual((last["from_version"], last["to_version"], last["peer_name"]), (LOW, HIGH, "Viktor-PC"))
        self.assertEqual([r.get("offset") for r in self.sender.requests], [0])
        self.assertEqual(self.sender.results, ["sent"])
        self.assertTrue(any("fetched and checked" in line for line in self.lines))
        self.assertTrue(any("FlowShift restarts now" in line for line in self.lines))
        self.assertTrue(any("installs it and then restarts" in text for _, text in self.notices))

    def test_notify_policy_transfers_nothing_until_the_user_clicks(self):
        ctrl = self.controller()
        self.assertEqual(ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "accepted")
        time.sleep(0.3)
        snapshot = ctrl.snapshot()
        self.assertEqual(snapshot["state"], pu.AVAILABLE)
        self.assertEqual(snapshot["offer"]["version"], HIGH)
        self.assertTrue(snapshot["offer"]["development"])
        self.assertEqual(self.sender.requests, [])
        self.assertFalse((self.data_dir / "updates" / "downloads").exists())
        self.assertEqual(ctrl.download(), "started")
        self.wait_state(ctrl, {pu.DOWNLOADED})
        self.assertEqual(self.handoffs, [])
        self.assertFalse(self.shutdown.is_set())
        self.assertEqual(ctrl.install(), "started")
        self.assertTrue(self.shutdown.wait(10))
        self.assertEqual(len(self.handoffs), 1)

    def test_download_policy_waits_for_the_install_click(self):
        self.settings["from_peers"]["policy"] = "download"
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        self.wait_state(ctrl, {pu.DOWNLOADED})
        time.sleep(0.2)
        self.assertEqual(self.handoffs, [])
        self.assertTrue((self.data_dir / "updates" / "downloads" / f"FlowShift-Peer-{HIGH}.zip").is_file())

    def test_install_waits_for_idle_and_never_restarts_during_a_transfer(self):
        self.settings["from_peers"]["policy"] = "install"
        self.safe[0] = False
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        snapshot = self.wait_state(ctrl, {pu.WAITING})
        deadline = time.monotonic() + 10
        while ctrl.snapshot()["wait_reason"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(ctrl.snapshot()["wait_reason"], "clipboard_transfer_active")
        time.sleep(0.3)
        self.assertEqual(self.handoffs, [])
        self.assertFalse(self.shutdown.is_set())
        self.safe[0] = True
        self.assertTrue(self.shutdown.wait(10))
        self.assertEqual(len(self.handoffs), 1)
        self.assertIn(1, self.reserved)
        self.assertIsNotNone(snapshot)

    def test_announced_install_can_be_cancelled_and_package_stays_ready(self):
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller(announce_seconds=30.0)
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        snapshot = self.wait_state(ctrl, {pu.WAITING})
        self.assertIsNotNone(snapshot["install_at"])
        self.assertTrue(any("restarts in about 30 seconds" in text for _, text in self.notices))
        self.assertTrue(ctrl.cancel())
        self.wait_state(ctrl, {pu.DOWNLOADED})
        self.assertEqual(self.handoffs, [])
        self.assertFalse(self.shutdown.is_set())

    def test_stable_channel_takes_development_versions_only_with_the_tick(self):
        self.settings["channel"] = "stable"
        self.settings["from_peers"]["policy"] = "download"
        ctrl = self.controller()
        self.assertEqual(ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()),
                         "development_version_not_allowed")
        self.assertEqual(ctrl.snapshot()["state"], pu.IDLE)
        self.assertEqual(self.sender.requests, [])
        self.settings["from_peers"]["allow_development"] = True
        self.assertEqual(ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "accepted")
        self.wait_state(ctrl, {pu.DOWNLOADED})

    # ── refusal ──
    def test_unpaired_device_and_older_version_are_ignored(self):
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        self.assertEqual(ctrl.on_offer("device:ffff0000", None, self.offer()), "not_paired")
        self.assertEqual(ctrl.on_offer("name:Somebody", None, self.offer()), "not_paired")
        self.assertEqual(ctrl.snapshot()["unpaired_offer"]["version"], HIGH)
        self.assertIsNone(ctrl.snapshot()["offer"])
        self.assertEqual(ctrl.snapshot()["state"], pu.IDLE)
        newer = self.controller(installed="0.7.1")
        self.assertEqual(newer.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "not_newer")
        same = self.controller(installed=HIGH)
        self.assertEqual(same.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "not_newer")
        time.sleep(0.2)
        self.assertEqual(self.sender.requests, [])
        self.assertEqual(self.handoffs, [])

    def test_sender_refuses_a_device_that_is_not_in_its_list(self):
        self.sender.peers = [{"name": "Other", "host": "10.0.0.9", "device_id": "99999999"}]
        self.settings["from_peers"]["policy"] = "download"
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        snapshot = self.wait_state(ctrl, {pu.FAILED})
        self.assertEqual(snapshot["error"]["code"], "not_paired")
        self.assertEqual(self.sender.results, ["not_paired"])

    def test_address_answering_with_another_device_id_is_refused(self):
        self.sender.own_id = "0badc0de"
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        snapshot = self.wait_state(ctrl, {pu.FAILED})
        self.assertEqual(snapshot["error"]["code"], "not_paired")
        self.assertEqual(self.handoffs, [])
        self.assertFalse(list((self.data_dir / "updates" / "downloads").glob("*.zip")))

    def test_failed_install_of_this_version_pauses_after_the_allowed_attempts(self):
        # Viktor 25.11. 17:34: no update loop; after N failed attempts a pause
        # with a message. One failure (e.g. a normal update of HIGH) is tried
        # once more, MAX_AUTOMATIC_ATTEMPTS failures stop automatic installs.
        updates = self.data_dir / "updates"
        updates.mkdir(parents=True)
        (updates / "last_update_result.json").write_text(json.dumps(
            {"to_version": HIGH, "from_version": LOW, "result": "rollback_success"}), encoding="utf-8")
        (updates / pu.STATE_NAME).write_text(json.dumps(
            {"from_version": LOW, "to_version": HIGH, "peer_name": "Viktor-PC",
             "handed_off_at": "2026-11-25T17:00:00Z", "attempts": pu.MAX_AUTOMATIC_ATTEMPTS}), encoding="utf-8")
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
        time.sleep(0.3)
        snapshot = ctrl.snapshot()
        self.assertEqual(snapshot["state"], pu.AVAILABLE)
        self.assertTrue(snapshot["paused"])
        self.assertEqual(snapshot["failed_attempts"], pu.MAX_AUTOMATIC_ATTEMPTS)
        self.assertEqual(self.sender.requests, [])
        self.assertTrue(any("paused" in title for title, _ in self.notices))
        # A click on Install still works.
        self.assertEqual(ctrl.install(), "started")
        self.assertTrue(self.shutdown.wait(30), self.lines)
        self.assertEqual(len(self.handoffs), 1)

    # ── what PC 1 sees (Viktor 25.11. 17:34) ──
    def linked(self, monitor, notices):
        """Receiver controller whose reports reach the sender's monitor the
        way the peer link carries them (identity = the sender's link)."""
        reports = []

        def report(identity, msg):
            self.assertEqual(identity, f"device:{SENDER_ID}")
            reports.append(json.loads(json.dumps(msg)))
            monitor.on_status(f"device:{RECEIVER_ID}", "PC 2", msg)
        return reports, report

    def test_pc1_follows_every_step_until_pc2_is_back_with_the_same_version(self):
        self.settings["from_peers"]["policy"] = "install"
        notices, lines = [], []
        monitor = pu.PeerUpdateMonitor(HIGH, notify=lambda title, text: notices.append((title, text)),
                                       log=lambda level, text: lines.append(text))
        receiver = f"device:{RECEIVER_ID}"
        # 1) Handshake: the versions are compared before any screen edge.
        self.assertEqual(monitor.on_link(receiver, "PC 2", LOW), pu.PEER_OLDER)
        self.assertEqual(monitor.view(receiver)["text"], f"Older version {LOW}")
        self.assertIsNone(monitor.switch_block(receiver))
        # PC 2 is being controlled from PC 1 right now: it must not restart.
        self.safe[0] = False
        safety = {"safe": False, "reason": "forwarding_active"}
        reports, report = self.linked(monitor, notices)
        ctrl = self.controller(report=report, safety_query=lambda: dict(safety),
                               reserve=lambda: dict(safety))
        self.assertEqual(ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer()), "accepted")
        deadline = time.monotonic() + 30
        while monitor.view(receiver)["phase"] != pu.PEER_WAITING or \
                (monitor.view(receiver).get("wait_reason") != "forwarding_active"):
            self.assertLess(time.monotonic(), deadline, reports[-3:])
            time.sleep(0.02)
        # 2) Transfer with percent, then "waits until you are back".
        percents = [r["percent"] for r in reports if r["state"] == pu.DOWNLOADING and "percent" in r]
        self.assertEqual(percents[-1], 100)
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(monitor.view(receiver)["text"], "Update waits until you are back on this PC")
        self.assertTrue(any(title == "Updating PC 2" for title, _ in notices))
        time.sleep(0.3)
        self.assertEqual(self.handoffs, [])
        self.assertFalse(self.shutdown.is_set())
        # 3) Viktor is back on PC 1: install, last report "restarting".
        safety.update(safe=True, reason="safe")
        self.assertTrue(self.shutdown.wait(10), self.lines)
        self.assertEqual(len(self.handoffs), 1)
        self.assertEqual(reports[-1]["state"], "restarting")
        self.assertIn(pu.INSTALLING, [r["state"] for r in reports])
        self.assertEqual(monitor.view(receiver)["phase"], pu.PEER_RESTARTING)
        # A switch now gets a message instead of failing silently.
        self.assertIn("PC 2 is being updated", monitor.switch_block(receiver))
        monitor.on_unlink(receiver)
        self.assertIn("PC 2 is being updated", monitor.switch_block(receiver))
        # 4) PC 2 dials in again by itself with the new version.
        notices.clear()
        self.assertEqual(monitor.on_link(receiver, "PC 2", HIGH), pu.PEER_UPDATED)
        self.assertEqual(monitor.view(receiver)["text"], f"Connected again, version {HIGH}")
        self.assertIsNone(monitor.switch_block(receiver))
        self.assertEqual([title for title, _ in notices], ["PC 2 is updated"])
        # The restarted PC 2 confirms the same version once; no second toast.
        restarted_reports, restarted_report = self.linked(monitor, notices)
        restarted = self.controller(installed=HIGH, report=restarted_report)
        self.assertEqual(restarted.on_peer_linked(f"device:{SENDER_ID}", self.peer()), "updated")
        self.assertIsNone(restarted.on_peer_linked(f"device:{SENDER_ID}", self.peer()))
        self.assertEqual([r["state"] for r in restarted_reports], ["updated"])
        self.assertEqual(restarted_reports[0]["installed_version"], HIGH)
        self.assertEqual(len(notices), 1)
        self.assertEqual(monitor.view(receiver)["phase"], pu.PEER_UPDATED)

    def test_failed_install_is_reported_to_pc1_and_retried_once_then_paused(self):
        self.settings["from_peers"]["policy"] = "install"
        notices = []
        monitor = pu.PeerUpdateMonitor(HIGH, notify=lambda title, text: notices.append((title, text)))
        receiver = f"device:{RECEIVER_ID}"
        updates = self.data_dir / "updates"

        def run_once(attempt):
            self.shutdown.clear()
            reports, report = self.linked(monitor, notices)
            ctrl = self.controller(report=report)
            # After the restart: PC 2 reports how the last attempt ended.
            ctrl.on_peer_linked(f"device:{SENDER_ID}", self.peer())
            ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.offer())
            return ctrl, reports

        for attempt in range(1, pu.MAX_AUTOMATIC_ATTEMPTS + 1):
            monitor.on_link(receiver, "PC 2", LOW)
            ctrl, reports = run_once(attempt)
            self.assertTrue(self.shutdown.wait(30), self.lines)
            self.assertEqual(len(self.handoffs), attempt)
            monitor.on_unlink(receiver)
            # The updater rolls back: PC 2 runs LOW again and knows why.
            (updates / "last_update_result.json").write_text(json.dumps(
                {"to_version": HIGH, "from_version": LOW, "result": "rollback_success",
                 "error": "pip install failed: no internet"}), encoding="utf-8")
            self.assertEqual(json.loads((updates / pu.STATE_NAME).read_text(encoding="utf-8"))["attempts"], attempt)
        # Back after the second failure: reason on PC 1, no third attempt.
        notices.clear()
        self.assertEqual(monitor.on_link(receiver, "PC 2", LOW), pu.PEER_FAILED)
        ctrl, reports = run_once(pu.MAX_AUTOMATIC_ATTEMPTS + 1)
        time.sleep(0.3)
        self.assertEqual(len(self.handoffs), pu.MAX_AUTOMATIC_ATTEMPTS)
        self.assertFalse(self.shutdown.is_set())
        self.assertEqual(reports[0]["state"], pu.FAILED)
        self.assertEqual(reports[0]["error"]["message"], "pip install failed: no internet")
        self.assertTrue(reports[0]["paused"])
        view = monitor.view(receiver)
        self.assertEqual(view["phase"], pu.PEER_FAILED)
        self.assertTrue(view["paused"])
        self.assertEqual(view["text"], f"Update failed: pip install failed: no internet; runs on with {LOW} "
                                       f"(automatic attempts paused after {pu.MAX_AUTOMATIC_ATTEMPTS})")
        self.assertTrue(any(title == "Update of PC 2 failed" and "pip install failed" in text
                            for title, text in notices))
        self.assertTrue(ctrl.snapshot()["paused"])

    # ── cancel and resume ──
    def test_cancel_during_the_transfer_keeps_nothing(self):
        cancel = threading.Event()
        seen = []

        def progress(done, total):
            seen.append(done)
            cancel.set()

        offer = pu.validate_offer(self.offer())
        offer["peer"] = self.peer()
        with self.assertRaises(pu.PeerPackageError) as caught:
            pu.download_package(lambda hello: pu.open_socket_channel(("127.0.0.1", self.sender.port), hello),
                                offer, RECEIVER_ID, self.base / "dl", progress=progress, cancel=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertTrue(seen)
        self.assertEqual(list((self.base / "dl").iterdir()), [])

    def test_broken_transfer_resumes_where_it_stopped(self):
        offer = pu.validate_offer(self.offer())
        offer["peer"] = self.peer()
        hellos = []

        class Breaking:
            """In-memory channel that loses the connection after two chunks once."""

            def __init__(self, hello, package, broken):
                hellos.append(dict(hello))
                self.messages = []
                pu.serve_channel(hello, package, self.messages.append, True, SENDER_ID)
                if broken:
                    chunks = [i for i, m in enumerate(self.messages) if m["type"] == pu.T_CHUNK]
                    self.messages = self.messages[:chunks[2]]

            def read_message(self, timeout):
                if not self.messages:
                    raise ConnectionError("connection closed")
                return self.messages.pop(0)

            def close(self):
                pass

        old_chunk = pu.CHUNK_SIZE
        pu.CHUNK_SIZE = 64 * 1024
        try:
            path = pu.download_package(lambda hello: Breaking(hello, self.package, broken=not hellos),
                                       offer, RECEIVER_ID, self.base / "dl", pause=0, sleep=lambda _: None)
        finally:
            pu.CHUNK_SIZE = old_chunk
        self.assertEqual([h["offset"] for h in hellos], [0, 2 * 64 * 1024])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.package["sha256"])
        self.assertFalse(Path(str(path) + ".part").exists())

    def test_transfer_that_never_completes_ends_cleanly(self):
        offer = pu.validate_offer(self.offer())
        offer["peer"] = self.peer()
        calls = []

        def unreachable(hello):
            calls.append(hello)
            raise ConnectionRefusedError("refused")

        with self.assertRaises(pu.PeerPackageError) as caught:
            pu.download_package(unreachable, offer, RECEIVER_ID, self.base / "dl", pause=0, sleep=lambda _: None)
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertEqual(len(calls), pu.DOWNLOAD_ATTEMPTS)
        self.assertFalse(list((self.base / "dl").glob("*.zip")))

    # ── manipulated packages ──
    def rewritten(self, change):
        """Copy of the real package with ``change(entries)`` applied; new offer for it."""
        source = Path(self.package["path"])
        target = self.base / "tampered" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        entries = change(entries)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        data = target.read_bytes()
        return target, {"version": HIGH, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def verify(self, path, offer):
        return pu.verify_package(path, offer, LOW, "development", False)

    def test_genuine_package_passes_the_check(self):
        offer = {"version": HIGH, "size": self.package["size"], "sha256": self.package["sha256"]}
        manifest = self.verify(self.package["path"], offer)
        names = {entry["path"] for entry in manifest["files"]}
        self.assertIn("src/python/tray.py", names)
        self.assertIn("webgui/dist/index.html", names)
        self.assertIn("update_flowshift.ps1", names)
        self.assertFalse(any(name.startswith("offline/") for name in names))
        self.assertNotIn("src/python/config.json", names)

    def assert_refused(self, change, code):
        path, offer = self.rewritten(change)
        with self.assertRaises(pu.PeerPackageError) as caught:
            self.verify(path, offer)
        self.assertEqual(caught.exception.code, code, caught.exception.message)

    def test_changed_program_file_is_refused(self):
        top = pu.top_folder(HIGH)
        self.assert_refused(lambda entries: [
            (name, data + b"\nimport os; os.system('attrappe')\n" if name == f"{top}/src/python/tray.py" else data)
            for name, data in entries], "file_damaged")

    def test_extra_unlisted_file_is_refused(self):
        top = pu.top_folder(HIGH)
        self.assert_refused(lambda entries: entries + [(f"{top}/src/python/sitecustomize.py", b"x")],
                            "unlisted_file")

    def test_missing_file_is_refused(self):
        top = pu.top_folder(HIGH)
        self.assert_refused(lambda entries: [(n, d) for n, d in entries if n != f"{top}/src/python/web_api.py"],
                            "incomplete")

    def test_unsafe_path_is_refused(self):
        self.assert_refused(lambda entries: entries + [("../evil.ps1", b"x")], "unsafe_path")
        top = pu.top_folder(HIGH)
        self.assert_refused(lambda entries: entries + [(f"{top}/../../evil.ps1", b"x")], "unsafe_path")

    def test_version_file_that_differs_from_the_offer_is_refused(self):
        top = pu.top_folder(HIGH)

        def change(entries):
            out = []
            for name, data in entries:
                if name == f"{top}/VERSION":
                    data = b"0.7.0-dev.99\n"
                if name == f"{top}/{pu.MANIFEST_NAME}":
                    manifest = json.loads(data)
                    for entry in manifest["files"]:
                        if entry["path"] == "VERSION":
                            entry["size"] = len(b"0.7.0-dev.99\n")
                            entry["sha256"] = hashlib.sha256(b"0.7.0-dev.99\n").hexdigest()
                    data = json.dumps(manifest).encode()
                out.append((name, data))
            return out
        self.assert_refused(change, "version_mismatch")

    def test_downgrade_package_is_refused_even_when_complete(self):
        offer = {"version": HIGH, "size": self.package["size"], "sha256": self.package["sha256"]}
        with self.assertRaises(pu.PeerPackageError) as caught:
            pu.verify_package(self.package["path"], offer, "0.7.1", "development", False)
        self.assertEqual(caught.exception.code, "not_newer")

    def test_bytes_that_differ_from_the_offer_are_deleted_and_never_installed(self):
        tampered, _ = self.rewritten(lambda entries: entries + [("x", b"y")])
        bad_cache = self.fixed_cache({**self.package, "path": str(tampered)})
        self.sender.cache = bad_cache
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        genuine = self.fixed_cache(self.package).offer_message("Viktor-PC")
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), genuine)
        snapshot = self.wait_state(ctrl, {pu.FAILED})
        self.assertEqual(snapshot["error"]["code"], "protocol_error")
        self.assertEqual(self.handoffs, [])
        self.assertFalse(self.shutdown.is_set())
        self.assertFalse(list((self.data_dir / "updates" / "downloads").glob("*.zip")))

    def test_manipulated_package_with_a_matching_offer_is_never_handed_over(self):
        top = pu.top_folder(HIGH)
        tampered, offer = self.rewritten(lambda entries: [
            (name, data + b"# attrappe\n" if name == f"{top}/install_flowshift.ps1" else data)
            for name, data in entries])
        package = {"path": str(tampered), "version": HIGH, **offer}
        self.sender.cache = self.fixed_cache(package)
        self.settings["from_peers"]["policy"] = "install"
        ctrl = self.controller()
        ctrl.on_offer(f"device:{SENDER_ID}", self.peer(), self.sender.cache.offer_message("Viktor-PC"))
        snapshot = self.wait_state(ctrl, {pu.FAILED})
        self.assertEqual(snapshot["error"]["code"], "file_damaged")
        self.assertEqual(self.handoffs, [])
        self.assertFalse(list((self.data_dir / "updates" / "downloads").glob("*.zip")))
        self.assertTrue(any("file_damaged" in line for line in self.lines))


class PeerUpdateRules(unittest.TestCase):
    def test_who_offers(self):
        self.assertTrue(pu.should_offer("0.7.0-dev.57", "0.7.0-dev.56"))
        self.assertTrue(pu.should_offer("0.7.0", "0.7.0-dev.99"))
        self.assertFalse(pu.should_offer("0.7.0-dev.56", "0.7.0-dev.57"))
        self.assertFalse(pu.should_offer("0.7.0", "0.7.0"))
        self.assertFalse(pu.should_offer("0.7.0", "unknown"))

    def test_accept_rules(self):
        self.assertEqual(pu.accept_decision("0.6.3", "0.7.0", "stable", False), (True, None))
        self.assertEqual(pu.accept_decision("0.6.3", "0.7.0-dev.5", "stable", False),
                         (False, "development_version_not_allowed"))
        self.assertEqual(pu.accept_decision("0.6.3", "0.7.0-dev.5", "stable", True), (True, None))
        self.assertEqual(pu.accept_decision("0.7.0-dev.4", "0.7.0-dev.5", "development", False), (True, None))
        self.assertEqual(pu.accept_decision("0.7.0", "0.7.0-dev.5", "development", True), (False, "not_newer"))
        self.assertEqual(pu.accept_decision("0.7.0", "0.7.1+build.1", "stable", False), (False, "invalid_version"))

    def test_only_listed_devices_are_paired(self):
        peers = [{"name": "A", "device_id": "AA11BB22", "host": "10.0.0.2"}, {"name": "B", "host": "10.0.0.3"}]
        self.assertEqual(pu.trusted_peer(peers, "device:aa11bb22")["name"], "A")
        self.assertIsNone(pu.trusted_peer(peers, "endpoint:10.0.0.3:45781"))
        self.assertIsNone(pu.trusted_peer(peers, "name:B"))
        self.assertIsNone(pu.trusted_peer(peers, "device:aa11bb22", own_device_id="aa11bb22"))
        self.assertIsNone(pu.trusted_peer(peers, "device:"))

    def test_offer_validation(self):
        good = {"type": pu.T_OFFER, "version": "0.7.0", "size": 10, "sha256": "a" * 64}
        self.assertIsNotNone(pu.validate_offer(good))
        for bad in ({**good, "size": 0}, {**good, "size": pu.MAX_PACKAGE_SIZE + 1}, {**good, "size": True},
                    {**good, "sha256": "A" * 64}, {**good, "version": "v0.7.0"}, {**good, "type": "x"}):
            self.assertIsNone(pu.validate_offer(bad))

    def test_settings_default_to_notify_without_development_versions(self):
        import config_schema
        updates = config_schema.normalize_updates({})
        self.assertEqual(updates["from_peers"], {"policy": "notify", "allow_development": False})
        updates = config_schema.normalize_updates({"from_peers": {"policy": "force", "allow_development": "yes"}})
        self.assertEqual(updates["from_peers"], {"policy": "notify", "allow_development": True})


class PeerUpdateMonitorRules(unittest.TestCase):
    def test_other_link_protocol_keeps_hello_and_update_but_blocks_switching(self):
        self.assertEqual(pu.link_mode(1, 1), pu.LINK_FULL)
        self.assertEqual(pu.link_mode(1, 2), pu.LINK_UPDATE_ONLY)
        self.assertEqual(pu.link_mode(1, 0), pu.LINK_UPDATE_ONLY)
        self.assertEqual(pu.link_mode(1, None), pu.LINK_UPDATE_ONLY)
        monitor = pu.PeerUpdateMonitor(HIGH)
        # Older peer with another protocol: still offered, but no forwarding.
        self.assertEqual(monitor.on_link("device:cc33dd44", "PC 2", LOW, pu.LINK_UPDATE_ONLY), pu.PEER_OLDER)
        self.assertIn("must be updated first", monitor.switch_block("device:cc33dd44"))
        # Newer peer with another protocol: this PC is the one to update.
        self.assertEqual(monitor.on_link("device:ee55ff66", "PC 3", "0.8.0", pu.LINK_UPDATE_ONLY),
                         pu.PEER_UPDATE_ONLY)
        self.assertIn("must be updated first", monitor.switch_block("device:ee55ff66"))
        # Same version, same protocol: nothing to show, nothing blocked.
        self.assertIsNone(monitor.on_link("device:0a0b0c0d", "PC 4", HIGH))
        self.assertIsNone(monitor.view("device:0a0b0c0d"))
        self.assertIsNone(monitor.switch_block("device:0a0b0c0d"))

    def test_peer_that_does_not_come_back_is_reported(self):
        now = [1000.0]
        monitor = pu.PeerUpdateMonitor(HIGH, clock=lambda: now[0], restart_timeout=300)
        peer = "device:cc33dd44"
        monitor.on_link(peer, "PC 2", LOW)
        monitor.on_status(peer, "PC 2", {"type": pu.T_STATUS, "state": pu.INSTALLING,
                                         "installed_version": LOW, "version": HIGH})
        self.assertEqual(monitor.view(peer)["text"], "Is being installed")
        monitor.on_unlink(peer)
        self.assertEqual(monitor.view(peer)["phase"], pu.PEER_RESTARTING)
        self.assertIn("about 90 s", monitor.view(peer)["text"])
        now[0] += 301
        view = monitor.view(peer)
        self.assertEqual(view["phase"], pu.PEER_FAILED)
        self.assertEqual(view["text"], f"Update failed: did not come back within 5 minutes; runs on with {LOW}")
        self.assertIsNone(monitor.switch_block(peer))

    def test_interrupted_transfer_and_malformed_reports(self):
        monitor = pu.PeerUpdateMonitor(HIGH)
        peer = "device:cc33dd44"
        monitor.on_link(peer, "PC 2", LOW)
        monitor.on_status(peer, "PC 2", {"type": pu.T_STATUS, "state": pu.DOWNLOADING,
                                         "installed_version": LOW, "version": HIGH, "percent": 37})
        self.assertEqual(monitor.view(peer)["text"], "Update is being transferred (37 %)")
        monitor.on_unlink(peer)
        self.assertEqual(monitor.view(peer)["text"], "Transfer interrupted; continues when connected again")
        for junk in ({"type": pu.T_STATUS, "state": "hacked", "installed_version": LOW},
                     {"type": pu.T_STATUS, "state": pu.DOWNLOADING, "installed_version": "not a version"},
                     {"type": pu.T_STATUS, "state": pu.DOWNLOADING, "installed_version": LOW, "version": "1.0.0+x"},
                     "text", None):
            self.assertIsNone(monitor.on_status(peer, "PC 2", junk))
        self.assertIsNone(pu.validate_status({"type": pu.T_STATUS, "state": pu.DOWNLOADING,
                                              "installed_version": LOW, "percent": 900}).get("percent"))
        self.assertEqual(monitor.view(peer)["phase"], pu.PEER_INTERRUPTED)

    def test_peer_without_reports_that_comes_back_updated_is_shown_as_updated(self):
        notices = []
        monitor = pu.PeerUpdateMonitor(HIGH, notify=lambda title, text: notices.append(title))
        peer = "device:cc33dd44"
        monitor.on_link(peer, "PC 2", LOW)
        # A second link (inbound + outbound) of the same peer changes nothing.
        self.assertEqual(monitor.on_link(peer, "PC 2", LOW), pu.PEER_OLDER)
        monitor.on_unlink(peer)
        self.assertEqual(monitor.on_link(peer, "PC 2", HIGH), pu.PEER_UPDATED)
        self.assertEqual(notices, ["PC 2 is updated"])
        # Still the same version on the next reconnect: no second message.
        monitor.on_unlink(peer)
        self.assertIsNone(monitor.on_link(peer, "PC 2", HIGH))
        self.assertEqual(notices, ["PC 2 is updated"])

    def test_second_hello_with_old_version_keeps_waiting_or_ready(self):
        notices = []
        monitor = pu.PeerUpdateMonitor(HIGH, notify=lambda title, text: notices.append(title))
        peer = "device:cc33dd44"
        monitor.on_link(peer, "PC 2", LOW)
        monitor.on_status(peer, "PC 2", {"type": pu.T_STATUS, "state": pu.WAITING,
                                         "installed_version": LOW, "version": HIGH,
                                         "wait_reason": "edge_session_active"})
        self.assertEqual(monitor.view(peer)["phase"], pu.PEER_WAITING)
        # Second hello (other connection direction) still with the old version.
        self.assertEqual(monitor.on_link(peer, "PC 2", LOW), pu.PEER_WAITING)
        view = monitor.view(peer)
        self.assertEqual(view["phase"], pu.PEER_WAITING)
        self.assertEqual(view["text"], "Update waits until you are back on this PC")
        self.assertNotIn("PC 2 update failed", notices)
        # Same for a downloaded update that waits for approval.
        monitor.on_status(peer, "PC 2", {"type": pu.T_STATUS, "state": pu.DOWNLOADED,
                                         "installed_version": LOW, "version": HIGH})
        self.assertEqual(monitor.on_link(peer, "PC 2", LOW), pu.PEER_READY)

    def test_peer_back_after_restart_with_old_version_is_failed(self):
        monitor = pu.PeerUpdateMonitor(HIGH)
        peer = "device:cc33dd44"
        monitor.on_link(peer, "PC 2", LOW)
        monitor.on_status(peer, "PC 2", {"type": pu.T_STATUS, "state": pu.INSTALLING,
                                         "installed_version": LOW, "version": HIGH})
        monitor.on_unlink(peer)
        self.assertEqual(monitor.on_link(peer, "PC 2", LOW), pu.PEER_FAILED)
        self.assertEqual(monitor.view(peer)["error"]["code"], "not_updated")

    def test_waiting_texts(self):
        entry = {"phase": pu.PEER_WAITING, "name": "PC 2", "wait_reason": "edge_session_active"}
        self.assertEqual(pu.describe(entry), "Update waits until you are back on this PC")
        entry["wait_reason"] = "clipboard_transfer_active"
        self.assertEqual(pu.describe(entry), "Update waits until PC 2 is idle (clipboard_transfer_active)")
        self.assertEqual(pu.describe({"phase": pu.PEER_WAITING, "install_in_seconds": 42}), "Installs in about 42 s")


class PeerHandoff(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.program_files = self.root / "Program Files"
        self.program_data = self.root / "ProgramData"
        self.install_dir = self.program_files / "FlowShift"
        self.data_dir = self.program_data / "FlowShift"
        self.install_dir.mkdir(parents=True)
        (self.install_dir / update_handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
        self.package = self.data_dir / "updates" / "downloads" / f"FlowShift-Peer-{HIGH}.zip"
        self.package.parent.mkdir(parents=True)
        self.package.write_bytes(b"attrappe package")
        self.digest = hashlib.sha256(b"attrappe package").hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def launcher(self, arguments, **kwargs):
        plan = json.loads(Path(arguments[arguments.index("-PlanPath") + 1]).read_text(encoding="utf-8"))
        ack = self.data_dir / "updates" / "acks" / f"update_ack-{plan['request_id']}.json"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text(json.dumps({"schema_version": 1, "request_id": plan["request_id"],
                                   "status": "accepted", "runner_pid": 42}), encoding="utf-8")
        return object()

    def call(self, **changes):
        values = dict(from_version=LOW, to_version=HIGH, package_path=self.package,
                      size=len(b"attrappe package"), sha256=self.digest, runtime_root=self.install_dir,
                      runtime_pid=1234, launcher=self.launcher, ack_timeout=1,
                      _program_files=self.program_files, _program_data=self.program_data,
                      elevation_check=lambda: True, lock_check=lambda _: False)
        values.update(changes)
        return update_handoff.handoff_peer_package(**values)

    def test_plan_names_the_peer_package(self):
        result = self.call()
        self.assertEqual(result.status, "started")
        plan = json.loads((self.data_dir / "updates" / "update_plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["from_version"], plan["to_version"], plan["package_kind"]), (LOW, HIGH, "peer"))
        self.assertEqual(Path(plan["installer_path"]).name, f"FlowShift-Peer-{HIGH}.zip")
        self.assertEqual(plan["installer_sha256"], self.digest)

    def test_refuses_downgrade_changed_bytes_and_foreign_paths(self):
        with self.assertRaises(update_handoff.UpdateHandoffError) as caught:
            self.call(from_version=HIGH, to_version=LOW)
        self.assertEqual(caught.exception.code, "invalid_release")
        with self.assertRaises(update_handoff.UpdateHandoffError) as caught:
            self.call(sha256="0" * 64)
        self.assertEqual(caught.exception.code, "hash_mismatch")
        elsewhere = self.root / f"FlowShift-Peer-{HIGH}.zip"
        elsewhere.write_bytes(b"attrappe package")
        with self.assertRaises(update_handoff.UpdateHandoffError) as caught:
            self.call(package_path=elsewhere)
        self.assertEqual(caught.exception.code, "invalid_asset")
        self.assertFalse((self.data_dir / "updates" / "update_plan.json").exists())


class UpdaterScriptKnowsPeerPackages(unittest.TestCase):
    def test_runner_accepts_and_unpacks_peer_packages(self):
        script = (Path(__file__).resolve().parents[2] / "update_flowshift.ps1").read_text(encoding="utf-8")
        self.assertIn("$packageKind -cne 'peer'", script)
        self.assertIn("'peer' { \"FlowShift-Peer-$toVersion.zip\" }", script)
        self.assertIn("$Plan.package_kind -ceq 'peer'", script)
        self.assertNotIn("$plan.package_kind -ceq 'source'", script)

    def test_unchanged_requirements_reuse_the_python_packages(self):
        # Viktor 25.11. 17:34: between PCs that have FlowShift only program and
        # WebGUI travel; Python packages only when requirements.txt changed.
        root = Path(__file__).resolve().parents[2]
        runner = (root / "update_flowshift.ps1").read_text(encoding="utf-8")
        installer = (root / "install_flowshift.ps1").read_text(encoding="utf-8-sig")
        set_at = runner.index("$env:FLOWSHIFT_PREVIOUS_INSTALL = $plan.rollback_dir")
        self.assertLess(runner.index("Prepare-FlowShiftRollback -Plan $plan"), set_at)
        self.assertIn("Remove-Item Env:FLOWSHIFT_PREVIOUS_INSTALL", runner)
        reuse = installer.split("function Get-ReusableVenv", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if ($script:UsbPackage -or -not $PyInfo) { return $null }", reuse)
        self.assertIn("(Get-FileHash -LiteralPath $oldReq -Algorithm SHA256).Hash -ne $newHash", reuse)
        self.assertIn("$venvHome -ine $pythonHome", reuse)
        self.assertIn("$env:FLOWSHIFT_PREVIOUS_INSTALL", reuse)
        body = installer.split("$reuseVenv = Get-ReusableVenv $pyInfo", 1)[1]
        # Copied, never moved: the rollback copy stays complete.
        self.assertIn("Copy-Item -LiteralPath $reuseVenv -Destination $VenvDir -Recurse", body.split("\n    }\n", 1)[0])
        self.assertNotIn("Move-Item -LiteralPath $reuseVenv", installer)
        deps = body.split("Step 'Installing Python dependencies'", 1)[1].split("Ensure-WebView2Runtime", 1)[0]
        reused, fresh = deps.split("} else {", 1)
        self.assertIn("if ($venvReused)", reused)
        self.assertNotIn("pip', 'install'", reused)
        self.assertIn("'--require-hashes', '-r', $req", fresh)
        # A copied venv that does not start falls back to a fresh one.
        self.assertIn("Remove-Item -LiteralPath $VenvDir -Recurse -Force", body)
        self.assertIn("if (-not $venvReused) {", body)


if __name__ == "__main__":
    unittest.main()
