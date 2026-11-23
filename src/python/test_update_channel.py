"""Offline tests for the development update channel (Forgejo main) and channel switching.

The development channel has no package build: FlowShift reads the head commit
of Forgejo main and its VERSION, and only for a newer version downloads and
checks the source archive of exactly that commit. The Windows part (unpacking
and running install_flowshift.ps1 in update_flowshift.ps1) is a check step for
Viktor; everything here runs without Windows.
"""
from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import update_client
import update_credentials
import update_download
import update_handoff
import update_model
from update_client import HttpResponse
from update_download import DownloadResponse, DownloadedAsset
from update_manager import (
    ERROR_CHANNEL_CHANGED,
    RESULT_NOT_AVAILABLE,
    RESULT_QUEUED,
    UpdateManager,
)
from update_model import (
    CHANNEL_DEVELOPMENT,
    CHANNEL_STABLE,
    FORGEJO_BRANCH_API,
    FORGEJO_ORIGIN,
    STATUS_ERROR,
    STATUS_UPDATE_AVAILABLE,
    STATUS_UP_TO_DATE,
    UpdateResult,
)
from update_state import DOWNLOADED, ERROR, UPDATE_AVAILABLE, UP_TO_DATE
import web_api


# Obvious dummy; never shaped like a real access key.
TOKEN = "attrappe-zugang"
COMMIT = "c0ffee" + "0" * 34
OTHER_COMMIT = "beef" + "1" * 36


def source_zip(version="0.7.0-dev.30", commit=COMMIT, top="flowshift", extra=None,
               skip=(), comment=None):
    """A source archive shaped like Forgejo's (git archive): one top folder,
    the commit ID as ZIP comment."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{top}/", b"")
        files = {name: b"x" for name in update_model.SOURCE_REQUIRED_FILES}
        files["VERSION"] = f"{version}\n".encode()
        for name, data in files.items():
            if name not in skip:
                archive.writestr(f"{top}/{name}", data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
        archive.comment = (commit if comment is None else comment).encode()
    return buffer.getvalue()


def branch(commit=COMMIT, message="Merge pull request 'X' (#12)\n\nbody"):
    return {"name": "main", "commit": {"id": commit, "message": message}}


class RoutingTransport:
    """Answers by URL and records every request with its headers."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        answer = self.routes.get(request.url)
        if answer is None:
            raise AssertionError(f"unexpected request {request.url}")
        if isinstance(answer, Exception):
            raise answer
        status, body = answer[:2]
        headers = answer[2] if len(answer) > 2 else {}
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        return HttpResponse(status=status, headers=headers, body=body)


def dev_routes(version="0.7.0-dev.30", commit=COMMIT, archive=None, branch_status=200):
    return {
        FORGEJO_BRANCH_API: (branch_status, branch(commit)),
        update_model.forgejo_version_url(commit): (200, f"{version}\n".encode()),
        update_model.forgejo_archive_url(commit): (200, archive if archive is not None
                                                    else source_zip(version, commit)),
    }


def dev_descriptor(content=b"dev source", version="0.7.0-dev.30", current="0.7.0-dev.29"):
    return update_model.development_descriptor(
        current, version, COMMIT, "Merge", len(content), hashlib.sha256(content).hexdigest())


class ModelTests(unittest.TestCase):
    def test_branch_and_version_parsing(self):
        self.assertEqual(update_model.parse_branch_payload(branch()),
                         (COMMIT, "Merge pull request 'X' (#12)"))
        for bad in ({"name": "dev", "commit": {"id": COMMIT}}, {"name": "main"},
                    {"name": "main", "commit": {"id": "abc"}},
                    {"name": "main", "commit": {"id": COMMIT.upper()}}, []):
            with self.subTest(bad=bad), self.assertRaises(update_model.UpdateValidationError):
                update_model.parse_branch_payload(bad)
        self.assertEqual(str(update_model.parse_source_version(b"\xef\xbb\xbf0.7.0-dev.30\r\n")),
                         "0.7.0-dev.30")
        for bad in (b"0.7", b"v0.7.0", b"0.7.0+build", b"\xff", b"07.0.0"):
            with self.subTest(bad=bad), self.assertRaises(update_model.UpdateValidationError):
                update_model.parse_source_version(bad)

    def test_urls_are_pinned_to_the_commit_on_the_fixed_origin(self):
        self.assertEqual(update_model.forgejo_archive_url(COMMIT),
                         f"{FORGEJO_ORIGIN}/api/v1/repos/private-platform/flowshift/archive/{COMMIT}.zip")
        self.assertTrue(update_model.forgejo_version_url(COMMIT).endswith(f"/raw/VERSION?ref={COMMIT}"))
        with self.assertRaises(update_model.UpdateValidationError):
            update_model.forgejo_archive_url("main")
        for url in ("http://evil.example/x", "https://github.com/CryseXIII/flowshift",
                    "http://user:pw@forgejo.thegoodhunter.vps/x",
                    "http://forgejo.thegoodhunter.vps:3001/x", "ftp://forgejo.thegoodhunter.vps/x"):
            with self.subTest(url=url), self.assertRaises(update_model.UpdateValidationError):
                update_model.validate_forgejo_url(url)

    def test_source_archive_checks(self):
        def check(data, version="0.7.0-dev.30", commit=COMMIT):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return update_model.validate_source_archive(archive, commit, version)

        self.assertEqual(check(source_zip()), "flowshift")
        cases = {
            "other commit": (source_zip(), "0.7.0-dev.30", OTHER_COMMIT),
            "no comment": (source_zip(comment=""), "0.7.0-dev.30", COMMIT),
            "version differs": (source_zip("0.7.0-dev.31"), "0.7.0-dev.30", COMMIT),
            "parent path": (source_zip(extra={"flowshift/../evil.ps1": b"x"}), "0.7.0-dev.30", COMMIT),
            "absolute": (source_zip(extra={"/evil.ps1": b"x"}), "0.7.0-dev.30", COMMIT),
            "drive": (source_zip(extra={"C:/evil.ps1": b"x"}), "0.7.0-dev.30", COMMIT),
            "backslash": (source_zip(extra={"flowshift\\..\\evil": b"x"}), "0.7.0-dev.30", COMMIT),
            "second top": (source_zip(extra={"other/file": b"x"}), "0.7.0-dev.30", COMMIT),
            "no install script": (source_zip(skip=("install_flowshift.ps1",)), "0.7.0-dev.30", COMMIT),
        }
        for name, (data, version, commit) in cases.items():
            with self.subTest(name), self.assertRaises(update_model.UpdateValidationError) as raised:
                check(data, version, commit)
            self.assertEqual(raised.exception.code, update_model.ERROR_INVALID_SOURCE)

    def test_descriptor_relation_and_minimum_updater(self):
        descriptor = dev_descriptor(version="0.7.0-dev.30", current="0.7.0-dev.29")
        self.assertEqual((descriptor.relation, descriptor.channel, descriptor.tag),
                         ("newer", CHANNEL_DEVELOPMENT, "v0.7.0-dev.30"))
        self.assertEqual(descriptor.minimum_updater_version, update_model.SOURCE_UPDATER_MIN_VERSION)
        self.assertIn(COMMIT[:10], descriptor.release_notes)
        self.assertEqual(dev_descriptor(version="0.7.0-dev.10", current="0.7.0-dev.9").relation, "newer")
        self.assertEqual(dev_descriptor(version="0.7.0-dev.40", current="0.7.0").relation, "older")
        with self.assertRaises(update_model.UpdateValidationError):
            update_model.development_descriptor("0.7.0-dev.29", "0.7.0-dev.30", COMMIT, "",
                                                update_model.MAX_SOURCE_ARCHIVE_SIZE + 1, "a" * 64)

    def test_the_minimum_updater_is_this_version(self):
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
        installed = update_model.parse_source_version(version_file.read_bytes())
        self.assertGreaterEqual(installed, update_model.parse_source_version(
            update_model.SOURCE_UPDATER_MIN_VERSION))

    def test_config_accepts_development_and_falls_back_on_unknown(self):
        self.assertEqual(config_schema.normalize_updates({"channel": "Development"})["channel"],
                         CHANNEL_DEVELOPMENT)
        self.assertEqual(config_schema.normalize_updates({"channel": "beta"})["channel"],
                         CHANNEL_STABLE)


class DevelopmentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        update_client._source_digests.clear()

    def discover(self, transport, current="0.7.0-dev.29", token=TOKEN):
        return update_client.discover_release(
            current_version=current, transport=transport, channel=CHANNEL_DEVELOPMENT,
            token_provider=lambda: token)

    def test_newer_main_is_found_and_bound_to_the_checked_archive(self):
        archive = source_zip()
        transport = RoutingTransport(dev_routes(archive=archive))
        result = self.discover(transport)
        self.assertEqual(result.status, STATUS_UPDATE_AVAILABLE)
        release = result.release
        self.assertEqual((release.version, release.channel), ("0.7.0-dev.30", CHANNEL_DEVELOPMENT))
        self.assertEqual(release.installer_url, update_model.forgejo_archive_url(COMMIT))
        self.assertEqual(release.installer_size, len(archive))
        self.assertEqual(release.installer_sha256, hashlib.sha256(archive).hexdigest())
        self.assertEqual(len(transport.requests), 3)
        for request in transport.requests:
            self.assertTrue(request.url.startswith(FORGEJO_ORIGIN + "/"))
            self.assertEqual(request.headers["Authorization"], f"token {TOKEN}")

        # The next periodic check of an unchanged main does not download again.
        again = RoutingTransport(dev_routes(archive=archive))
        self.assertEqual(self.discover(again).release, release)
        self.assertEqual(len(again.requests), 2)

    def test_same_or_older_main_is_up_to_date_without_archive_download(self):
        for current in ("0.7.0-dev.30", "0.7.0-dev.31"):
            with self.subTest(current=current):
                transport = RoutingTransport(dev_routes())
                result = self.discover(transport, current=current)
                self.assertEqual(result.status, STATUS_UP_TO_DATE)
                self.assertIn(COMMIT[:10], result.message)
                self.assertEqual(len(transport.requests), 2)

    def test_tampered_archive_is_refused(self):
        for archive in (source_zip(commit=OTHER_COMMIT), source_zip("0.7.0-dev.31"),
                        source_zip(extra={"flowshift/../../x": b"x"}), b"not a zip"):
            with self.subTest(size=len(archive)):
                update_client._source_digests.clear()
                result = self.discover(RoutingTransport(dev_routes(archive=archive)))
                self.assertEqual((result.status, result.error_code),
                                 (STATUS_ERROR, update_model.ERROR_INVALID_SOURCE))

    def test_missing_token_makes_no_request(self):
        transport = RoutingTransport({})
        result = self.discover(transport, token=None)
        self.assertEqual((result.status, result.error_code),
                         (STATUS_ERROR, update_model.ERROR_CREDENTIALS_MISSING))
        self.assertEqual(transport.requests, [])

    def test_rejected_token(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                result = self.discover(RoutingTransport(dev_routes(branch_status=status)))
                self.assertEqual(result.error_code, update_model.ERROR_CREDENTIALS_REJECTED)

    def test_without_vpn_the_source_is_reported_unreachable(self):
        import socket
        for error in (socket.gaierror("no dns"), ConnectionRefusedError(), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                transport = RoutingTransport({FORGEJO_BRANCH_API: error})
                result = self.discover(transport)
                self.assertEqual(result.error_code, update_model.ERROR_SOURCE_UNREACHABLE)
                self.assertNotIn(TOKEN, result.message)

    def test_redirect_to_foreign_host_is_refused_and_gets_no_token(self):
        routes = dev_routes()
        routes[update_model.forgejo_archive_url(COMMIT)] = (
            302, b"", {"Location": "http://evil.example/a.zip"})
        transport = RoutingTransport(routes)
        result = self.discover(transport)
        self.assertEqual(result.error_code, update_model.ERROR_INVALID_URL)
        self.assertTrue(all(r.url.startswith(FORGEJO_ORIGIN) for r in transport.requests))

    def test_redirect_within_forgejo_is_followed(self):
        archive = source_zip()
        moved = f"{FORGEJO_ORIGIN}/private-platform/flowshift/archive/{COMMIT}.zip"
        routes = dev_routes(archive=archive)
        routes[update_model.forgejo_archive_url(COMMIT)] = (302, b"", {"Location": moved})
        routes[moved] = (200, archive)
        result = self.discover(RoutingTransport(routes))
        self.assertEqual(result.status, STATUS_UPDATE_AVAILABLE)


class DownloadTests(unittest.TestCase):
    def test_development_download_is_the_source_zip_with_token_and_hash(self):
        content = source_zip()
        release = dev_descriptor(content)
        seen = []

        def transport(request):
            seen.append(request)
            return DownloadResponse(200, {"Content-Length": str(len(content))}, io.BytesIO(content))

        with tempfile.TemporaryDirectory() as temporary:
            asset = update_download.download_installer(
                release, temporary, "0.7.0-dev.29", transport=transport,
                token_provider=lambda: TOKEN)
            self.assertEqual(asset.basename, "FlowShift-Source-0.7.0-dev.30.zip")
            self.assertEqual(asset.sha256, release.installer_sha256)
            self.assertEqual(Path(asset.path).read_bytes(), content)
        self.assertEqual(seen[0].url, update_model.forgejo_archive_url(COMMIT))
        self.assertEqual(seen[0].headers["Authorization"], f"token {TOKEN}")

    def test_changed_archive_bytes_are_refused(self):
        release = dev_descriptor(b"checked bytes")

        def transport(request):
            return DownloadResponse(200, {}, io.BytesIO(b"other bytes!!"))

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(update_download.DownloadError) as raised:
                update_download.download_installer(release, temporary, "0.7.0-dev.29",
                                                   transport=transport,
                                                   token_provider=lambda: TOKEN)
            self.assertEqual(raised.exception.code, update_download.ERROR_HASH_MISMATCH)

    def test_development_download_needs_token_and_refuses_foreign_redirect(self):
        release = dev_descriptor()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(update_download.DownloadError) as raised:
                update_download.download_installer(release, temporary, "0.7.0-dev.29",
                                                   transport=lambda r: None)
            self.assertEqual(raised.exception.code, update_model.ERROR_CREDENTIALS_MISSING)

            def redirect(request):
                return DownloadResponse(302, {"Location": "https://github.com/x"}, io.BytesIO())

            with self.assertRaises(update_download.DownloadError) as raised:
                update_download.download_installer(release, temporary, "0.7.0-dev.29",
                                                   transport=redirect,
                                                   token_provider=lambda: TOKEN)
            self.assertEqual(raised.exception.code, update_model.ERROR_INVALID_URL)

    def test_stable_download_never_sends_a_token(self):
        content = b"stable bytes"
        release = replace(
            dev_descriptor(content, version="0.7.0", current="0.6.3"),
            channel=CHANNEL_STABLE,
            installer_url="https://github.com/CryseXIII/flowshift/releases/download/v0.7.0/FlowShift-Setup.exe")
        seen = []

        def transport(request):
            seen.append(request)
            return DownloadResponse(200, {}, io.BytesIO(content))

        with tempfile.TemporaryDirectory() as temporary:
            asset = update_download.download_installer(
                release, temporary, "0.6.3", transport=transport, token_provider=lambda: TOKEN)
        self.assertEqual(asset.basename, "FlowShift-Setup-0.7.0.exe")
        self.assertNotIn("Authorization", seen[0].headers)


class NodeProbeTests(unittest.TestCase):
    def make_node(self, root):
        node = Path(root) / "nodejs" / "node.exe"
        node.parent.mkdir(parents=True)
        node.write_bytes(b"")
        (node.parent / "npm.cmd").write_bytes(b"")
        return node

    def test_node_24_or_newer_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            node = self.make_node(temporary)
            for output, code, expected in (("v24.11.0\n", 0, True), ("v26.0.0", 0, True),
                                           ("v22.12.0", 0, False), ("garbage", 0, False),
                                           ("v24.1.0", 1, False)):
                with self.subTest(output=output, code=code):
                    def run(arguments, **kwargs):
                        self.assertEqual(arguments, [str(node), "--version"])
                        return subprocess.CompletedProcess(arguments, code, stdout=output)
                    found = update_handoff.find_node([node], run=run)
                    self.assertEqual(found is not None, expected)

            def broken(arguments, **kwargs):
                raise OSError("cannot start")
            self.assertIsNone(update_handoff.find_node([node], run=broken))
        self.assertIsNone(update_handoff.find_node([Path(temporary) / "missing" / "node.exe"]))


class HandoffTests(unittest.TestCase):
    def setup_layout(self, root, release, content):
        install_dir = root / "Program Files" / "FlowShift"
        install_dir.mkdir(parents=True)
        (install_dir / update_handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
        package = (root / "ProgramData" / "FlowShift" / "updates" / "downloads"
                   / update_download.asset_basename(release.version, release.channel))
        package.parent.mkdir(parents=True)
        package.write_bytes(content)
        asset = DownloadedAsset(str(package), package.name, release.version,
                                len(content), release.installer_sha256)
        return install_dir, asset

    def test_source_package_is_handed_off_with_its_kind_and_needs_node(self):
        content = source_zip()
        release = dev_descriptor(content)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_dir, asset = self.setup_layout(root, release, content)
            plans = []

            def launcher(arguments, **kwargs):
                plan_path = Path(arguments[arguments.index("-PlanPath") + 1])
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                plans.append(plan)
                ack = plan_path.parent / "acks" / f"update_ack-{plan['request_id']}.json"
                ack.parent.mkdir(parents=True, exist_ok=True)
                ack.write_text(json.dumps({"schema_version": 1, "request_id": plan["request_id"],
                                           "status": "accepted", "runner_pid": 5}),
                               encoding="utf-8")

            values = dict(runtime_root=install_dir, runtime_pid=1234, launcher=launcher,
                          ack_timeout=1, _program_files=root / "Program Files",
                          _program_data=root / "ProgramData", _request_id="b" * 32,
                          _created_at="2026-11-19T12:00:00Z")
            with self.assertRaises(update_handoff.UpdateHandoffError) as raised:
                update_handoff.handoff_update(release, asset, node_finder=lambda: None, **values)
            self.assertEqual(raised.exception.code, "node_missing")
            self.assertEqual(plans, [])  # FlowShift was not stopped

            result = update_handoff.handoff_update(
                release, asset, node_finder=lambda: ("node.exe", "v24.11.0"), **values)
            self.assertEqual(result.status, "started")
            plan = plans[0]
            self.assertEqual((plan["from_version"], plan["to_version"], plan["package_kind"]),
                             ("0.7.0-dev.29", "0.7.0-dev.30", "source"))
            self.assertTrue(plan["installer_path"].endswith("FlowShift-Source-0.7.0-dev.30.zip"))
            self.assertNotIn(TOKEN, json.dumps(plan))
            with self.assertRaises(update_handoff.UpdateHandoffError):
                update_handoff.handoff_update(replace(release, channel=CHANNEL_STABLE),
                                              asset, node_finder=lambda: ("n", "v24"), **values)


class AutomaticInstallHoldTests(unittest.TestCase):
    def test_failed_version_is_not_installed_again_automatically(self):
        content = b"dev source"
        release = dev_descriptor(content)
        installs = []

        def transport(request):
            return DownloadResponse(200, {}, io.BytesIO(content))

        with tempfile.TemporaryDirectory() as temporary:
            official = Path(temporary) / "Program Files" / "FlowShift"
            official.mkdir(parents=True)
            result_path = Path(temporary) / "updates" / "last_update_result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps({
                "schema_version": 1, "from_version": "0.7.0-dev.29", "to_version": "0.7.0-dev.30",
                "started_at": "2026-11-19T12:00:00Z", "finished_at": "2026-11-19T12:03:00Z",
                "result": "rollback_success", "error": "install_flowshift.ps1 exited with code 1",
            }), encoding="utf-8")
            settings = {"enabled": True, "check_on_start": True, "channel": CHANNEL_DEVELOPMENT,
                        "policy": "install"}
            manager = UpdateManager(
                data_dir=temporary, current_version="0.7.0-dev.29",
                updater_version="0.7.0-dev.29",
                discovery=lambda **kwargs: UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                download_transport=transport, runtime_root=official,
                official_install_dir=official, program_files=Path(temporary) / "other",
                channel_getter=lambda: CHANNEL_DEVELOPMENT, token_provider=lambda: TOKEN,
                handoff=lambda descriptor, asset: installs.append(descriptor.version),
                safety_query=lambda: {"safe": True, "reason": "safe", "blockers": []})
            try:
                manager.start_automatic_updates(lambda: settings, lambda: True,
                                                startup_delay=2.0, poll_interval=0.05,
                                                startup_wait=lambda event, delay: False)
                deadline = time.monotonic() + 3
                while manager.snapshot()["state"] != DOWNLOADED and time.monotonic() < deadline:
                    time.sleep(0.02)
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], DOWNLOADED)
                self.assertTrue(snapshot["can_install"])
                self.assertTrue(snapshot["automatic_install_held"])
                time.sleep(0.4)
                self.assertEqual(installs, [])

                # A successful earlier result (or another version) does not hold anything.
                data = json.loads(result_path.read_text(encoding="utf-8"))
                data["result"] = "success"
                result_path.write_text(json.dumps(data), encoding="utf-8")
                self.assertFalse(manager.snapshot()["automatic_install_held"])
            finally:
                manager.shutdown()


class CredentialTests(unittest.TestCase):
    def test_token_validation_and_status_never_reveal_the_token(self):
        store = update_credentials.MemoryCredentialStore()
        self.assertFalse(update_credentials.access_status(store)["configured"])
        store.write(f"  {TOKEN}  ")
        self.assertEqual(store.read(), TOKEN)
        status = update_credentials.access_status(store)
        self.assertEqual(status, {"configured": True, "secure_store": False, "error": None})
        self.assertNotIn(TOKEN, json.dumps(status))
        for bad in ("", "short", "with space inside", "x" * 600, 12345678):
            with self.subTest(bad=bad), self.assertRaises(update_credentials.CredentialError):
                store.write(bad)
        self.assertTrue(store.delete())
        self.assertIsNone(update_credentials.token_provider(store)())


class ManagerChannelTests(unittest.TestCase):
    def make(self, root, channel_box, discovery, **kwargs):
        return UpdateManager(
            data_dir=root, current_version="0.7.0-dev.29", updater_version="0.7.0-dev.29",
            discovery=discovery, runtime_root=Path(root) / "checkout",
            channel_getter=lambda: channel_box["channel"],
            token_provider=lambda: TOKEN, **kwargs)

    def test_check_uses_configured_channel_and_records_source(self):
        calls = []

        def discovery(**kwargs):
            calls.append(kwargs)
            if kwargs["channel"] == CHANNEL_DEVELOPMENT:
                return UpdateResult(STATUS_UPDATE_AVAILABLE, dev_descriptor())
            return UpdateResult(STATUS_UP_TO_DATE)

        box = {"channel": CHANNEL_DEVELOPMENT}
        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make(temporary, box, discovery)
            try:
                self.assertEqual(manager.check_for_updates().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], UPDATE_AVAILABLE)
                self.assertEqual(snapshot["latest_version"], "0.7.0-dev.30")
                self.assertEqual(snapshot["checked_channel"], CHANNEL_DEVELOPMENT)
                self.assertEqual(snapshot["checked_source"], "forgejo")
                self.assertEqual(snapshot["source"]["source"], "forgejo")
                self.assertIs(calls[0]["token_provider"](), TOKEN)

                # Switching back to stable: a dev descriptor may not be downloaded.
                box["channel"] = CHANNEL_STABLE
                result = manager.download_update()
                self.assertEqual((result.status, result.error_code),
                                 (RESULT_NOT_AVAILABLE, ERROR_CHANNEL_CHANGED))
                self.assertTrue(manager._periodic_check_due(manager.snapshot()))
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], UP_TO_DATE)
                self.assertEqual(snapshot["checked_channel"], CHANNEL_STABLE)
                self.assertEqual(snapshot["source"]["source"], "github")
                self.assertFalse(manager._periodic_check_due(snapshot))
            finally:
                manager.shutdown()

    def test_development_rechecks_periodically_and_finds_the_next_merge(self):
        versions = ["0.7.0-dev.29", "0.7.0-dev.30"]
        lock = threading.Lock()

        def discovery(**kwargs):
            with lock:
                newest = versions[-1] if len(versions) > 2 else versions[0]
            if newest == "0.7.0-dev.29":
                return UpdateResult(STATUS_UP_TO_DATE)
            return UpdateResult(STATUS_UPDATE_AVAILABLE, dev_descriptor(version=newest))

        box = {"channel": CHANNEL_DEVELOPMENT}
        settings = {"enabled": True, "check_on_start": True, "channel": CHANNEL_DEVELOPMENT,
                    "policy": "notify"}
        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make(temporary, box, discovery, development_check_interval=0.2)
            try:
                manager.start_automatic_updates(lambda: settings, lambda: True,
                                                startup_delay=2.0, poll_interval=0.05,
                                                startup_wait=lambda event, delay: False)
                deadline = time.monotonic() + 3
                while manager.snapshot()["state"] != UP_TO_DATE and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(manager.snapshot()["state"], UP_TO_DATE)
                with lock:
                    versions.append("0.7.0-dev.30")  # the next merge lands on main
                deadline = time.monotonic() + 3
                while manager.snapshot()["state"] != UPDATE_AVAILABLE and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(manager.snapshot()["state"], UPDATE_AVAILABLE)
                self.assertEqual(manager.snapshot()["latest_version"], "0.7.0-dev.30")
            finally:
                manager.shutdown()

    def test_unreachable_source_keeps_last_successful_check(self):
        answers = [UpdateResult(STATUS_UP_TO_DATE),
                   UpdateResult(STATUS_ERROR, error_code=update_model.ERROR_SOURCE_UNREACHABLE,
                                message=update_client.UNREACHABLE_MESSAGE)]

        def discovery(**kwargs):
            return answers.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make(temporary, {"channel": CHANNEL_DEVELOPMENT}, discovery)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                good = manager.snapshot()["last_successful_check_at"]
                self.assertIsNotNone(good)
                time.sleep(1.1)
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], ERROR)
                self.assertEqual(snapshot["last_error"]["code"],
                                 update_model.ERROR_SOURCE_UNREACHABLE)
                self.assertEqual(snapshot["last_successful_check_at"], good)
                self.assertNotEqual(snapshot["last_check_at"], good)
            finally:
                manager.shutdown()

    def test_downloaded_dev_package_is_not_installable_after_switch_to_stable(self):
        content = b"dev installer"
        release = dev_descriptor(content)
        box = {"channel": CHANNEL_DEVELOPMENT}

        def transport(request):
            return DownloadResponse(200, {}, io.BytesIO(content))

        with tempfile.TemporaryDirectory() as temporary:
            official = Path(temporary) / "Program Files" / "FlowShift"
            official.mkdir(parents=True)
            manager = UpdateManager(
                data_dir=temporary, current_version="0.7.0-dev.29",
                updater_version="0.7.0-dev.29",
                discovery=lambda **kwargs: UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                download_transport=transport, runtime_root=official,
                official_install_dir=official, program_files=Path(temporary) / "other",
                channel_getter=lambda: box["channel"], token_provider=lambda: TOKEN)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.download_update().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["state"], DOWNLOADED)
                self.assertTrue(manager.snapshot()["can_install"])
                box["channel"] = CHANNEL_STABLE
                snapshot = manager.snapshot()
                self.assertFalse(snapshot["can_install"])
                self.assertEqual(snapshot["blocked_reason"], ERROR_CHANNEL_CHANGED)
                self.assertEqual(manager.install_update().status, RESULT_NOT_AVAILABLE)
            finally:
                manager.shutdown()


class FakeManager:
    def __init__(self):
        self.checks = 0

    def snapshot(self):
        return {"state": "up_to_date", "current_version": "0.7.0-dev.30",
                "checked_channel": "stable", "download_progress": None}

    def check_for_updates(self):
        self.checks += 1


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config


class WebApiChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {
            "config_schema_version": 1,
            "updates": {"enabled": True, "check_on_start": True, "channel": "stable",
                        "policy": "install"},
        })
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.manager = FakeManager()
        self.store = update_credentials.MemoryCredentialStore()
        web_api._refs = {}
        web_api.init(update_manager=self.manager, istate=self.state,
                     update_install_safety=lambda: {"safe": True, "reason": "safe",
                                                    "blockers": []},
                     save_config=lambda cfg: config_schema.save_config(self.config_path, cfg),
                     update_credentials=self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{self.server.server_address[1]}"}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_one_switch_to_development_with_access_shows_source_and_checks(self):
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        update = body["update"]
        self.assertEqual(update["channel"], "stable")
        self.assertEqual(update["source"]["source"], "github")
        self.assertFalse(update["development_access"]["configured"])

        status, body = self.request("POST", "/api/update/development-access", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["development_access"]["configured"])
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(self.manager.checks, 0)  # still stable: no Forgejo check yet

        status, _ = self.request("POST", "/api/update/settings", {
            "enabled": True, "check_on_start": True, "channel": "development",
            "policy": "install"})
        self.assertEqual(status, 200)
        self.assertEqual(self.manager.checks, 1)
        saved = self.config_path.read_text(encoding="utf-8")
        self.assertIn('"development"', saved)
        self.assertNotIn(TOKEN, saved)

        status, body = self.request("GET", "/api/update/status")
        update = body["update"]
        self.assertEqual(update["channel"], "development")
        self.assertEqual(update["source"]["source"], "forgejo")
        self.assertEqual(update["checked_channel"], "stable")
        self.assertTrue(update["development_access"]["configured"])
        self.assertNotIn(TOKEN, json.dumps(body))

        # Saving the same channel again does not trigger another check.
        self.request("POST", "/api/update/settings", {
            "enabled": True, "check_on_start": True, "channel": "development",
            "policy": "install"})
        self.assertEqual(self.manager.checks, 1)

    def test_access_endpoint_validates_and_removes(self):
        for body in ({"token": "short"}, {"token": 5}, {"token": TOKEN, "extra": 1}, {}):
            with self.subTest(body=body):
                status, _ = self.request("POST", "/api/update/development-access", body)
                self.assertEqual(status, 400)
        self.assertIsNone(self.store.read())
        self.request("POST", "/api/update/development-access", {"token": TOKEN})
        status, body = self.request("POST", "/api/update/development-access", {"token": None})
        self.assertEqual(status, 200)
        self.assertFalse(body["development_access"]["configured"])
        self.assertIsNone(self.store.read())


if __name__ == "__main__":
    unittest.main()
