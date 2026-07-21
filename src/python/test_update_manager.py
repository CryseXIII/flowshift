"""Focused offline tests for the persistent update manager and downloader."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_download as download
from update_manager import (
    ERROR_INCOMPATIBLE_UPDATER,
    RESULT_ALREADY_RUNNING,
    RESULT_NOT_AVAILABLE,
    RESULT_QUEUED,
    RESULT_SHUTTING_DOWN,
    UpdateManager,
    is_development_mode,
)
from update_model import ReleaseDescriptor, STATUS_ERROR, STATUS_UPDATE_AVAILABLE, UpdateResult
from update_state import (
    CHECKING,
    DOWNLOADED,
    DOWNLOADING,
    ERROR,
    IDLE,
    INSTALL_HANDOFF,
    INSTALLING,
    RESTARTING,
    UPDATE_AVAILABLE,
    UP_TO_DATE,
    WAITING_FOR_IDLE,
    InvalidStateTransition,
    UpdateStateMachine,
    UpdateStateStore,
)


def descriptor(content=b"verified installer", version="0.5.0", minimum="0.4.0"):
    digest = hashlib.sha256(content).hexdigest()
    return ReleaseDescriptor(
        current_version="0.4.0",
        version=version,
        tag=f"v{version}",
        relation="newer",
        release_url=f"https://github.com/CryseXIII/flowshift/releases/tag/v{version}",
        release_notes="Notes",
        installer_url=("https://github.com/CryseXIII/flowshift/releases/download/"
                       f"v{version}/FlowShift-Setup.exe"),
        installer_size=len(content),
        installer_sha256=digest,
        manifest_url="https://github.com/CryseXIII/flowshift/update-manifest.json",
        checksums_url="https://github.com/CryseXIII/flowshift/SHA256SUMS.txt",
        minimum_updater_version=minimum,
    )


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def request(self, request):
        with self.lock:
            self.requests.append(request)
            if not self.responses:
                raise AssertionError("unexpected request")
            outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(status, content=b"", headers=None, stream=None):
    return download.DownloadResponse(status, headers or {}, stream or io.BytesIO(content))


class ChunkStream:
    def __init__(self, chunks, delay=0):
        self.chunks = list(chunks)
        self.delay = delay
        self.started = threading.Event()
        self.closed = False

    def read(self, size):
        self.started.set()
        if self.delay:
            time.sleep(self.delay)
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class UpdateStateTests(unittest.TestCase):
    def test_every_transition_through_handoff_and_restart(self):
        machine = UpdateStateMachine("0.4.0")
        for target in (CHECKING, UPDATE_AVAILABLE, DOWNLOADING, DOWNLOADED,
                       WAITING_FOR_IDLE, INSTALL_HANDOFF, INSTALLING, RESTARTING, IDLE):
            machine.transition(target)
        self.assertEqual(machine.snapshot()["state"], IDLE)
        machine.transition(CHECKING)
        machine.transition(UP_TO_DATE)
        self.assertEqual(machine.snapshot()["state"], UP_TO_DATE)

    def test_invalid_transition_and_error_retry(self):
        machine = UpdateStateMachine("0.4.0")
        with self.assertRaises(InvalidStateTransition):
            machine.transition(DOWNLOADED)
        machine.transition(ERROR)
        machine.transition(CHECKING)
        machine.transition(ERROR)
        machine.transition(DOWNLOADING)
        self.assertEqual(machine.snapshot()["state"], DOWNLOADING)

    def test_snapshot_is_deep_copy(self):
        machine = UpdateStateMachine("0.4.0")
        first = machine.snapshot()
        first["timestamps"]["created_at"] = "changed"
        self.assertNotEqual(machine.snapshot()["timestamps"]["created_at"], "changed")

    def test_corrupt_state_is_preserved_and_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "update_state.json"
            path.write_bytes(b'{"broken":')
            store = UpdateStateStore(path=path)
            state = store.load("0.4.0")
            backups = list(path.parent.glob("update_state.backup-corrupt-*.json"))
            self.assertEqual(state["state"], IDLE)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'{"broken":')
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_atomic_save_and_recovery_indicators_are_non_destructive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            part = root / "updates" / "downloads" / "old.exe.part"
            plan = root / "updates" / "update_plan.json"
            rollback = root / "rollback"
            result = root / "update_result.json"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"partial")
            plan.write_text("{}", encoding="utf-8")
            rollback.mkdir()
            result.write_text("{}", encoding="utf-8")
            store = UpdateStateStore(data_dir=root)
            replacements = []
            real_replace = os.replace

            def recording_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                return real_replace(source, destination)

            import update_state
            update_state.os.replace = recording_replace
            try:
                self.assertTrue(store.save(store.load("0.4.0")))
            finally:
                update_state.os.replace = real_replace
            notices = store.recovery_notices()
            codes = {item["code"] for item in notices}
            self.assertTrue({"stale_partial_download", "stale_update_plan",
                             "rollback_directory_present", "update_result_present"} <= codes)
            self.assertTrue(part.exists() and plan.exists() and rollback.exists() and result.exists())
            self.assertTrue(any(source.suffix == ".tmp" and destination == store.path
                                for source, destination in replacements))


class DownloadTests(unittest.TestCase):
    def run_download(self, root, content, transport=None, release=None, **kwargs):
        release = release or descriptor(content)
        transport = transport or FakeTransport([
            response(200, content, {"Content-Length": str(len(content))})])
        asset = download.download_installer(
            release, root, "0.4.0", transport=transport, **kwargs)
        return asset, transport

    def test_success_streams_progress_hash_size_and_atomic_finalization(self):
        content = b"0123456789" * 100
        with tempfile.TemporaryDirectory() as temporary:
            progress = []
            replacements = []
            real_replace = download.os.replace
            old_interval = download.PROGRESS_INTERVAL_SECONDS

            def recording_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                return real_replace(source, destination)

            download.os.replace = recording_replace
            download.PROGRESS_INTERVAL_SECONDS = 0
            try:
                stream = ChunkStream([content[:300], content[300:700], content[700:]])
                asset, transport = self.run_download(
                    temporary, content,
                    FakeTransport([response(200, headers={"Content-Length": str(len(content))},
                                                    stream=stream)]),
                    progress_callback=progress.append,
                )
            finally:
                download.os.replace = real_replace
                download.PROGRESS_INTERVAL_SECONDS = old_interval
            final = Path(asset.path)
            self.assertEqual(final.read_bytes(), content)
            self.assertEqual(asset.sha256, hashlib.sha256(content).hexdigest())
            self.assertFalse(final.with_suffix(final.suffix + ".part").exists())
            self.assertEqual(progress[-1]["percentage"], 100.0)
            self.assertTrue(any(0 < item["bytes_downloaded"] < len(content) for item in progress))
            self.assertTrue(any(source.name.endswith(".part") and destination == final
                                for source, destination in replacements))
            self.assertEqual(transport.requests[0].headers["User-Agent"], "FlowShift/0.4.0")

    def test_incorrect_hash_size_mismatch_and_truncation_delete_part(self):
        content = b"correct bytes"
        cases = [
            (replace(descriptor(content), installer_sha256="00" * 32),
             response(200, content, {"Content-Length": str(len(content))}),
             download.ERROR_HASH_MISMATCH),
            (descriptor(content), response(200, content, {"Content-Length": str(len(content) + 1)}),
             download.ERROR_SIZE_MISMATCH),
            (descriptor(content), response(200, content[:-1]), download.ERROR_SIZE_MISMATCH),
        ]
        for release, fake_response, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(download.DownloadError) as raised:
                    self.run_download(temporary, content, FakeTransport([fake_response]), release)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(list(Path(temporary).rglob("*.part")))
                self.assertFalse(list(Path(temporary).rglob("*.exe")))

    def test_interruption_deletes_part(self):
        content = b"a" * 20
        checks = iter((False, False, True))
        stream = ChunkStream([content[:10], content[10:]])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(download.DownloadError) as raised:
                self.run_download(
                    temporary, content,
                    FakeTransport([response(200, headers={}, stream=stream)]),
                    should_abort=lambda: next(checks, True),
                )
            self.assertEqual(raised.exception.code, download.ERROR_INTERRUPTED)
            self.assertFalse(list(Path(temporary).rglob("*.part")))

    def test_stale_part_clean_restart_and_final_reuse_or_replacement(self):
        content = b"fresh installer"
        release = descriptor(content)
        with tempfile.TemporaryDirectory() as temporary:
            downloads, final, part = download._validated_paths(temporary, release.version)
            downloads.mkdir(parents=True)
            part.write_bytes(b"stale")
            first, first_transport = self.run_download(temporary, content, release=release)
            self.assertEqual(final.read_bytes(), content)
            self.assertFalse(part.exists())
            no_requests = FakeTransport([])
            reuse_progress = []
            reused, _ = self.run_download(
                temporary, content, no_requests, release,
                progress_callback=reuse_progress.append)
            self.assertTrue(reused.reused)
            self.assertFalse(no_requests.requests)
            self.assertEqual(reuse_progress[-1]["percentage"], 100.0)
            final.write_bytes(b"corrupt")
            replacement, replacement_transport = self.run_download(temporary, content, release=release)
            self.assertFalse(replacement.reused)
            self.assertEqual(len(replacement_transport.requests), 1)
            self.assertEqual(Path(first.path).read_bytes(), content)

    def test_only_descriptor_url_and_managed_path_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(download.DownloadError):
                download.download_installer(
                    object(), temporary, "0.4.0", transport=FakeTransport([]))
            unsafe = replace(descriptor(b"x"), installer_url="https://example.com/setup.exe")
            with self.assertRaises(download.DownloadError) as raised:
                download.download_installer(
                    unsafe, temporary, "0.4.0", transport=FakeTransport([]))
            self.assertEqual(raised.exception.code, "invalid_url")

    def test_redirects_validate_every_host_and_enforce_count(self):
        content = b"redirected"
        release = descriptor(content)
        valid = FakeTransport([
            response(302, headers={"Location": "https://objects.githubusercontent.com/object"}),
            response(200, content, {"Content-Length": str(len(content))}),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            self.run_download(temporary, content, valid, release)
        self.assertEqual(valid.requests[-1].url, "https://objects.githubusercontent.com/object")

        unsafe = FakeTransport([response(302, headers={"Location": "https://evil.example/setup"})])
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(download.DownloadError) as raised:
            self.run_download(temporary, content, unsafe, release)
        self.assertEqual(raised.exception.code, "invalid_url")

        redirects = FakeTransport([
            response(302, headers={"Location": f"https://objects.githubusercontent.com/{index}"})
            for index in range(download.MAX_DOWNLOAD_REDIRECTS + 1)
        ])
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(download.DownloadError) as raised:
            self.run_download(temporary, content, redirects, release)
        self.assertEqual(raised.exception.code, "too_many_redirects")


class ManagerTests(unittest.TestCase):
    def make_manager(self, root, discovery, **kwargs):
        return UpdateManager(
            data_dir=root,
            current_version="0.4.0",
            updater_version="0.4.0",
            discovery=discovery,
            runtime_root=Path(root) / "checkout",
            **kwargs,
        )

    def test_concurrent_check_rejected_and_descriptor_persisted(self):
        started = threading.Event()
        release_check = threading.Event()
        release = descriptor(b"manager")

        def delayed(**kwargs):
            started.set()
            release_check.wait(2)
            return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, delayed)
            try:
                self.assertEqual(manager.check_for_updates().status, RESULT_QUEUED)
                self.assertTrue(started.wait(1))
                self.assertEqual(manager.check_for_updates().status, RESULT_ALREADY_RUNNING)
                self.assertEqual(manager.download_update().status, RESULT_ALREADY_RUNNING)
                release_check.set()
                self.assertTrue(manager.wait_for_quiescence())
                snap = manager.snapshot()
                self.assertEqual(snap["state"], UPDATE_AVAILABLE)
                self.assertEqual(snap["release"]["installer_sha256"], release.installer_sha256)
                persisted = json.loads((Path(temporary) / "update_state.json").read_text("utf-8"))
                self.assertEqual(persisted["current_version"], "0.4.0")
                self.assertEqual(persisted["latest_version"], "0.5.0")
                self.assertIsNotNone(persisted["last_check_at"])
                self.assertIsNotNone(persisted["last_successful_check_at"])
            finally:
                release_check.set()
                manager.shutdown()

    def test_streamed_manager_download_and_concurrent_rejection(self):
        content = b"manager streamed bytes"
        release = descriptor(content)
        stream = ChunkStream([content[:5], content[5:]], delay=0.05)
        transport = FakeTransport([
            response(200, headers={"Content-Length": str(len(content))}, stream=stream)])

        def discover(**kwargs):
            return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, discover, download_transport=transport)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.download_update().status, RESULT_QUEUED)
                self.assertTrue(stream.started.wait(1))
                self.assertEqual(manager.download_update().status, RESULT_ALREADY_RUNNING)
                self.assertTrue(manager.wait_for_quiescence())
                snap = manager.snapshot()
                self.assertEqual(snap["state"], DOWNLOADED)
                self.assertEqual(snap["download_progress"]["percentage"], 100.0)
                self.assertEqual(Path(snap["downloaded_asset"]["path"]).read_bytes(), content)
            finally:
                manager.shutdown()

    def test_minimum_updater_blocks_download(self):
        release = descriptor(b"future", minimum="0.5.0")

        def discover(**kwargs):
            return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, discover, download_transport=FakeTransport([]))
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                result = manager.download_update()
                self.assertEqual(result.status, RESULT_NOT_AVAILABLE)
                self.assertEqual(result.error_code, ERROR_INCOMPATIBLE_UPDATER)
                self.assertEqual(manager.snapshot()["last_error"]["code"], ERROR_INCOMPATIBLE_UPDATER)
            finally:
                manager.shutdown()

    def test_error_then_retry(self):
        release = descriptor(b"retry")
        calls = iter((UpdateResult(STATUS_ERROR, error_code="offline", message="offline"),
                      UpdateResult(STATUS_UPDATE_AVAILABLE, release)))

        def discover(**kwargs):
            return next(calls)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, discover)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["state"], ERROR)
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["state"], UPDATE_AVAILABLE)
            finally:
                manager.shutdown()

    def test_same_descriptor_is_normalized_to_up_to_date(self):
        same = replace(descriptor(b"same", version="0.4.0"), relation="same")

        def discover(**kwargs):
            return UpdateResult(STATUS_UPDATE_AVAILABLE, same)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, discover)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["state"], UP_TO_DATE)
                self.assertEqual(manager.download_update().status, RESULT_NOT_AVAILABLE)
            finally:
                manager.shutdown()

    def test_shutdown_during_delayed_check_and_download(self):
        gate = threading.Event()
        started = threading.Event()
        release = descriptor(b"shutdown")

        def delayed(**kwargs):
            started.set()
            gate.wait(2)
            return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, delayed)
            manager.check_for_updates()
            self.assertTrue(started.wait(1))
            self.assertFalse(manager.shutdown(timeout=0.01))
            self.assertEqual(manager.check_for_updates().status, RESULT_SHUTTING_DOWN)
            gate.set()
            self.assertTrue(manager.shutdown(timeout=2))
            self.assertEqual(manager.snapshot()["state"], ERROR)

        stream = ChunkStream([b"shut", b"down"], delay=0.1)
        transport = FakeTransport([response(200, headers={"Content-Length": "8"}, stream=stream)])

        def immediate(**kwargs):
            return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(temporary, immediate, download_transport=transport)
            manager.check_for_updates()
            self.assertTrue(manager.wait_for_quiescence())
            manager.download_update()
            self.assertTrue(stream.started.wait(1))
            self.assertTrue(manager.shutdown(timeout=2))
            snap = manager.snapshot()
            self.assertEqual(snap["state"], ERROR)
            self.assertFalse(list(Path(temporary).rglob("*.part")))

    def test_development_mode_is_exact_and_never_blocks_download_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            official = root / "Program Files" / "FlowShift"
            self.assertFalse(is_development_mode(official, official_install_dir=official,
                                                 program_files=root / "other"))
            self.assertTrue(is_development_mode(official / "src", official_install_dir=official,
                                                program_files=root / "other"))
            manager = UpdateManager(
                data_dir=root / "data",
                current_version="0.4.0",
                discovery=lambda **kwargs: UpdateResult(STATUS_ERROR, error_code="unused"),
                runtime_root=root / "checkout",
                official_install_dir=official,
                program_files=root / "other",
            )
            try:
                snap = manager.snapshot()
                self.assertTrue(snap["development_mode"])
                self.assertFalse(snap["can_install"])
                self.assertEqual(snap["blocked_reason"], "development_checkout")
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
