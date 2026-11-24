"""The stable channel end to end, as on a PC without Git, Node.js or Forgejo.

Viktor 24.11.: other PCs get updates only from the GitHub repository (Forgejo
is mirrored there now and then); they have no Git, and they must be able to
update with every policy and by hand in Settings > Software Update. These tests
drive the real discovery, download, update manager and handoff against a fake
GitHub and fail on any request to another host, any use of the Forgejo access,
any Node.js probe or a Forgejo error shown on stable.
"""
from __future__ import annotations

import ast
from functools import partial
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import update_client
import update_handoff
from update_download import DownloadResponse
from update_manager import (
    FORGEJO_ONLY_ERRORS,
    RESULT_QUEUED,
    UpdateManager,
)
from update_model import (
    CHANNEL_DEVELOPMENT,
    CHANNEL_STABLE,
    ERROR_CREDENTIALS_MISSING,
    ERROR_SOURCE_UNREACHABLE,
    REPOSITORY,
    STATUS_ERROR,
    UpdateResult,
)
from update_state import DOWNLOADED, ERROR, IDLE, INSTALLING, UPDATE_AVAILABLE, UP_TO_DATE

ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = ROOT / "src" / "python"
GITHUB_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com"}


class NotAllowed(AssertionError):
    pass


def forbidden(name):
    def call(*args, **kwargs):
        raise NotAllowed(f"{name} must not be used on the stable channel")
    return call


class FakeGitHub:
    """releases/latest, the manifest and the Setup.exe of CryseXIII/flowshift."""

    def __init__(self, version=None):
        self.lock = threading.Lock()
        self.urls = []
        self.offline = False
        self.release = None
        if version:
            self.publish(version)

    def publish(self, version, prerelease=False):
        setup = f"FlowShift-Setup {version}".encode("ascii")
        digest = hashlib.sha256(setup).hexdigest()
        base = f"https://github.com/{REPOSITORY}/releases/download/v{version}"
        payload = {
            "tag_name": f"v{version}", "draft": False, "prerelease": prerelease,
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/v{version}",
            "body": f"FlowShift {version}",
            "assets": [
                {"name": "FlowShift-Setup.exe", "size": len(setup),
                 "digest": f"sha256:{digest}",
                 "browser_download_url": f"{base}/FlowShift-Setup.exe"},
                {"name": "update-manifest.json", "size": 1,
                 "browser_download_url": f"{base}/update-manifest.json"},
                {"name": "SHA256SUMS.txt", "size": 1,
                 "browser_download_url": f"{base}/SHA256SUMS.txt"},
            ],
        }
        manifest = {
            "schema_version": 1, "version": version, "tag": f"v{version}",
            "channel": "stable", "minimum_updater_version": "0.6.0",
            "installer": {"name": "FlowShift-Setup.exe", "size": len(setup), "sha256": digest},
        }
        with self.lock:
            self.release = {"payload": payload, "manifest": manifest, "setup": setup,
                            "base": base}

    def _route(self, url, headers):
        with self.lock:
            self.urls.append(url)
            if self.offline:
                raise URLError("network is unreachable")
            release = self.release
        host = urlparse(url).hostname
        if host not in GITHUB_HOSTS:
            raise NotAllowed(f"request to {host} on the stable channel")
        if any(name.lower() == "authorization" for name in headers):
            raise NotAllowed("an access token was sent to GitHub")
        if url == update_client.LATEST_RELEASE_API:
            if release is None:
                return 404, b'{"message": "Not Found"}'
            return 200, json.dumps(release["payload"]).encode("utf-8")
        if release is not None and url == f"{release['base']}/update-manifest.json":
            return 200, json.dumps(release["manifest"]).encode("utf-8")
        if release is not None and url == f"{release['base']}/FlowShift-Setup.exe":
            return 200, release["setup"]
        return 404, b""

    def api(self, request):
        status, body = self._route(request.url, request.headers)
        return update_client.HttpResponse(status, {}, body)

    def download(self, request):
        status, body = self._route(request.url, request.headers)
        return DownloadResponse(status, {}, io.BytesIO(body))

    def requested(self, suffix):
        with self.lock:
            return [url for url in self.urls if url.endswith(suffix)]


class StablePC:
    """An installed FlowShift (Program Files / ProgramData) on the stable channel."""

    def __init__(self, root, github, current_version, settings=None, **kwargs):
        self.root = Path(root)
        self.program_files = self.root / "Program Files"
        self.program_data = self.root / "ProgramData"
        self.install_dir = self.program_files / "FlowShift"
        self.install_dir.mkdir(parents=True)
        (self.install_dir / update_handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
        self.settings = dict(settings or {"enabled": True, "check_on_start": True,
                                          "channel": CHANNEL_STABLE, "policy": "notify"})
        self.plans = []
        self.shutdowns = []
        self.manager = UpdateManager(
            data_dir=self.program_data / "FlowShift",
            current_version=current_version, updater_version=current_version,
            discovery=partial(update_client.discover_release, transport=github.api),
            download_transport=github.download,
            runtime_root=self.install_dir, official_install_dir=self.install_dir,
            program_files=self.program_files,
            handoff=self.handoff,
            shutdown_callback=lambda: self.shutdowns.append(True),
            channel_getter=lambda: self.settings["channel"],
            # A PC without the Forgejo access: reading it is already a failure.
            token_provider=forbidden("the Forgejo access"),
            install_retry_interval=0.05,
            **kwargs)

    def handoff(self, descriptor, asset):
        return update_handoff.handoff_update(
            descriptor, asset, self.install_dir, runtime_pid=4321,
            launcher=self.accepting_launcher, ack_timeout=2, poll_interval=0.01,
            node_finder=forbidden("the Node.js probe"), elevation_check=lambda: True,
            lock_check=lambda data_dir: False,
            _program_files=self.program_files, _program_data=self.program_data)

    def accepting_launcher(self, arguments, **kwargs):
        plan_path = Path(arguments[arguments.index("-PlanPath") + 1])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.plans.append(plan)
        ack = plan_path.parent / "acks" / f"update_ack-{plan['request_id']}.json"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text(json.dumps({"schema_version": 1, "request_id": plan["request_id"],
                                   "status": "accepted", "runner_pid": 99}), encoding="utf-8")
        return object()

    def start_policy(self):
        self.manager.start_automatic_updates(
            lambda: dict(self.settings), lambda: True, startup_delay=2.0,
            poll_interval=0.02, startup_wait=lambda event, delay: False)

    def wait_state(self, state, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.manager.snapshot()["state"] == state:
                return True
            time.sleep(0.01)
        return self.manager.snapshot()["state"] == state


class NewInstallationDefaultsTests(unittest.TestCase):
    def test_new_installation_uses_stable(self):
        self.assertEqual(config_schema.DEFAULT_UPDATES["channel"], CHANNEL_STABLE)
        self.assertEqual(config_schema.normalize_updates({})["channel"], CHANNEL_STABLE)
        self.assertEqual(config_schema.normalize_updates({"channel": "nightly"})["channel"],
                         CHANNEL_STABLE)
        example = json.loads((PYTHON_DIR / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["updates"]["channel"], CHANNEL_STABLE)
        # The config install_flowshift.ps1 writes on a fresh installation.
        installer = (ROOT / "install_flowshift.ps1").read_text(encoding="utf-8")
        written = re.findall(r"updates\s*=\s*\[ordered\]@\{[^}]*channel = '(\w+)'", installer)
        self.assertEqual(written, ["stable"])


class ManualStepsTests(unittest.TestCase):
    def test_check_download_install_by_hand_from_a_development_build(self):
        # Automatic updates off: only the three buttons in Software Update.
        # The PC ran a development state once; the stable 0.7.0 must still
        # install through GitHub (before: refused as invalid_release).
        github = FakeGitHub("0.7.0")
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.7.0-dev.50",
                          settings={"enabled": False, "check_on_start": False,
                                    "channel": CHANNEL_STABLE, "policy": "notify"})
            manager = pc.manager
            try:
                self.assertEqual(manager.check_for_updates().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], UPDATE_AVAILABLE)
                self.assertEqual(snapshot["latest_version"], "0.7.0")
                self.assertEqual(snapshot["source"]["source"], "github")
                self.assertIsNone(snapshot["last_error"])

                self.assertEqual(manager.download_update().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], DOWNLOADED)
                self.assertTrue(snapshot["can_install"])
                downloaded = pc.program_data / "FlowShift" / "updates" / "downloads"
                self.assertEqual(sorted(p.name for p in downloaded.iterdir()),
                                 ["FlowShift-Setup-0.7.0.exe"])

                self.assertEqual(manager.install_update().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["state"], INSTALLING)
                self.assertEqual(len(pc.plans), 1)
                plan = pc.plans[0]
                self.assertEqual((plan["from_version"], plan["to_version"], plan["package_kind"]),
                                 ("0.7.0-dev.50", "0.7.0", "installer"))
                self.assertTrue(plan["installer_path"].endswith("FlowShift-Setup-0.7.0.exe"))
                self.assertEqual(pc.shutdowns, [True])
            finally:
                manager.shutdown()
        self.assertEqual(len(github.requested("FlowShift-Setup.exe")), 1)

    def test_offline_check_reports_an_error_with_a_reason(self):
        github = FakeGitHub("0.7.0")
        github.offline = True
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.6.3")
            try:
                pc.manager.check_for_updates()
                self.assertTrue(pc.manager.wait_for_quiescence())
                snapshot = pc.manager.snapshot()
                self.assertEqual(snapshot["state"], ERROR)
                self.assertEqual(snapshot["last_error"]["operation"], "check")
                self.assertNotIn(snapshot["last_error"]["code"], FORGEJO_ONLY_ERRORS)
                self.assertTrue(snapshot["last_error"]["message"])
                github.offline = False
                pc.manager.check_for_updates()
                self.assertTrue(pc.manager.wait_for_quiescence())
                self.assertEqual(pc.manager.snapshot()["state"], UPDATE_AVAILABLE)
            finally:
                pc.manager.shutdown()

    def test_stable_never_installs_a_prerelease(self):
        github = FakeGitHub()
        github.publish("0.8.0-dev.1", prerelease=True)
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.7.0")
            try:
                pc.manager.check_for_updates()
                self.assertTrue(pc.manager.wait_for_quiescence())
                self.assertEqual(pc.manager.snapshot()["state"], UP_TO_DATE)
                self.assertEqual(github.requested("FlowShift-Setup.exe"), [])
            finally:
                pc.manager.shutdown()


class AutomaticPolicyTests(unittest.TestCase):
    def test_running_pc_picks_up_a_new_release_with_every_policy(self):
        # FlowShift runs for days: the release appears on GitHub after start
        # and is found by the periodic stable check, without a restart.
        expected = {"notify": UPDATE_AVAILABLE, "download": DOWNLOADED, "install": INSTALLING}
        for policy, state in expected.items():
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temporary:
                github = FakeGitHub("0.6.3")
                pc = StablePC(temporary, github, "0.6.3",
                              settings={"enabled": True, "check_on_start": True,
                                        "channel": CHANNEL_STABLE, "policy": policy},
                              stable_check_interval=0.2)
                try:
                    pc.start_policy()
                    self.assertTrue(pc.wait_state(UP_TO_DATE))
                    github.publish("0.7.0")
                    self.assertTrue(pc.wait_state(state), pc.manager.snapshot()["state"])
                    time.sleep(0.15)
                    self.assertEqual(pc.manager.snapshot()["state"], state)
                    self.assertEqual(len(github.requested("FlowShift-Setup.exe")),
                                     0 if policy == "notify" else 1)
                    self.assertEqual(len(pc.plans), 1 if policy == "install" else 0)
                    if pc.plans:
                        self.assertEqual(pc.plans[0]["package_kind"], "installer")
                finally:
                    pc.manager.shutdown()

    def test_check_on_start_off_waits_for_the_interval(self):
        github = FakeGitHub("0.7.0")
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.6.3",
                          settings={"enabled": True, "check_on_start": False,
                                    "channel": CHANNEL_STABLE, "policy": "notify"},
                          stable_check_interval=0.4)
            try:
                pc.start_policy()
                time.sleep(0.2)
                self.assertEqual(github.requested("releases/latest"), [])
                self.assertTrue(pc.wait_state(UPDATE_AVAILABLE))
            finally:
                pc.manager.shutdown()

    def test_default_stable_interval_is_hours_not_minutes(self):
        github = FakeGitHub("0.6.3")
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.6.3")
            try:
                pc.start_policy()
                self.assertTrue(pc.wait_state(UP_TO_DATE))
                time.sleep(0.3)
                self.assertEqual(len(github.requested("releases/latest")), 1)
                self.assertFalse(pc.manager._periodic_check_due(pc.manager.snapshot()))
            finally:
                pc.manager.shutdown()


class NoForgejoErrorOnStableTests(unittest.TestCase):
    def test_switch_back_to_stable_hides_forgejo_errors_and_rechecks_github(self):
        github = FakeGitHub("0.7.0")
        stable_discovery = partial(update_client.discover_release, transport=github.api)

        def discovery(**kwargs):
            if kwargs["channel"] == CHANNEL_DEVELOPMENT:
                return UpdateResult(STATUS_ERROR, error_code=ERROR_CREDENTIALS_MISSING,
                                    message="Forgejo access is not set up")
            return stable_discovery(**kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, github, "0.6.3",
                          settings={"enabled": True, "check_on_start": True,
                                    "channel": CHANNEL_DEVELOPMENT, "policy": "notify"})
            manager = pc.manager
            manager._discovery = discovery
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.snapshot()["last_error"]["code"],
                                 ERROR_CREDENTIALS_MISSING)
                pc.settings["channel"] = CHANNEL_STABLE
                snapshot = manager.snapshot()
                self.assertIsNone(snapshot["last_error"])
                self.assertEqual(snapshot["state"], IDLE)
                self.assertEqual(snapshot["source"]["source"], "github")
                self.assertTrue(manager._periodic_check_due(snapshot))
                pc.start_policy()
                self.assertTrue(pc.wait_state(UPDATE_AVAILABLE))
                self.assertEqual(manager.snapshot()["latest_version"], "0.7.0")
            finally:
                manager.shutdown()

    def test_forgejo_only_error_without_channel_record_is_not_the_main_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            pc = StablePC(temporary, FakeGitHub(), "0.6.3")
            try:
                pc.manager._set_error(ERROR_SOURCE_UNREACHABLE, "Forgejo is not reachable", "check")
                snapshot = pc.manager.snapshot()
                self.assertIsNone(snapshot["last_error"])
                self.assertEqual(snapshot["state"], IDLE)
            finally:
                pc.manager.shutdown()

    def test_webgui_explains_every_stable_error(self):
        # Each error the stable check or download can end with has a plain
        # sentence in Software Update (no raw code as the only feedback).
        source = (ROOT / "webgui" / "src" / "components" / "SoftwareUpdateSection.jsx").read_text(
            encoding="utf-8")
        block = re.search(r"const STABLE_ERROR_HINTS = \{(.*?)\n\}", source, re.S)
        self.assertIsNotNone(block)
        hinted = set(re.findall(r"^\s+(\w+):", block.group(1), re.M))
        import update_model
        stable_codes = {update_model.ERROR_RATE_LIMIT, update_model.ERROR_SERVER,
                        update_model.ERROR_HTTP, update_model.ERROR_TIMEOUT,
                        update_model.ERROR_DNS, update_model.ERROR_OFFLINE,
                        update_model.ERROR_TRANSPORT, update_model.ERROR_MISSING_ASSET,
                        update_model.ERROR_INVALID_MANIFEST, update_model.ERROR_DIGEST_MISMATCH,
                        "incompatible_updater", "hash_mismatch", "size_mismatch",
                        "download_http_error", "download_timeout", "download_transport_error",
                        "download_interrupted", "download_filesystem_error"}
        self.assertEqual(stable_codes - hinted, set())


class NoGitNodeForgejoOnThePcTests(unittest.TestCase):
    def test_setup_payload_contains_every_module_the_runtime_imports(self):
        # FlowShift-Setup.exe ships a curated file list; a module missing there
        # would only fail on a PC that installs from GitHub.
        script = (ROOT / "packaging" / "build_release.ps1").read_text(encoding="utf-8")
        listed = set(re.findall(r"'([^']+\.py)'",
                                re.search(r"\$pythonFiles = @\((.*?)\)", script, re.S).group(1)))
        local = {path.stem for path in PYTHON_DIR.glob("*.py")}
        seen, pending = set(), ["tray", "overlay_host", "web_api"]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            tree = ast.parse((PYTHON_DIR / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    pending += [a.name.split(".")[0] for a in node.names
                                if a.name.split(".")[0] in local]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    if node.module.split(".")[0] in local:
                        pending.append(node.module.split(".")[0])
        self.assertIn("update_handoff", seen)
        self.assertEqual(sorted(f"{name}.py" for name in seen if f"{name}.py" not in listed), [])

    def test_setup_and_updater_do_not_call_git_or_node(self):
        iss = (ROOT / "packaging" / "FlowShift.iss").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"RunPowerShellInstaller\('([^']+)'", iss),
                         ["install_flowshift.ps1"])
        updater = (ROOT / "update_flowshift.ps1").read_text(encoding="utf-8")
        code = "\n".join(line for line in updater.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"(?i)\b(git|node|npm|npx)(\.exe|\.cmd)?\b\s", code))
        # install_flowshift.ps1 only reaches Node.js through install_webgui.ps1
        # when the prebuilt webgui\dist does not match (never with Setup.exe,
        # which build_release.ps1 only builds with a VERSION-stamped dist).
        build = (ROOT / "packaging" / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn("$distVersionPath = Require-File", build)


if __name__ == "__main__":
    unittest.main()
