"""Offline tests for the development update channel (Forgejo) and channel switching."""
from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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
    FORGEJO_ORIGIN,
    FORGEJO_RELEASES_API,
    ReleaseDescriptor,
    STATUS_ERROR,
    STATUS_UPDATE_AVAILABLE,
    STATUS_UP_TO_DATE,
    UpdateResult,
)
from update_state import DOWNLOADED, ERROR, UPDATE_AVAILABLE, UP_TO_DATE
import web_api


# Obvious dummy; never shaped like a real access key.
TOKEN = "attrappe-zugang"
RELEASES = f"{FORGEJO_ORIGIN}/private-platform/flowshift/releases"


def forgejo_release(version, *, prerelease=True, draft=False, assets=None, host=None,
                    size=100):
    base = host or FORGEJO_ORIGIN
    download = f"{base}/private-platform/flowshift/releases/download/v{version}"
    if assets is None:
        assets = [
            {"name": "FlowShift-Setup.exe", "size": size,
             "browser_download_url": f"{download}/FlowShift-Setup.exe"},
            {"name": "update-manifest.json", "size": 300,
             "browser_download_url": f"{download}/update-manifest.json"},
            {"name": "SHA256SUMS.txt", "size": 200,
             "browser_download_url": f"{download}/SHA256SUMS.txt"},
        ]
    return {
        "tag_name": f"v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"{RELEASES}/tag/v{version}",
        "body": f"FlowShift {version}",
        "assets": assets,
    }


def manifest(version, size=100, digest="a" * 64, channel=CHANNEL_DEVELOPMENT):
    return {
        "schema_version": 1,
        "version": version,
        "tag": f"v{version}",
        "channel": channel,
        "installer": {"name": "FlowShift-Setup.exe", "size": size, "sha256": digest},
        "minimum_updater_version": "0.4.0",
    }


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
        status, body = answer
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        return HttpResponse(status=status, headers={}, body=body)


def dev_descriptor(content=b"dev installer", version="0.7.0-dev.27", current="0.7.0-dev.22"):
    download = f"{RELEASES}/download/v{version}"
    return ReleaseDescriptor(
        current_version=current,
        version=version,
        tag=f"v{version}",
        relation="newer",
        release_url=f"{RELEASES}/tag/v{version}",
        release_notes="dev",
        installer_url=f"{download}/FlowShift-Setup.exe",
        installer_size=len(content),
        installer_sha256=hashlib.sha256(content).hexdigest(),
        manifest_url=f"{download}/update-manifest.json",
        checksums_url=f"{download}/SHA256SUMS.txt",
        minimum_updater_version="0.4.0",
        channel=CHANNEL_DEVELOPMENT,
    )


class ModelTests(unittest.TestCase):
    def test_highest_complete_prerelease_wins_and_incomplete_ones_are_skipped(self):
        incomplete = forgejo_release("0.7.0-dev.30", assets=[])
        draft = forgejo_release("0.7.0-dev.29", draft=True)
        release = update_model.select_development_release(
            [forgejo_release("0.7.0-dev.26"), incomplete, forgejo_release("0.7.0-dev.28"),
             draft, {"tag_name": "garbage"}],
            "0.7.0-dev.22")
        self.assertEqual(release.version_text, "0.7.0-dev.28")
        self.assertEqual(release.relation, "newer")
        self.assertEqual(release.channel, CHANNEL_DEVELOPMENT)

    def test_dev_ordering_is_numeric_and_stable_beats_its_prereleases(self):
        release = update_model.select_development_release(
            [forgejo_release("0.7.0-dev.9"), forgejo_release("0.7.0-dev.10")], "0.7.0-dev.9")
        self.assertEqual(release.version_text, "0.7.0-dev.10")
        release = update_model.select_development_release(
            [forgejo_release("0.7.0-dev.40"), forgejo_release("0.7.0", prerelease=False)],
            "0.7.0-dev.26")
        self.assertEqual(release.version_text, "0.7.0")

    def test_foreign_hosts_and_url_credentials_are_rejected(self):
        for url in ("http://evil.example/x", "https://github.com/CryseXIII/flowshift",
                    "http://user:pw@forgejo.thegoodhunter.vps/x",
                    "http://forgejo.thegoodhunter.vps:3001/x", "ftp://forgejo.thegoodhunter.vps/x"):
            with self.subTest(url=url), self.assertRaises(update_model.UpdateValidationError):
                update_model.validate_forgejo_url(url)
        with self.assertRaises(update_model.NoStableRelease):
            update_model.select_development_release(
                [forgejo_release("0.7.0-dev.27", host="http://evil.example")], "0.7.0-dev.22")

    def test_stable_channel_still_rejects_prereleases_and_forgejo(self):
        with self.assertRaises(update_model.NoStableRelease):
            update_model.validate_release_payload(forgejo_release("0.7.0-dev.27"), "0.6.3")
        with self.assertRaises(update_model.UpdateValidationError):
            update_model.validate_release_payload(
                forgejo_release("0.7.0", prerelease=False), "0.6.3")

    def test_manifest_channel_rules(self):
        release = update_model.select_development_release(
            [forgejo_release("0.7.0-dev.27")], "0.7.0-dev.22")
        descriptor = update_model.validate_manifest_payload(manifest("0.7.0-dev.27"), release)
        self.assertEqual(descriptor.channel, CHANNEL_DEVELOPMENT)
        with self.assertRaises(update_model.UpdateValidationError):
            update_model.validate_manifest_payload(
                manifest("0.7.0-dev.27", channel="nightly"), release)
        stable = replace(release, channel=CHANNEL_STABLE)
        with self.assertRaises(update_model.UpdateValidationError):
            update_model.validate_manifest_payload(manifest("0.7.0-dev.27"), stable)

    def test_config_accepts_development_and_falls_back_on_unknown(self):
        self.assertEqual(config_schema.normalize_updates({"channel": "Development"})["channel"],
                         CHANNEL_DEVELOPMENT)
        self.assertEqual(config_schema.normalize_updates({"channel": "beta"})["channel"],
                         CHANNEL_STABLE)


class DevelopmentDiscoveryTests(unittest.TestCase):
    def routes(self, version="0.7.0-dev.27", list_status=200):
        manifest_url = (f"{RELEASES}/download/v{version}/update-manifest.json")
        return {
            FORGEJO_RELEASES_API: (list_status, [forgejo_release("0.7.0-dev.26"),
                                                 forgejo_release(version)]),
            manifest_url: (200, manifest(version)),
        }

    def discover(self, transport, current="0.7.0-dev.22", token=TOKEN):
        return update_client.discover_release(
            current_version=current, transport=transport, channel=CHANNEL_DEVELOPMENT,
            token_provider=lambda: token)

    def test_newer_package_found_and_token_only_goes_to_forgejo(self):
        transport = RoutingTransport(self.routes())
        result = self.discover(transport)
        self.assertEqual(result.status, STATUS_UPDATE_AVAILABLE)
        self.assertEqual(result.release.version, "0.7.0-dev.27")
        self.assertEqual(result.release.channel, CHANNEL_DEVELOPMENT)
        self.assertEqual(len(transport.requests), 2)
        for request in transport.requests:
            self.assertTrue(request.url.startswith(FORGEJO_ORIGIN + "/"))
            self.assertEqual(request.headers["Authorization"], f"token {TOKEN}")

    def test_same_version_is_up_to_date(self):
        result = self.discover(RoutingTransport(self.routes()), current="0.7.0-dev.27")
        self.assertEqual(result.status, STATUS_UP_TO_DATE)

    def test_missing_token_makes_no_request(self):
        transport = RoutingTransport({})
        result = self.discover(transport, token=None)
        self.assertEqual((result.status, result.error_code),
                         (STATUS_ERROR, update_model.ERROR_CREDENTIALS_MISSING))
        self.assertEqual(transport.requests, [])

    def test_rejected_token(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                result = self.discover(RoutingTransport(self.routes(list_status=status)))
                self.assertEqual(result.error_code, update_model.ERROR_CREDENTIALS_REJECTED)

    def test_without_vpn_the_source_is_reported_unreachable(self):
        import socket
        for error in (socket.gaierror("no dns"), ConnectionRefusedError(), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                transport = RoutingTransport({FORGEJO_RELEASES_API: error})
                result = self.discover(transport)
                self.assertEqual(result.error_code, update_model.ERROR_SOURCE_UNREACHABLE)
                self.assertNotIn(TOKEN, result.message)

    def test_manifest_redirect_to_foreign_host_is_refused(self):
        routes = self.routes()
        manifest_url = f"{RELEASES}/download/v0.7.0-dev.27/update-manifest.json"
        transport = RoutingTransport(routes)
        original = transport.request

        def redirecting(request):
            if request.url == manifest_url:
                transport.requests.append(request)
                return HttpResponse(302, {"Location": "http://evil.example/m.json"}, b"")
            return original(request)

        transport.request = redirecting
        result = self.discover(transport)
        self.assertEqual(result.error_code, update_model.ERROR_INVALID_URL)
        self.assertTrue(all(r.url.startswith(FORGEJO_ORIGIN) for r in transport.requests))


class DownloadTests(unittest.TestCase):
    def test_development_download_sends_token_and_verifies_hash(self):
        content = b"dev installer bytes"
        release = dev_descriptor(content)
        seen = []

        def transport(request):
            seen.append(request)
            return DownloadResponse(200, {"Content-Length": str(len(content))}, io.BytesIO(content))

        with tempfile.TemporaryDirectory() as temporary:
            asset = update_download.download_installer(
                release, temporary, "0.7.0-dev.22", transport=transport,
                token_provider=lambda: TOKEN)
            self.assertEqual(asset.sha256, release.installer_sha256)
            self.assertEqual(Path(asset.path).read_bytes(), content)
        self.assertEqual(seen[0].headers["Authorization"], f"token {TOKEN}")

    def test_development_download_needs_token_and_refuses_foreign_redirect(self):
        release = dev_descriptor()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(update_download.DownloadError) as raised:
                update_download.download_installer(release, temporary, "0.7.0-dev.22",
                                                   transport=lambda r: None)
            self.assertEqual(raised.exception.code, update_model.ERROR_CREDENTIALS_MISSING)

            def redirect(request):
                return DownloadResponse(302, {"Location": "https://github.com/x"}, io.BytesIO())

            with self.assertRaises(update_download.DownloadError) as raised:
                update_download.download_installer(release, temporary, "0.7.0-dev.22",
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
            update_download.download_installer(release, temporary, "0.6.3", transport=transport,
                                               token_provider=lambda: TOKEN)
        self.assertNotIn("Authorization", seen[0].headers)


class HandoffTests(unittest.TestCase):
    def test_development_prerelease_is_handed_off_and_stable_stays_strict(self):
        content = b"dev setup"
        release = dev_descriptor(content)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_dir = root / "Program Files" / "FlowShift"
            install_dir.mkdir(parents=True)
            (install_dir / update_handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
            installer = (root / "ProgramData" / "FlowShift" / "updates" / "downloads"
                         / f"FlowShift-Setup-{release.version}.exe")
            installer.parent.mkdir(parents=True)
            installer.write_bytes(content)
            asset = DownloadedAsset(str(installer), installer.name, release.version,
                                    len(content), release.installer_sha256)
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
            result = update_handoff.handoff_update(release, asset, **values)
            self.assertEqual(result.status, "started")
            self.assertEqual((plans[0]["from_version"], plans[0]["to_version"]),
                             ("0.7.0-dev.22", "0.7.0-dev.27"))
            with self.assertRaises(update_handoff.UpdateHandoffError):
                update_handoff.handoff_update(replace(release, channel=CHANNEL_STABLE),
                                              asset, **values)


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
            data_dir=root, current_version="0.7.0-dev.22", updater_version="0.7.0-dev.22",
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
                self.assertEqual(snapshot["latest_version"], "0.7.0-dev.27")
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
        versions = ["0.7.0-dev.22", "0.7.0-dev.27"]
        lock = threading.Lock()

        def discovery(**kwargs):
            with lock:
                newest = versions[-1] if len(versions) > 2 else versions[0]
            if newest == "0.7.0-dev.22":
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
                    versions.append("0.7.0-dev.27")  # the next merge is packaged
                deadline = time.monotonic() + 3
                while manager.snapshot()["state"] != UPDATE_AVAILABLE and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(manager.snapshot()["state"], UPDATE_AVAILABLE)
                self.assertEqual(manager.snapshot()["latest_version"], "0.7.0-dev.27")
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
                data_dir=temporary, current_version="0.7.0-dev.22",
                updater_version="0.7.0-dev.22",
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
        return {"state": "up_to_date", "current_version": "0.7.0-dev.27",
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
        headers = {"Content-Type": "application/json"} if payload is not None else {}
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
